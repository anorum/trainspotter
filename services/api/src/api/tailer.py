"""Feeds the live-state reducer from the bus.

Two groupless tailers (sessions from the beginning - compaction makes that the
full history; observations from the beginning of retention), one shared
reducer, one change-notification queue per SSE subscriber. Decisions live in
blockade.api.state; this file only moves records.

Fail-fast on purpose: an unhandled tailer error exits the process, kubelet
restarts the pod, and the replay rebuilds the world. A silently dead tailer
serving a frozen board is the one failure mode this must never have.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from blockade.api.state import LiveState
from blockade.bus import TopicTailer
from blockade.config import Settings
from blockade.schemas import BlockageSession, FrameRecord, ObservationRecord

log = logging.getLogger(__name__)


class StateFeed:
    """Owns the reducer, the tail tasks, and the SSE fan-out."""

    def __init__(self, settings: Settings, state: LiveState) -> None:
        self.state = state
        self._settings = settings
        assert settings.kafka_bootstrap is not None
        self._sessions_tail = TopicTailer(
            settings.kafka_bootstrap, settings.kafka_sessions_topic, "blockade-api-sessions"
        )
        self._observations_tail = TopicTailer(
            settings.kafka_bootstrap, settings.kafka_observations_topic, "blockade-api-obs"
        )
        # Poll outcomes, so the board can blame stale pictures correctly:
        # ODOT down, ODOT frozen, or our own capture gone quiet.
        self._frames_tail = TopicTailer(
            settings.kafka_bootstrap, settings.kafka_frames_topic, "blockade-api-frames"
        )
        self._tasks: list[asyncio.Task] = []
        self._subscribers: set[asyncio.Queue[None]] = set()

    async def start(self) -> None:
        # Independent brokers-and-offsets handshakes; startup pays the max,
        # not the sum, and readiness gates on both regardless.
        await asyncio.gather(
            self._sessions_tail.start(),
            self._observations_tail.start(),
            self._frames_tail.start(),
        )
        self._tasks = [
            asyncio.create_task(self._run(self._sessions_tail, self._apply_session)),
            asyncio.create_task(self._run(self._observations_tail, self._apply_observation)),
            asyncio.create_task(self._run(self._frames_tail, self._apply_frame)),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._sessions_tail.stop()
        await self._observations_tail.stop()
        await self._frames_tail.stop()

    @property
    def ready(self) -> bool:
        """Readiness: both tails past their boot-time end offsets, so a fresh
        pod never serves a half-rebuilt board."""
        return (
            self._sessions_tail.caught_up
            and self._observations_tail.caught_up
            and self._frames_tail.caught_up
        )

    def subscribe(self) -> asyncio.Queue[None]:
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=2)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[None]) -> None:
        self._subscribers.discard(queue)

    # ------------------------------------------------------------ internals

    async def _run(self, tail: TopicTailer, apply) -> None:
        try:
            while True:
                for record in await tail.get_batch():
                    if record.value is None:
                        continue
                    try:
                        apply(record.value)
                    except ValueError:
                        # A poison record must not stall the board; it stays in
                        # the topic for inspection, same policy as the detector.
                        log.error("unparseable record at %s:%s", record.partition, record.offset)
                if self.state.changed:
                    self.state.changed = False
                    self._notify()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("tailer failed; exiting so the pod restarts and replays")
            os._exit(1)

    def _apply_session(self, value: bytes) -> None:
        self.state.apply_session(BlockageSession.model_validate_json(value))

    def _apply_observation(self, value: bytes) -> None:
        self.state.apply_observation(ObservationRecord.model_validate_json(value))

    def _apply_frame(self, value: bytes) -> None:
        self.state.apply_frame(FrameRecord.model_validate_json(value))

    def _notify(self) -> None:
        for queue in self._subscribers:
            # A full queue means that subscriber already has a wake-up pending;
            # coalescing is the point.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)
