"""The sessionizer service host, against the batch oracle and its own clock rules.

The cores (StreamingSessionizer, RisingEdgeAlerter) are tested elsewhere; what
this file pins is the hosting discipline that used to be Flink's job:

- the Processor's closed sessions still equal the batch oracle's,
- gap deadlines fire only after the out-of-orderness margin has passed,
- a restart (fresh Processor, full replay) rebuilds an open session with its
  original started_at and session_id - the property the Flink deployment's
  stateless upgrades did NOT have,
- replayed history never re-alerts, but an edge fresh enough to matter does,
- the loop produces sessions keyed by session_id and survives poison messages.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from blockade.alerts import Alert
from blockade.config import Settings
from blockade.schemas import BlockageSession, CrossingState, ObservationRecord
from blockade.sessions import derive_sessions
from sessionizer.runner import ALERT_FRESHNESS, OUT_OF_ORDERNESS, Processor, run_loop

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
) -> tuple[list[BlockageSession], list[Alert]]:
    """Feed observations in event order; collect all emissions."""
    sessions: list[BlockageSession] = []
    alerts: list[Alert] = []
    for o in sorted(observations, key=lambda o: o.captured_at):
        emitted, alert = processor.observe(o, caught_up, now or o.captured_at)
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
    deadline = T0 + timedelta(minutes=6 + 10, milliseconds=1)

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
    fresh = [obs(fresh_minute + m, CrossingState.BLOCKED, crossing="SE_8TH_DIVISION")
             for m in (0, 3)]
    _, alerts = drive(processor, fresh, caught_up=False, now=now)
    assert [a.crossing_id for a in alerts] == ["SE_8TH_DIVISION"]


@dataclass
class FakeMessage:
    value: bytes
    partition: int = 0
    offset: int = 0


class FakeTailer:
    def __init__(self, batches: list[list[FakeMessage]]) -> None:
        self._batches = batches
        self.caught_up = True

    async def get_batch(self, timeout_ms: int = 1000):  # noqa: ARG002
        # The real tailer awaits the broker; without this yield the loop under
        # test would spin without ever letting the stop task run.
        await asyncio.sleep(0)
        return self._batches.pop(0) if self._batches else []


class FakeProducer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, bytes]] = []

    async def send(self, topic: str, key: str, value: bytes):
        self.sent.append((topic, key, value))
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        fut.set_result(None)
        return fut


@pytest.mark.asyncio
async def test_loop_produces_sessions_keyed_by_session_id_and_skips_poison() -> None:
    """One qualified session flows out keyed for compaction; a poison message
    is logged and skipped rather than wedging the loop; and the empty poll at
    the head sweeps the long-stale deadline closed - these observations are
    days old against wall clock, exactly like a boot replay."""
    blocked = [obs(m, CrossingState.BLOCKED) for m in range(0, 9, 3)]
    messages = [FakeMessage(o.model_dump_json().encode()) for o in blocked]
    messages.insert(1, FakeMessage(b"not json"))

    tailer = FakeTailer([messages, []])
    producer = FakeProducer()
    stop = asyncio.Event()

    async def stop_soon():
        await asyncio.sleep(0.05)
        stop.set()

    settings = Settings(kafka_bootstrap="test:9092")
    await asyncio.gather(
        run_loop(tailer, producer, Processor(), settings, stop),  # type: ignore[arg-type]
        stop_soon(),
    )

    session_records = [s for s in producer.sent if s[0] == settings.kafka_sessions_topic]
    assert session_records, "a qualified session must be produced"
    for _, key, value in session_records:
        assert json.loads(value)["session_id"] == key
    emissions = [json.loads(v)["is_open"] for _, _, v in session_records]
    assert emissions[0] is True, "the session is announced open as soon as it qualifies"
    assert emissions[-1] is False, "the head-of-topic sweep closes the stale session"
