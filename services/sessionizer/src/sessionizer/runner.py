"""Sessionizer service: observations in, sessions and alerts out.

The decisions live in blockade-core - ``StreamingSessionizer`` and
``RisingEdgeAlerter``, unit-tested and diffed against the batch oracle. This
module hosts them on a plain Kafka tail, replacing the Flink job that used to
host the same two classes at the cost of an operator, a JVM, and 2.5Gi of
memory for three keys of state.

Recovery is replay. The state is a pure function of the observations log, so
every boot re-reads the topic from the beginning (groupless - the same
``TopicTailer`` pattern the API uses) and rebuilds open sessions with their
original ``started_at``. That is strictly better than the Flink deployment it
replaces, whose stateless upgrade mode discarded state on every deploy and
split any session that spanned it. Session emissions are idempotent
(deterministic session_id, compacted topic, upserts downstream), so replay
converges rather than duplicating.

Replaying is not republishing, though. Batch owns history: a re-scored window
is loaded by deleting the sessions it supersedes and inserting the new
derivation, so a boot that re-announced every session it re-derived would put
the superseded ones straight back. So a ``GroupProgress`` bookkeeper carries a
committed offset alongside the tail - not to decide what to read, which is
always everything, but to mark what this service has already published.
Records below it rebuild state in silence; records at or past it - which is
exactly what arrived while the pod was down - emit as usual, so an outage's
closes and alerts are not lost.

The gap deadlines the replay rebuilds need the same line drawn, and a record
offset cannot draw it alone: a session closes on silence, so its close belongs
to no record. The commit point supplies the other half. Offsets move only on
the empty poll at the head of the topic, after that poll's sweep and every
emission it produced are acked - never mid-drain. So a committed offset does
not just say "these records were read", it says "at some wall clock later than
every event in them, this service stood at the head and fired every deadline
then due". Event time turns that into a test: the newest observation below the
boundary is a lower bound on that wall clock, so a deadline this boot inherited
that fell before it was announced back then and is retired silently here -
whether the sweep is what fires it or a later train supersedes it in the record
path. A deadline beyond the mark is the session that was open when the pod died
and is this life's to announce - and so is one whose run any record from above
the boundary joined, however old that run looks: a train that blocked and
cleared entirely during the outage is behind the mark on event time but was
never seen by the life the mark describes.

A life that dies mid-drain therefore commits nothing, and its successor replays
from the same line and re-emits everything it emitted - duplicates, which are
idempotent, rather than a close nobody ever made and a row left open forever.
The residual runs the other way: a deadline that came due between the newest
replayed observation and the commit itself can be announced twice. Nothing else
can have claimed that window - a backfill refuses to come within a session gap
of now - so the duplicate lands on its own row and idempotently.

Two timing rules replace the watermark:

- **Close on silence only when silence is provable.** A gap deadline fires
  only when wall clock has passed it by the out-of-orderness allowance AND
  the last poll came back empty (the consumer is at the head of the topic).
  Wall clock alone would close sessions early while draining a backlog whose
  event times lag; head-of-topic alone would never close a quiet night.
- **Replayed history must not re-alert.** During boot replay, a rising edge
  is produced only if it is fresher than the alert freshness window. This sits
  on top of the published-offset boundary and catches what that cannot: after
  a long outage every buried edge is unpublished and past the boundary, and
  only the recent one is still worth a page.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
from datetime import UTC, datetime, timedelta

import typer
from blockade.alerts import Alert, RisingEdgeAlerter
from blockade.bus import GroupProgress, RecordProducer, TopicTailer
from blockade.config import Settings, get_settings
from blockade.schemas import BlockageSession, ObservationRecord
from blockade.stream_sessions import SessionizerState, StreamingSessionizer, to_ms
from prometheus_client import Counter, Gauge, start_http_server

log = logging.getLogger(__name__)

app = typer.Typer(help="Sessionizing: observations to sessions and alerts.", no_args_is_help=True)

OUT_OF_ORDERNESS = timedelta(minutes=2)
"""The two cameras of a crossing drift up to this far apart, so an event this
much older than the newest one seen may still be in flight. Same bound the
Flink watermark used; a deadline is not judged passed until wall clock clears
it by this much."""

OUT_OF_ORDERNESS_MS = int(OUT_OF_ORDERNESS.total_seconds() * 1000)
"""The same margin in the sweep's own unit - one spelling, both retirement paths."""

ALERT_FRESHNESS = timedelta(minutes=10)
"""During boot replay, rising edges older than this stay silent. An alert for
last night is noise; one for a train that arrived while the pod was restarting
is exactly the point."""

EMITTED = Counter("blockade_sessionizer_emitted_total", "Records produced, by kind", ["kind"])
OPEN_SESSIONS = Gauge("blockade_sessionizer_open_sessions", "Crossings with an open session")
CAUGHT_UP = Gauge("blockade_sessionizer_caught_up", "1 once the boot replay has passed the head")


