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

Two timing rules replace the watermark:

- **Close on silence only when silence is provable.** A gap deadline fires
  only when wall clock has passed it by the out-of-orderness allowance AND
  the last poll came back empty (the consumer is at the head of the topic).
  Wall clock alone would close sessions early while draining a backlog whose
  event times lag; head-of-topic alone would never close a quiet night.
- **Replayed history must not re-alert.** During boot replay, a rising edge
  is produced only if it is fresher than the alert freshness window - so a
  restart never re-announces last week, but a train that arrived during the
  outage still gets its alert.
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
from blockade.bus import RecordProducer, TopicTailer
from blockade.config import Settings, get_settings
from blockade.schemas import BlockageSession, ObservationRecord
from blockade.stream_sessions import SessionizerState, StreamingSessionizer
from prometheus_client import Counter, Gauge, start_http_server

log = logging.getLogger(__name__)

app = typer.Typer(help="Sessionizing: observations to sessions and alerts.", no_args_is_help=True)

OUT_OF_ORDERNESS = timedelta(minutes=2)
"""The two cameras of a crossing drift up to this far apart, so an event this
much older than the newest one seen may still be in flight. Same bound the
Flink watermark used; a deadline is not judged passed until wall clock clears
it by this much."""

ALERT_FRESHNESS = timedelta(minutes=10)
"""During boot replay, rising edges older than this stay silent. An alert for
last night is noise; one for a train that arrived while the pod was restarting
is exactly the point."""

EMITTED = Counter(
    "blockade_sessionizer_emitted_total", "Records produced, by kind", ["kind"]
)
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

    def observe(
        self, obs: ObservationRecord, caught_up: bool, now: datetime
    ) -> tuple[list[BlockageSession], Alert | None]:
        state, sessions, timer = self.sessionizer.observe(self.states.get(obs.crossing_id), obs)
        self.states[obs.crossing_id] = state
        if timer is not None:
            # Only the latest deadline matters; observe() ignores stale timers.
            self.deadlines[obs.crossing_id] = timer
        alert = self.alerter.observe(obs)
        if alert is not None and not caught_up and now - alert.started_at > ALERT_FRESHNESS:
            alert = None
        return sessions, alert

    def sweep(self, now: datetime) -> list[BlockageSession]:
        """Fire every gap deadline that wall clock has safely passed.

        Callers invoke this only at the head of the topic; the out-of-orderness
        margin here covers camera drift, and head-of-topic covers backlog lag.
        """
        now_ms = int(now.timestamp() * 1000)
        margin_ms = int(OUT_OF_ORDERNESS.total_seconds() * 1000)
        emissions: list[BlockageSession] = []
        for crossing_id, deadline in list(self.deadlines.items()):
            if now_ms >= deadline + margin_ms:
                state, sessions = self.sessionizer.on_timer(self.states.get(crossing_id), deadline)
                self.states[crossing_id] = state
                del self.deadlines[crossing_id]
                emissions.extend(sessions)
        return emissions

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
    settings: Settings,
    stop: asyncio.Event,
) -> None:
    """Tail, decide, produce, repeat. No offset commits: replay is the recovery."""
    while not stop.is_set():
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
            emitted, alert = processor.observe(obs, tailer.caught_up, now)
            sessions.extend(emitted)
            if alert is not None:
                alerts.append(alert)
        # An empty poll means we are at the head right now: silence is real,
        # not a backlog, so gap deadlines may be judged against wall clock.
        if not batch and tailer.caught_up:
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
        EMITTED.labels("session").inc(len(sessions))
        EMITTED.labels("alert").inc(len(alerts))
        OPEN_SESSIONS.set(processor.open_count)
        CAUGHT_UP.set(1 if tailer.caught_up else 0)


async def _serve(settings: Settings) -> None:
    tailer = TopicTailer(
        settings.kafka_bootstrap or "",
        settings.kafka_observations_topic,
        client_id="blockade-sessionizer",
    )
    producer = RecordProducer(settings.kafka_bootstrap or "", client_id="blockade-sessionizer")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    await tailer.start()
    await producer.start()
    log.info(
        "replaying %s -> %s + %s",
        settings.kafka_observations_topic,
        settings.kafka_sessions_topic,
        settings.kafka_alerts_topic,
    )
    try:
        await run_loop(tailer, producer, Processor(), settings, stop)
    finally:
        await producer.stop()
        await tailer.stop()
    log.info("shutdown complete")


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
