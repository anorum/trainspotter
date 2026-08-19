"""The sessionizer service host, against the batch oracle and its own clock rules.

The cores (StreamingSessionizer, RisingEdgeAlerter) are tested elsewhere; what
this file pins is the hosting discipline that used to be Flink's job:

- the Processor's closed sessions still equal the batch oracle's,
- gap deadlines fire only after the out-of-orderness margin has passed,
- a restart (fresh Processor, full replay) rebuilds an open session with its
  original started_at and session_id - the property the Flink deployment's
  stateless upgrades did NOT have,
- replayed history never re-alerts, but an edge fresh enough to matter does,
- records below the published-offset boundary build state and emit nothing,
  and neither does retiring a deadline the previous life outlived - by sweep
  or by the next train - so a boot cannot restore sessions a backfill deleted,
- but a run any record above the boundary contributed to is closed on both
  paths regardless: no earlier life saw it, so none can have announced it,
- the loop produces sessions keyed by session_id and survives poison messages,
- the argv the deployment passes the container reaches the `run` command.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from blockade.alerts import Alert
from blockade.config import REPO_ROOT, Settings
from blockade.schemas import BlockageSession, CrossingState, ObservationRecord
from blockade.sessions import derive_sessions
from sessionizer.runner import ALERT_FRESHNESS, OUT_OF_ORDERNESS, Processor, app, run_loop
from typer.testing import CliRunner

T0 = datetime(2026, 8, 10, 6, 0, tzinfo=UTC)


def obs(
    minute: float, state: CrossingState, crossing: str = "SE_12TH_CLINTON"
) -> ObservationRecord:
    return ObservationRecord(
        crossing_id=crossing,
        camera_id="odot-678",
        captured_at=T0 + timedelta(minutes=minute),
        observed_at=T0 + timedelta(minutes=minute),
        state=state,
        confidence=0.9 if state is CrossingState.BLOCKED else 0.85,
        reason="test",
        object_key="frames/x.jpg",
        detector_version="test/1",
    )


def drive(
    processor: Processor,
    observations: list[ObservationRecord],
    caught_up: bool = True,
    now: datetime | None = None,
    already_published: bool = False,
) -> tuple[list[BlockageSession], list[Alert]]:
    """Feed observations in event order; collect all emissions."""
    sessions: list[BlockageSession] = []
    alerts: list[Alert] = []
    for o in sorted(observations, key=lambda o: o.captured_at):
        emitted, alert = processor.observe(o, already_published, caught_up, now or o.captured_at)
        sessions.extend(emitted)
        if alert is not None:
            alerts.append(alert)
    return sessions, alerts


def test_processor_closed_sessions_match_oracle() -> None:
    """A blockage, a gap, another blockage - swept well after the last frame."""
    observations = (
        [obs(m, CrossingState.BLOCKED) for m in range(0, 30, 3)]
        + [obs(50 + m, CrossingState.BLOCKED) for m in range(0, 12, 3)]
        + [obs(20.5, CrossingState.UNKNOWN), obs(41, CrossingState.CLEAR)]
    )
    processor = Processor()
    sessions, _ = drive(processor, observations)
    sessions += processor.sweep(now=T0 + timedelta(hours=2))

    closed = sorted(
        (s.model_dump() for s in sessions if not s.is_open), key=lambda s: s["started_at"]
    )
    oracle = [s.model_dump() for s in derive_sessions(observations)]
    assert closed == oracle


def test_sweep_waits_out_the_drift_margin() -> None:
    """The deadline alone is not enough: two cameras drift up to two minutes
    apart, so a close inside the margin could still be overtaken by a frame
    already captured. One second past the margin, the silence is real."""
    processor = Processor()
    drive(processor, [obs(m, CrossingState.BLOCKED) for m in range(0, 9, 3)])
    deadline = T0 + timedelta(minutes=6 + 15, milliseconds=1)

    assert processor.sweep(now=deadline + OUT_OF_ORDERNESS - timedelta(seconds=1)) == []
    swept = processor.sweep(now=deadline + OUT_OF_ORDERNESS + timedelta(seconds=1))
    assert [s.is_open for s in swept] == [False]


def test_replay_rebuilds_an_open_session_with_its_identity() -> None:
    """The restart property. Kill the host mid-session, replay everything into
    a fresh Processor: the open session keeps started_at and session_id, so
    'blocked since N o'clock' survives a deploy."""
    observations = [obs(m, CrossingState.BLOCKED) for m in range(0, 15, 3)]
    first = Processor()
    before, _ = drive(first, observations)

    replayed = Processor()
    after, _ = drive(replayed, observations)

    assert before and after
    assert after[-1].is_open
    assert after[-1].session_id == before[-1].session_id
    assert after[-1].started_at == observations[0].captured_at