class Processor:
    """The decisions, Kafka-free: observations and clock ticks in, emissions out.

    Owns the per-crossing state the two cores need a host for. Pure enough to
    test against the batch oracle without a broker.
    """

    def __init__(self) -> None:
        self.sessionizer = StreamingSessionizer()
        self.alerter = RisingEdgeAlerter()
        self.states: dict[str, SessionizerState | None] = {}
        self.deadlines: dict[str, int] = {}
        self.replayed_through_ms: int | None = None
        """Newest event time among the records an earlier life had published -
        a lower bound on how far its wall clock ran. None if this life has
        replayed nothing, which is also the answer for a topic with no history
        behind it."""
        self._unpublished_runs: set[str] = set()
        """Crossings whose currently open run took in a record from at or past
        the published boundary. Nobody announced that run's close, whatever the
        mark says: the previous life never saw the run. Cleared when the run
        does."""
        self._inherits_deadlines = True

    def observe(
        self,
        obs: ObservationRecord,
        already_published: bool,
        caught_up: bool,
        now: datetime,
    ) -> tuple[list[BlockageSession], Alert | None]:
        inherited = self.deadlines.get(obs.crossing_id)
        # Both about the run as it stands *before* this record: it is that run
        # the record may be closing.
        inherited_run_was_published = obs.crossing_id not in self._unpublished_runs
        previous = self.states.get(obs.crossing_id)
        state, sessions, timer = self.sessionizer.observe(previous, obs)
        self.states[obs.crossing_id] = state
        if timer is not None:
            # Only the latest deadline matters; observe() ignores stale timers.
            self.deadlines[obs.crossing_id] = timer
            self._note_contribution(obs.crossing_id, previous, state, already_published)
        alert = self.alerter.observe(obs)
        if already_published:
            # State is rebuilt from this record; its consequences were announced
            # by whoever was running when it first arrived.
            at = to_ms(obs.captured_at)
            self.replayed_through_ms = max(self.replayed_through_ms or at, at)
            return [], None
        if alert is not None and not caught_up and now - alert.started_at > ALERT_FRESHNESS:
            alert = None
        if self._already_announced(inherited, inherited_run_was_published):
            # A BLOCKED arriving past the gap closes the run before it in the
            # record path rather than waiting for the timer. When that run is
            # one the boot inherited, its close was made by whoever held it.
            sessions = [s for s in sessions if s.is_open]
        return sessions, alert

    def _note_contribution(
        self,
        crossing_id: str,
        previous: SessionizerState | None,
        state: SessionizerState | None,
        already_published: bool,
    ) -> None:
        """Record where the run now open on this crossing came from.

        A BLOCKED past the gap ends the run before it and starts a fresh one in
        the same call, so a new run inherits nothing from the old one's origin.
        """
        started_a_new_run = previous is None or (
            state is not None and state.started_at_ms != previous.started_at_ms
        )
        if not already_published:
            self._unpublished_runs.add(crossing_id)
        elif started_a_new_run:
            self._unpublished_runs.discard(crossing_id)

    def sweep(self, now: datetime) -> list[BlockageSession]:
        """Fire every gap deadline that wall clock has safely passed.

        Callers invoke this only at the head of the topic; the out-of-orderness
        margin here covers camera drift, and head-of-topic covers backlog lag.
        """
        now_ms = to_ms(now)
        emissions: list[BlockageSession] = []
        for crossing_id, deadline in list(self.deadlines.items()):
            if now_ms < deadline + OUT_OF_ORDERNESS_MS:
                continue
            state, sessions = self.sessionizer.on_timer(self.states.get(crossing_id), deadline)
            self.states[crossing_id] = state
            del self.deadlines[crossing_id]
            announced = self._already_announced(deadline, crossing_id not in self._unpublished_runs)
            if state is None:
                self._unpublished_runs.discard(crossing_id)
            if announced:
                continue
            emissions.extend(sessions)
        self._inherits_deadlines = False
        return emissions

    def _already_announced(self, deadline_ms: int | None, run_was_published: bool) -> bool:
        """Did an earlier life provably close the run this deadline belongs to?

        A boot inherits a deadline for every crossing the replay touched, and
        most of them closed long ago - a crossing quiet since yesterday closed
        yesterday. The commit point is the proof: offsets move only at the head
        of the topic, after a sweep, so a committed offset says that life's
        wall clock had passed every deadline then due. ``replayed_through_ms``
        is a lower bound on that clock.

        That proof covers only a run the previous life actually saw, so it is
        offered only for runs built entirely from published records. A run any
        record from at or past the boundary contributed to is one this life
        assembled - a train that blocked and cleared while the pod was down, its
        event times still behind the newest published record on some other
        partition - and its close is this life's to make, however old it looks.

        Two paths retire an inherited deadline - the sweep, and a later BLOCKED
        arriving past the gap - and both ask here, so the rule cannot drift
        between them. It applies only until the first sweep of the boot: after
        that every deadline is this life's own.
        """
        if deadline_ms is None or not self._inherits_deadlines or not run_was_published:
            return False
        fires_at_ms = deadline_ms + OUT_OF_ORDERNESS_MS
        return self.replayed_through_ms is not None and fires_at_ms <= self.replayed_through_ms

    @property
    def open_count(self) -> int:
        return sum(1 for s in self.states.values() if s is not None)


