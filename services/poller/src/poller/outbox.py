"""The outbox publisher: tails the manifest and publishes to crossing.frames.v1.

The manifest is already an append-only log, so it *is* the outbox - no second
table, no dual write. The poll loop's transaction ends at the manifest append;
this task runs beside it and never in front of it, so a Kafka outage costs
publish latency and never a frame.

Delivery is at-least-once, and the order of operations is the entire design:

    read new manifest lines -> publish -> await broker acks -> advance position

A crash anywhere in that sequence re-publishes at most one batch. That is the
correct trade - a duplicate is absorbed downstream (observation identity is
(crossing_id, captured_at), session_id is deterministic), while a dropped frame
is unrecoverable because ODOT overwrites the image within the minute.

Replay is this same mechanism pointed backwards: delete or rewind a position
file and the corpus republishes from local manifests.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from blockade.bus import RecordProducer
from blockade.config import Settings
from blockade.schemas import FrameRecord
from prometheus_client import Counter, Gauge

log = logging.getLogger(__name__)

PUBLISHED = Counter(
    "blockade_outbox_published_total",
    "Manifest records published to Kafka",
    ["camera_id"],
)
SKIPPED = Counter(
    "blockade_outbox_skipped_total",
    "Manifest lines dropped as unparseable - each one is a bug somewhere",
    ["camera_id"],
)
BACKLOG_BYTES = Gauge(
    "blockade_outbox_backlog_bytes",
    "Manifest bytes not yet published; zero means the outbox is caught up",
    ["camera_id"],
)


class Position:
    """Per-camera progress marker: which manifest file, and how far into it.

    Stored as a small JSON file, written atomically (tmp then rename) so a
    crash cannot leave a torn marker. Losing one is safe - the camera's
    backlog republishes and downstream absorbs the duplicates - but it should
    take a disk failure, not a pod restart.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self.file: str = ""
        self.offset: int = 0
        try:
            state = json.loads(path.read_text())
            self.file, self.offset = state["file"], int(state["offset"])
        except FileNotFoundError:
            pass  # First run: start from the earliest manifest on disk.
        except (json.JSONDecodeError, KeyError, ValueError):
            log.warning("unreadable position file %s; republishing from the start", path)

    def advance(self, file: str, offset: int) -> None:
        self.file, self.offset = file, offset
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"file": file, "offset": offset}))
        tmp.replace(self._path)