def test_replayed_history_does_not_realert_but_a_fresh_edge_does() -> None:
    """Boot replay walks months of edges; none may page. But an edge inside
    the freshness window - a train that arrived during the restart - must."""
    stale = [obs(m, CrossingState.BLOCKED) for m in (0, 3)]
    now = T0 + timedelta(hours=6)

    processor = Processor()
    _, alerts = drive(processor, stale, caught_up=False, now=now)
    assert alerts == []

    fresh_minute = (now - T0).total_seconds() / 60 - ALERT_FRESHNESS.total_seconds() / 60 / 2
    fresh = [
        obs(fresh_minute + m, CrossingState.BLOCKED, crossing="SE_8TH_DIVISION") for m in (0, 3)
    ]
    _, alerts = drive(processor, fresh, caught_up=False, now=now)
    assert [a.crossing_id for a in alerts] == ["SE_8TH_DIVISION"]


def test_the_first_sweep_drops_closes_the_previous_life_outlived() -> None:
    """A boot inherits a deadline for every crossing the replay touched, and a
    crossing quiet since this morning was closed this morning - by whoever was
    running then. Re-announcing it would put back a row a backfill may since
    have superseded, which is the whole point of the published boundary.

    The mark is the newest event time the replay carried: the previous life ran
    at least that far, so it had passed the quiet crossing's deadline and had
    not reached the deadline of the run still blocked when the log ends. Only
    the second close is this life's to make - the state behind the first is
    still cleared, or the sweep would retry it forever."""
    quiet = [obs(m, CrossingState.BLOCKED) for m in range(0, 9, 3)]
    blocked_at_shutdown = [
        obs(240 + m, CrossingState.BLOCKED, crossing="SE_8TH_DIVISION") for m in range(0, 9, 3)
    ]

    processor = Processor()
    emitted, alerts = drive(processor, quiet + blocked_at_shutdown, already_published=True)
    assert (emitted, alerts) == ([], []), "the replay itself announces nothing"

    swept = processor.sweep(now=T0 + timedelta(hours=8))

    assert [(s.crossing_id, s.is_open) for s in swept] == [("SE_8TH_DIVISION", False)]
    assert processor.deadlines == {}, "both deadlines fired, announced or not"
    assert processor.open_count == 0, "the silent close still cleared its state"


def test_sweeps_after_the_first_announce_unconditionally() -> None:
    """The mark describes the deadlines a boot inherited and nothing else. A
    session this life saw from first frame to close is its own to announce even
    when its event times sit behind the mark - a camera whose clock lags, or a
    frame delivered late - because nobody else ever held it."""
    replayed = [obs(240 + m, CrossingState.BLOCKED) for m in range(0, 9, 3)]
    processor = Processor()
    drive(processor, replayed, already_published=True)
    processor.sweep(now=T0 + timedelta(hours=8))

    behind_the_mark = [
        obs(100 + m, CrossingState.BLOCKED, crossing="SE_8TH_DIVISION") for m in range(0, 9, 3)
    ]
    drive(processor, behind_the_mark)

    swept = processor.sweep(now=T0 + timedelta(hours=8))
    assert [(s.crossing_id, s.is_open) for s in swept] == [("SE_8TH_DIVISION", False)]