def _alert_payload(alert: Alert) -> bytes:
    return json.dumps(
        {
            "crossing_id": alert.crossing_id,
            "started_at": alert.started_at.isoformat(),
            "confidence": alert.confidence,
            "reason": alert.reason,
        }
    ).encode()


async def run_loop(
    tailer: TopicTailer,
    producer: RecordProducer,
    processor: Processor,
    progress: GroupProgress,
    settings: Settings,
    stop: asyncio.Event,
) -> None:
    """Tail, decide, produce, and - at the head of the topic - commit.

    The tail is the whole log every boot; the group offset says how much of it
    this service has already published. What that means for a record is the
    Processor's to decide - both halves of its consequences are suppressed
    below the line, and so are the gap closes an earlier life already made.

    Progress is noted every batch but committed only on the empty poll, after
    that poll's sweep and every emission are acked. Committing mid-drain would
    push the line past records whose gap deadlines nothing had judged yet, and
    the next boot would take the line's word for closes that never happened.
    """
    while not stop.is_set():
        # Sampled before the fetch, because get_batch() advances the tailer's
        # positions as it returns records: the batch that crosses the boot end
        # offsets is the tail of the replay, and reading the flag afterwards
        # would call up to 500 records of history live.
        was_caught_up = tailer.caught_up
        batch = await tailer.get_batch(timeout_ms=1000)
        now = datetime.now(UTC)
        sessions: list[BlockageSession] = []
        alerts: list[Alert] = []
        for message in batch:
            try:
                obs = ObservationRecord.model_validate_json(message.value)
            except ValueError:
                log.error("unparseable observation at %s:%s", message.partition, message.offset)
                continue
            emitted, alert = processor.observe(obs, progress.published(message), was_caught_up, now)
            sessions.extend(emitted)
            if alert is not None:
                alerts.append(alert)
        # An empty poll means we are at the head right now: silence is real,
        # not a backlog, so gap deadlines may be judged against wall clock.
        at_head = not batch and was_caught_up
        if at_head:
            sessions.extend(processor.sweep(now))

        futures = [
            await producer.send(
                settings.kafka_sessions_topic, s.session_id, s.model_dump_json().encode()
            )
            for s in sessions
        ]
        futures += [
            await producer.send(settings.kafka_alerts_topic, a.crossing_id, _alert_payload(a))
            for a in alerts
        ]
        await RecordProducer.await_acks(futures)
        progress.advance(batch)
        if at_head:
            await progress.commit()
        EMITTED.labels("session").inc(len(sessions))
        EMITTED.labels("alert").inc(len(alerts))
        OPEN_SESSIONS.set(processor.open_count)
        CAUGHT_UP.set(1 if tailer.caught_up else 0)


async def _serve(settings: Settings) -> None:
    assert settings.kafka_bootstrap is not None
    tailer = TopicTailer(
        settings.kafka_bootstrap,
        settings.kafka_observations_topic,
        client_id="blockade-sessionizer",
    )
    progress = GroupProgress(
        settings.kafka_bootstrap,
        group_id="blockade-sessionizer",
        client_id="blockade-sessionizer-offsets",
    )
    producer = RecordProducer(settings.kafka_bootstrap, client_id="blockade-sessionizer")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    await tailer.start()
    await progress.start(tailer.boot_end_offsets)
    await producer.start()
    log.info(
        "replaying %s -> %s + %s",
        settings.kafka_observations_topic,
        settings.kafka_sessions_topic,
        settings.kafka_alerts_topic,
    )
    try:
        await run_loop(tailer, producer, Processor(), progress, settings, stop)
    finally:
        await producer.stop()
        await progress.stop()
        await tailer.stop()
    log.info("shutdown complete")


@app.callback()
def cli() -> None:
    """Keeps `run` a real subcommand: with a single command and no callback,
    typer collapses the app into the root command and `blockade-sessionizer
    run` - the argv the Dockerfile and the deployment both pass - fails with
    "unexpected extra argument", which is how the API's first pod died."""


@app.command()
def run() -> None:
    """Consume observations and publish sessions and alerts continuously.

    Crashes are allowed: state rebuilds by replay, emissions are idempotent,
    and Kubernetes is the retry loop.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    if not settings.kafka_bootstrap:
        typer.secho("BLOCKADE_KAFKA_BOOTSTRAP is not set.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    start_http_server(settings.metrics_port)
    asyncio.run(_serve(settings))


if __name__ == "__main__":
    app()