class ManifestOutbox:
    """Drains manifest lines into Kafka, one position file per camera."""

    def __init__(
        self,
        settings: Settings,
        producer: RecordProducer | None = None,
        idle_delay: float = 5.0,
        max_batch: int = 500,
    ) -> None:
        self._manifest_root = settings.manifest_dir
        self._outbox_dir = settings.outbox_dir
        self._topic = settings.kafka_frames_topic
        self._producer = producer or RecordProducer(
            settings.kafka_bootstrap or "", client_id="blockade-outbox"
        )
        self._idle_delay = idle_delay
        # Position persists at least every max_batch records, so a crash during
        # a large backlog drain re-publishes one batch, not the whole backlog.
        self._max_batch = max_batch
        self._positions: dict[str, Position] = {}

    # -- file mechanics ------------------------------------------------------

    def _position(self, camera_id: str) -> Position:
        if camera_id not in self._positions:
            self._positions[camera_id] = Position(self._outbox_dir / f"{camera_id}.json")
        return self._positions[camera_id]

    def _pending_files(self, camera_dir: Path, pos: Position) -> list[Path]:
        """Manifest files at or after the position, oldest first.

        Hourly filenames (YYYY-MM-DD-HH.jsonl) sort lexicographically in time
        order, which is what makes "the next file" a simple string comparison.
        """
        files = sorted(p for p in camera_dir.glob("*.jsonl"))
        if not pos.file:
            return files
        return [p for p in files if p.name >= pos.file]

    @staticmethod
    def _complete_lines(path: Path, offset: int) -> tuple[list[bytes], int]:
        """New newline-terminated lines after ``offset``, and the new offset.

        Only complete lines are consumed. The writer appends a full line per
        call, but this reader can still catch a line mid-write; a fragment
        without its newline stays unread until the newline lands, so a torn
        read never becomes a torn publish.
        """
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
        end = data.rfind(b"\n")
        if end < 0:
            return [], offset
        return data[: end + 1].splitlines(), offset + end + 1

    # -- publishing ----------------------------------------------------------

    async def _publish_batch(self, camera_id: str, lines: list[bytes]) -> int:
        """Validate, send, and wait for every ack; returns the count actually
        published, which excludes skipped lines. The caller advances the
        position only after this returns - that ordering is at-least-once."""
        futures = []
        for line in lines:
            try:
                FrameRecord.model_validate_json(line)
            except ValueError:
                # A newline-terminated line that does not parse is not a torn
                # write - it is corruption, and publishing it would poison every
                # consumer. Skip it loudly; the manifest still holds the bytes.
                log.error("unparseable manifest line for %s: %.200s", camera_id, line)
                SKIPPED.labels(camera_id).inc()
                continue
            # The original manifest bytes go on the wire, not a re-serialization:
            # the topic carries exactly what the manifest holds, so replay from
            # either source is byte-identical.
            futures.append(await self._producer.send(self._topic, camera_id, line))
        await RecordProducer.await_acks(futures)
        PUBLISHED.labels(camera_id).inc(len(futures))
        return len(futures)

    async def _drain_camera(self, camera_dir: Path) -> int:
        camera_id = camera_dir.name
        pos = self._position(camera_id)
        published = 0
        for path in self._pending_files(camera_dir, pos):
            offset = pos.offset if path.name == pos.file else 0
            while True:
                lines, new_offset = self._complete_lines(path, offset)
                if not lines:
                    break
                for start in range(0, len(lines), self._max_batch):
                    batch = lines[start : start + self._max_batch]
                    published += await self._publish_batch(camera_id, batch)
                # Offsets are recomputed from byte positions rather than summed
                # from line lengths, so a final unterminated fragment is never
                # counted as consumed.
                pos.advance(path.name, new_offset)
                offset = new_offset
        backlog = sum(
            max(0, p.stat().st_size - (pos.offset if p.name == pos.file else 0))
            for p in self._pending_files(camera_dir, pos)
        )
        BACKLOG_BYTES.labels(camera_id).set(backlog)
        return published

    async def drain_once(self) -> int:
        """One pass over every camera. Returns records published."""
        if not self._manifest_root.exists():
            return 0
        total = 0
        for camera_dir in sorted(p for p in self._manifest_root.iterdir() if p.is_dir()):
            total += await self._drain_camera(camera_dir)
        return total

    async def run(self) -> None:
        """Publish forever. Failures back off and retry; they never propagate,
        because this task shares an event loop with capture and capture is
        sacred - an outbox crash must cost publish latency, not frames."""
        backoff = 1.0
        while True:
            try:
                await self._producer.start()
                log.info("outbox connected; draining %s", self._manifest_root)
                backoff = 1.0
                while True:
                    published = await self.drain_once()
                    if published:
                        log.info("outbox published %d records", published)
                    await asyncio.sleep(self._idle_delay)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("outbox failed; retrying in %.0fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            finally:
                # Suppress errors here: stop() flushes, and if the broker is the
                # thing that failed, the flush fails too - that is the retry
                # path's problem, not shutdown's.
                try:
                    await self._producer.stop()
                except Exception:  # noqa: BLE001
                    log.warning("producer close failed; unacked records will republish")

    async def close(self) -> None:
        try:
            await self._producer.stop()
        except Exception:  # noqa: BLE001
            log.warning("producer close failed; unacked records will republish")