def test_the_next_train_does_not_reclose_a_session_the_boot_inherited() -> None:
    """The sweep is not the only thing that closes a run. A BLOCKED arriving
    past the gap closes the one before it right there in the record path, and
    that path runs during the drain - before any sweep could have retired the
    deadline quietly.

    So: a train at 06:00, closed and announced by the life that saw it, then
    two hours of CLEAR frames, then the pod dies. It comes back to find a train
    that blocked at 09:00 waiting above the boundary. That record must announce
    its own session and nothing else; re-announcing the 06:00 close would put
    back a row a backfill may have superseded during the downtime."""
    old_train = [obs(m, CrossingState.BLOCKED) for m in range(0, 9, 3)]
    then_quiet = [obs(m, CrossingState.CLEAR) for m in range(9, 123, 3)]
    next_train = [obs(180 + m, CrossingState.BLOCKED) for m in range(0, 9, 3)]

    processor = Processor()
    drive(processor, old_train + then_quiet, already_published=True)
    emitted, _ = drive(processor, next_train)

    assert all(s.is_open for s in emitted), "no close may be re-announced"
    assert {s.started_at for s in emitted} == {next_train[0].captured_at}
    assert emitted[0].started_at == next_train[0].captured_at


def test_the_next_train_does_close_a_session_that_was_open_at_shutdown() -> None:
    """The other half of the same rule. Here the pod died while the train was
    still standing on the crossing, so its deadline fell after everything the
    replay carries and nobody ever announced that close. The next train's first
    frame is what finally supersedes it, and the close must go out - dropping
    it would leave the row open forever."""
    open_at_shutdown = [obs(m, CrossingState.BLOCKED) for m in range(0, 9, 3)]
    next_train = [obs(180 + m, CrossingState.BLOCKED) for m in range(0, 9, 3)]

    processor = Processor()
    drive(processor, open_at_shutdown, already_published=True)
    emitted, _ = drive(processor, next_train)

    assert [s.is_open for s in emitted][:1] == [False], "the inherited close goes out first"
    assert emitted[0].started_at == open_at_shutdown[0].captured_at
    assert all(s.is_open for s in emitted[1:]), "then the new train's own upserts"


def outage_backlog() -> tuple[list[ObservationRecord], list[ObservationRecord]]:
    """A train that blocked and cleared entirely while the pod was down, and
    published history that ends *after* it.

    The mark is a max over event times, and the topic is partitioned by
    crossing: a busy crossing's frames stay published right up to the crash
    while a quieter one's whole blockage sits unread behind them. So the
    backlog's train is older than the mark even though nobody ever saw it.
    """
    published = [obs(30 + m, CrossingState.BLOCKED) for m in range(0, 9, 3)]
    buried = [obs(m, CrossingState.BLOCKED, crossing="SE_POWELL") for m in range(0, 9, 3)]
    return published, buried


def test_the_sweep_closes_a_session_the_previous_life_never_saw() -> None:
    """The mark proves what the previous life announced only for runs it read.
    This train arrived above the published boundary, so no earlier life ever
    held it, and its close is this life's to make however far behind the mark
    its deadline falls. Dropping it would leave the row open forever - missing
    from analytics, pinned to the live board as blocked."""
    published, buried = outage_backlog()

    processor = Processor()
    drive(processor, published, already_published=True)
    opened, _ = drive(processor, buried)
    swept = processor.sweep(now=T0 + timedelta(hours=8))

    assert {(s.crossing_id, s.is_open) for s in opened} == {("SE_POWELL", True)}
    assert ("SE_POWELL", False) in [(s.crossing_id, s.is_open) for s in swept]
    assert processor.open_count == 0


def test_the_next_train_closes_a_session_the_previous_life_never_saw() -> None:
    """The same rule on the record path: the backlog's train is superseded by a
    later one before any sweep judges its deadline, and that close must go out
    for the same reason."""
    published, buried = outage_backlog()
    next_train = [obs(120 + m, CrossingState.BLOCKED, crossing="SE_POWELL") for m in range(0, 9, 3)]

    processor = Processor()
    drive(processor, published, already_published=True)
    drive(processor, buried)
    emitted, _ = drive(processor, next_train)

    assert [s.is_open for s in emitted][:1] == [False]
    assert emitted[0].started_at == buried[0].captured_at
    assert all(s.is_open for s in emitted[1:])


def test_the_deployments_argv_reaches_the_run_command() -> None:
    """The container's argv is a contract between the manifest and this CLI. A
    single-command typer app collapses into the root command, and then the
    `run` the deployment passes is an unexpected extra argument (exit 2) - the
    pod CrashLoopBackOffs before it opens a socket. Exit 1 with the missing-
    bootstrap complaint is `run`'s own body talking, which is the proof."""
    manifest = (REPO_ROOT / "deploy/sessionizer/deployment.yaml").read_text()
    deployment = next(
        doc for doc in yaml.safe_load_all(manifest) if doc.get("kind") == "Deployment"
    )
    container = next(
        c
        for c in deployment["spec"]["template"]["spec"]["containers"]
        if c["name"] == "sessionizer"
    )

    result = CliRunner().invoke(app, container["args"], env={"BLOCKADE_KAFKA_BOOTSTRAP": ""})

    assert result.exit_code == 1, f"argv {container['args']} did not reach `run`: {result.output}"
    assert "BLOCKADE_KAFKA_BOOTSTRAP" in result.output


TOPIC = "crossing.observations.v1"


@dataclass
class FakeMessage:
    value: bytes
    offset: int = 0
    topic: str = TOPIC
    partition: int = 0


def messages(observations: list[ObservationRecord], first_offset: int = 0) -> list[FakeMessage]:
    return [
        FakeMessage(o.model_dump_json().encode(), offset)
        for offset, o in enumerate(observations, start=first_offset)
    ]


class FakeTailer:
    """Reaches the head *inside* a batch, exactly like the real one.

    ``TopicTailer.get_batch`` advances its positions as it returns records, so
    the batch that crosses the boot end offsets already reports ``caught_up``
    by the time it lands - while every record in it is still replayed history.
    """

    def __init__(self, batches: list[list[FakeMessage]], caught_up: bool = True) -> None:
        self._batches = batches
        self.caught_up = caught_up

    async def get_batch(self, timeout_ms: int = 1000):  # noqa: ARG002
        # The real tailer awaits the broker; without this yield the loop under
        # test would spin without ever letting the stop task run.
        await asyncio.sleep(0)
        batch = self._batches.pop(0) if self._batches else []
        self.caught_up = True
        return batch


class DyingTailer(FakeTailer):
    """Hands over its backlog and the process dies - no empty poll, ever.

    The OOMKill in the middle of a drain. The loop exits after the last batch it
    handed back rather than going round again to find the head.
    """

    def __init__(self, batches: list[list[FakeMessage]], stop: asyncio.Event) -> None:
        super().__init__(batches, caught_up=False)
        self._stop = stop

    async def get_batch(self, timeout_ms: int = 1000):
        batch = await super().get_batch(timeout_ms)
        if not self._batches:
            self._stop.set()
        return batch


class FakeProducer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, bytes]] = []

    async def send(self, topic: str, key: str, value: bytes):
        self.sent.append((topic, key, value))
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        fut.set_result(None)
        return fut


class FakeProgress:
    """The published-offset boundary. ``GroupProgress`` and its derivation from
    committed offsets are pinned in test_group_progress.py; here it is just the
    line the loop is expected to respect, plus where it chose to move it.

    ``_boundary`` is fixed for a life, as the real one's is - it is read once at
    boot. ``committed`` is what the next life would inherit.
    """

    def __init__(self, boundary: int = 0) -> None:
        self._boundary = boundary
        self.committed: int | None = None
        self._noted: int | None = None

    def published(self, record: FakeMessage) -> bool:
        return record.offset < self._boundary

    def advance(self, records: list[FakeMessage]) -> None:
        for record in records:
            self._noted = max(self._noted or 0, record.offset + 1)

    async def commit(self) -> None:
        if self._noted is not None:
            self.committed = self._noted


async def drain(
    tailer: FakeTailer, producer: FakeProducer, progress: FakeProgress, settings: Settings
) -> None:
    stop = asyncio.Event()

    async def stop_soon():
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(
        run_loop(tailer, producer, Processor(), progress, settings, stop),  # type: ignore[arg-type]
        stop_soon(),
    )


def produced(producer: FakeProducer, topic: str) -> list[dict]:
    return [json.loads(value) for sent_to, _, value in producer.sent if sent_to == topic]


@pytest.mark.asyncio
async def test_loop_produces_sessions_keyed_by_session_id_and_skips_poison() -> None:
    """One qualified session flows out keyed for compaction; a poison message
    is logged and skipped rather than wedging the loop; and the empty poll at
    the head sweeps the long-stale deadline closed - these observations are
    days old against wall clock, exactly like a boot replay."""
    batch = messages([obs(m, CrossingState.BLOCKED) for m in range(0, 9, 3)])
    batch.insert(1, FakeMessage(b"not json", offset=99))

    producer = FakeProducer()
    settings = Settings(kafka_bootstrap="test:9092")
    await drain(FakeTailer([batch, []]), producer, FakeProgress(), settings)

    session_records = [s for s in producer.sent if s[0] == settings.kafka_sessions_topic]
    assert session_records, "a qualified session must be produced"
    for _, key, value in session_records:
        assert json.loads(value)["session_id"] == key
    emissions = [json.loads(v)["is_open"] for _, _, v in session_records]
    assert emissions[0] is True, "the session is announced open as soon as it qualifies"
    assert emissions[-1] is False, "the head-of-topic sweep closes the stale session"


@pytest.mark.asyncio
async def test_the_batch_that_crosses_the_head_is_still_replay() -> None:
    """The last replayed batch must not page. Both crossings below carry an
    equally ancient rising edge; the only difference is that one arrived in the
    batch that crossed the head and the other after it. Judging freshness
    against the flag as it reads *after* the fetch would announce a train from
    days ago to everyone holding a pager."""
    replayed = [obs(m, CrossingState.BLOCKED) for m in (0, 3)]
    live = [obs(m, CrossingState.BLOCKED, crossing="SE_8TH_DIVISION") for m in (0, 3)]
    tailer = FakeTailer(
        [messages(replayed), messages(live, first_offset=len(replayed))], caught_up=False
    )

    producer = FakeProducer()
    settings = Settings(kafka_bootstrap="test:9092")
    await drain(tailer, producer, FakeProgress(), settings)

    alerted = [a["crossing_id"] for a in produced(producer, settings.kafka_alerts_topic)]
    assert alerted == ["SE_8TH_DIVISION"]


@pytest.mark.asyncio
async def test_records_below_the_boundary_build_state_but_publish_nothing() -> None:
    """The backfill guard. Everything up to the committed offset was already
    published in a previous life, and republishing it would restore sessions a
    re-scored window deliberately deleted - so those records feed the
    sessionizer and emit nothing. State is still theirs: the one record past
    the boundary announces a session that started five observations earlier,
    with the started_at only the replayed records could supply."""
    blocked = [obs(m, CrossingState.BLOCKED) for m in range(0, 15, 3)]
    producer = FakeProducer()
    settings = Settings(kafka_bootstrap="test:9092")

    progress = FakeProgress(boundary=len(blocked) - 1)
    await drain(FakeTailer([messages(blocked), []]), producer, progress, settings)

    sessions = produced(producer, settings.kafka_sessions_topic)
    assert [s["is_open"] for s in sessions] == [True, False], (
        "only the record at the boundary announces, and the head sweep closes it"
    )
    assert datetime.fromisoformat(sessions[0]["started_at"]) == blocked[0].captured_at
    assert sessions[0]["duration_seconds"] == 12 * 60
    assert progress.committed == len(blocked), "the boundary advances past what was acked"


@pytest.mark.asyncio
async def test_a_fresh_group_republishes_no_history() -> None:
    """First deployment against a topic full of retained observations. Someone
    published those sessions already - the boundary starts at the head, so the
    replay rebuilds state in silence and pages nobody. The sweep it inherits is
    held to the same line: the crossing that went quiet four hours before the
    log ends was closed and announced back then, and only the run still blocked
    at the head reaches the bus."""
    quiet = [obs(m, CrossingState.BLOCKED) for m in range(0, 9, 3)]
    blocked_at_shutdown = [
        obs(240 + m, CrossingState.BLOCKED, crossing="SE_8TH_DIVISION") for m in range(0, 9, 3)
    ]
    records = messages(quiet + blocked_at_shutdown)
    producer = FakeProducer()
    settings = Settings(kafka_bootstrap="test:9092")

    await drain(FakeTailer([records, []]), producer, FakeProgress(boundary=len(records)), settings)

    assert produced(producer, settings.kafka_alerts_topic) == []
    sessions = produced(producer, settings.kafka_sessions_topic)
    assert [(s["crossing_id"], s["is_open"]) for s in sessions] == [("SE_8TH_DIVISION", False)]


@pytest.mark.asyncio
async def test_a_life_that_dies_mid_drain_leaves_its_work_to_be_redone() -> None:
    """What the committed offset has to mean, and why it moves only at the head.

    Life 1 boots into a three-hour backlog: it announces the train as open, arms
    its gap deadline, works on through the CLEAR frames that follow - and is
    killed before the poll that would ever judge that deadline. It committed
    nothing, because it finished nothing.

    That matters because the first sweep of the next boot reads the boundary as
    'everything below this was published, closes included'. Had life 1 committed
    as it drained, life 2 would see a deadline older than the newest replayed
    frame, believe the close was announced, and drop it - and the session would
    read open forever, missing from analytics and pinned to the live board.
    Redoing life 1's work costs duplicates, which the session_id absorbs."""
    train = [obs(m, CrossingState.BLOCKED) for m in range(0, 9, 3)]
    afterwards = [obs(m, CrossingState.CLEAR) for m in range(9, 93, 3)]
    backlog = messages(train + afterwards)
    settings = Settings(kafka_bootstrap="test:9092")

    life1 = FakeProgress()
    doomed = FakeProducer()
    killed = asyncio.Event()
    await run_loop(
        DyingTailer([backlog[:4], backlog[4:]], killed),  # type: ignore[arg-type]
        doomed,  # type: ignore[arg-type]
        Processor(),
        life1,  # type: ignore[arg-type]
        settings,
        killed,
    )
    opens = produced(doomed, settings.kafka_sessions_topic)
    assert opens and all(s["is_open"] for s in opens), "life 1 announced only opens"

    life2 = FakeProgress(boundary=life1.committed or 0)
    reborn = FakeProducer()
    await drain(FakeTailer([backlog, []]), reborn, life2, settings)

    sessions = produced(reborn, settings.kafka_sessions_topic)
    assert sessions[-1]["is_open"] is False, "the close life 1 never made"
    assert all(s["is_open"] for s in sessions[:-1])
    assert life1.committed is None, "a drain that never reached the head finished nothing"
    assert life2.committed == len(backlog)
