"""Phase 0 capture: poll ODOT camera images, dedupe, store, and manifest them.

ODOT does not archive these images -- each is overwritten by the next -- so every
minute this is not running is a minute permanently missing from the corpus. That
priority drives the error handling: nothing short of a fatal signal may stop the
loop, and any per-camera failure is recorded and retried rather than raised.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import signal
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
import typer
from blockade import __version__
from blockade.config import Camera, Settings, get_settings, load_roster
from blockade.schemas import CapturedAtSource, FetchStatus, FrameRecord
from blockade.storage import (
    LocalFrameCache,
    ManifestWriter,
    S3ObjectStore,
    content_hash,
    frame_key,
)
from prometheus_client import Counter, Gauge, Histogram, start_http_server

log = logging.getLogger(__name__)

FRAMES = Counter("blockade_frames_total", "Frame poll outcomes", ["camera_id", "status"])
FETCH_SECONDS = Histogram("blockade_fetch_seconds", "Image fetch latency", ["camera_id"])
BYTES_STORED = Counter("blockade_bytes_stored_total", "New image bytes stored", ["camera_id"])
LAST_NEW_FRAME = Gauge(
    "blockade_last_new_frame_timestamp",
    "Unix time of the last new (non-duplicate) frame",
    ["camera_id"],
)
CONSECUTIVE_ERRORS = Gauge(
    "blockade_consecutive_errors", "Consecutive failed polls", ["camera_id"]
)


@dataclass
class CameraCursor:
    """Per-camera state carried between polls.

    Held in memory only. On restart the first poll of each camera re-downloads and
    re-hashes, which costs one redundant fetch and is preferable to persisting
    state that could go stale against a camera that changed behind our back.
    """

    last_etag: str | None = None
    last_modified: str | None = None
    last_hash: str | None = None
    last_object_key: str | None = None
    consecutive_errors: int = 0
    backoff_until: float = 0.0

    def remember(self, digest: str, key: str) -> None:
        self.last_hash = digest
        self.last_object_key = key


def _captured_at(
    response: httpx.Response, fetched_at: datetime
) -> tuple[datetime, CapturedAtSource]:
    """Derive event time from the image server's Last-Modified header.

    Event time must be when the camera captured the frame, not when we fetched it.
    Last-Modified is the only signal available; when it is missing the fetch time
    is used, which can lag the true capture by up to one refresh interval. That
    lag is why the Phase 2 watermark allows 90s of out-of-orderness.
    """
    raw = response.headers.get("last-modified")
    if raw:
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            # A clock-skewed or bogus header must not push event time into the
            # future, which would stall watermarks for the whole job downstream.
            if parsed <= fetched_at:
                return parsed, CapturedAtSource.LAST_MODIFIED
        except (TypeError, ValueError):
            log.debug("unparseable Last-Modified: %r", raw)
    return fetched_at, CapturedAtSource.FETCHED_AT


class FramePoller:
    """Polls one roster of cameras and records every outcome."""

    def __init__(
        self,
        settings: Settings,
        cameras: list[Camera],
        client: httpx.AsyncClient,
        cache: LocalFrameCache,
        manifest: ManifestWriter,
        store: S3ObjectStore | None,
    ) -> None:
        self._settings = settings
        self._cameras = cameras
        self._client = client
        self._cache = cache
        self._manifest = manifest
        self._store = store
        self._cursors: dict[str, CameraCursor] = {c.camera_id: CameraCursor() for c in cameras}
        self._seed_metrics(cameras)

    @staticmethod
    def _seed_metrics(cameras: list[Camera]) -> None:
        """Create every per-camera series at startup rather than on first
        observation.

        A labelled metric has no series until something touches that label, and
        an alert threshold over a series that does not exist evaluates to no
        data -- it stays silent instead of firing. So the cameras most worth
        alerting on are exactly the ones that never register: one whose first
        poll dies before any counter is reached leaves
        `blockade_last_new_frame_timestamp{camera_id=...}` absent forever, and
        BlockadeCameraStalled has nothing to compare. Seeding the last-new-frame
        gauge to process start makes that camera breach the same 15-minute
        threshold a camera that went quiet later would.
        """
        started = datetime.now(UTC).timestamp()
        for camera in cameras:
            LAST_NEW_FRAME.labels(camera.camera_id).set(started)
            CONSECUTIVE_ERRORS.labels(camera.camera_id).set(0)
            for status in FetchStatus:
                FRAMES.labels(camera.camera_id, status.value)

    async def poll_once(self, camera: Camera) -> FrameRecord:
        """Poll a single camera and return the record that was written.

        Never raises for a fetch failure: an ERROR record is written instead, so
        a dark camera is visible in the timeline as UNKNOWN rather than absent.
        """
        cursor = self._cursors[camera.camera_id]
        fetched_at = datetime.now(UTC)

        headers = {"User-Agent": self._settings.user_agent}
        # Conditional GET: the polite path. A camera refreshing every 5 minutes
        # answers 304 to nine out of ten polls and transfers no bytes at all.
        if cursor.last_etag:
            headers["If-None-Match"] = cursor.last_etag
        if cursor.last_modified:
            headers["If-Modified-Since"] = cursor.last_modified

        try:
            with FETCH_SECONDS.labels(camera.camera_id).time():
                response = await self._client.get(
                    str(camera.image_url),
                    headers=headers,
                    timeout=self._settings.request_timeout_seconds,
                )
        except (httpx.HTTPError, OSError) as exc:
            return self._record_error(camera, cursor, fetched_at, f"{type(exc).__name__}: {exc}")

        if response.status_code == 304:
            cursor.consecutive_errors = 0
            captured_at, source = _captured_at(response, fetched_at)
            return self._write(
                camera,
                FrameRecord(
                    camera_id=camera.camera_id,
                    crossing_id=camera.crossing_id,
                    captured_at=captured_at,
                    captured_at_source=source,
                    fetched_at=fetched_at,
                    fetch_status=FetchStatus.NOT_MODIFIED,
                    object_key=cursor.last_object_key,
                    content_hash=cursor.last_hash,
                    image_bytes=None,
                    poller_version=__version__,
                ),
            )

        if response.status_code != 200:
            return self._record_error(
                camera, cursor, fetched_at, f"HTTP {response.status_code}"
            )

        data = response.content
        if not data:
            return self._record_error(camera, cursor, fetched_at, "empty response body")

        cursor.consecutive_errors = 0
        CONSECUTIVE_ERRORS.labels(camera.camera_id).set(0)
        cursor.last_etag = response.headers.get("etag")
        cursor.last_modified = response.headers.get("last-modified")
        captured_at, source = _captured_at(response, fetched_at)
        digest = content_hash(data)

        # Dedupe on bytes. ODOT re-serves an identical image when the camera has
        # not refreshed, and some cameras refresh far slower than 30s.
        if digest == cursor.last_hash:
            return self._write(
                camera,
                FrameRecord(
                    camera_id=camera.camera_id,
                    crossing_id=camera.crossing_id,
                    captured_at=captured_at,
                    captured_at_source=source,
                    fetched_at=fetched_at,
                    fetch_status=FetchStatus.DUPLICATE,
                    # Points at the first occurrence of these bytes: the timeline
                    # stays complete without storing the same JPEG twice.
                    object_key=cursor.last_object_key,
                    content_hash=digest,
                    image_bytes=len(data),
                    poller_version=__version__,
                ),
            )

        key = frame_key(camera.camera_id, captured_at, digest)
        self._cache.write(key, data)
        if self._store is not None:
            try:
                self._store.put(key, data, "image/jpeg")
            except Exception:
                # The local cache already holds the bytes and the manifest will
                # reference this key, so a later sweep can re-upload. Dropping the
                # frame entirely would be the worse outcome.
                log.exception("S3 upload failed for %s; frame retained locally", key)

        cursor.remember(digest, key)
        BYTES_STORED.labels(camera.camera_id).inc(len(data))
        LAST_NEW_FRAME.labels(camera.camera_id).set(fetched_at.timestamp())

        return self._write(
            camera,
            FrameRecord(
                camera_id=camera.camera_id,
                crossing_id=camera.crossing_id,
                captured_at=captured_at,
                captured_at_source=source,
                fetched_at=fetched_at,
                fetch_status=FetchStatus.OK,
                object_key=key,
                content_hash=digest,
                image_bytes=len(data),
                poller_version=__version__,
            ),
        )

    def _record_error(
        self, camera: Camera, cursor: CameraCursor, fetched_at: datetime, message: str
    ) -> FrameRecord:
        cursor.consecutive_errors += 1
        CONSECUTIVE_ERRORS.labels(camera.camera_id).set(cursor.consecutive_errors)
        # Exponential backoff capped at 5 minutes, so a camera that is down for a
        # day is retried steadily instead of hammered or abandoned.
        delay = min(2**cursor.consecutive_errors, 300)
        cursor.backoff_until = asyncio.get_running_loop().time() + delay
        log.warning(
            "camera %s poll failed (%d consecutive): %s; backing off %ss",
            camera.camera_id,
            cursor.consecutive_errors,
            message,
            delay,
        )
        return self._write(
            camera,
            FrameRecord(
                camera_id=camera.camera_id,
                crossing_id=camera.crossing_id,
                captured_at=fetched_at,
                captured_at_source=CapturedAtSource.FETCHED_AT,
                fetched_at=fetched_at,
                fetch_status=FetchStatus.ERROR,
                poller_version=__version__,
                error=message[:500],
            ),
        )

    def _write(self, camera: Camera, record: FrameRecord) -> FrameRecord:
        FRAMES.labels(camera.camera_id, record.fetch_status.value).inc()
        self._manifest.append(record)
        return record

    async def run_camera(self, camera: Camera) -> None:
        """Poll one camera forever. Each camera gets its own task so a slow or
        broken feed cannot delay the others."""
        loop = asyncio.get_running_loop()
        # Stagger the first poll so six cameras do not fire simultaneously every tick.
        await asyncio.sleep(random.uniform(0, camera.poll_interval_seconds))
        while True:
            started = loop.time()
            cursor = self._cursors[camera.camera_id]
            if started >= cursor.backoff_until:
                try:
                    await self.poll_once(camera)
                except Exception:
                    # poll_once handles its own failures; anything reaching here is
                    # a bug in our code and still must not kill the capture loop.
                    log.exception("unexpected error polling %s", camera.camera_id)
            elapsed = loop.time() - started
            await asyncio.sleep(max(0.0, camera.poll_interval_seconds - elapsed))

    async def run(self) -> None:
        tasks = [asyncio.create_task(self.run_camera(c), name=c.camera_id) for c in self._cameras]
        sweeper = asyncio.create_task(self._sweep_cache_periodically(), name="cache-sweeper")
        try:
            await asyncio.gather(*tasks, sweeper)
        finally:
            for task in (*tasks, sweeper):
                task.cancel()
            self._manifest.flush_all()

    async def _sweep_cache_periodically(self) -> None:
        """Expire cached frames -- but only when a durable copy exists elsewhere.

        The TTL exists because S3 holds the archive and the local copy is just a
        read cache that saves egress. With no object store configured there is no
        second copy, so sweeping would permanently destroy frames that ODOT has
        long since overwritten. Running local-only is a legitimate mode; silently
        deleting the corpus a week later is not.
        """
        if self._store is None:
            log.warning(
                "no object store configured: local frames will be kept indefinitely "
                "rather than expired after %d days, because there is no second copy. "
                "Expect ~180 MB/day. Run `blockade-sync` once a bucket exists.",
                self._settings.local_cache_ttl_days,
            )
            return

        while True:
            await asyncio.sleep(3600)
            try:
                removed = self._cache.sweep()
                log.info("cache sweep removed %d expired frames", removed)
            except Exception:
                log.exception("cache sweep failed")


app = typer.Typer(help="Phase 0 frame capture.", no_args_is_help=True)


def _fail(message: str) -> None:
    """Report a setup problem as a message, not a traceback. This is the first
    command anyone runs, and a missing roster is a configuration step, not a crash."""
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


async def _run(settings: Settings, local_only: bool, once: bool) -> None:
    roster = load_roster(settings.camera_config_path)
    cameras = roster.enabled()
    store = None if local_only else S3ObjectStore(settings)
    cache = LocalFrameCache(settings.local_cache_dir, settings.local_cache_ttl_days)
    manifest = ManifestWriter(settings.manifest_dir, store)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        poller = FramePoller(settings, cameras, client, cache, manifest, store)
        if once:
            for camera in cameras:
                record = await poller.poll_once(camera)
                typer.echo(
                    f"{record.camera_id:24} {record.fetch_status.value:14} "
                    f"{record.image_bytes or 0:>8}B  {record.captured_at.isoformat()} "
                    f"({record.captured_at_source.value})"
                )
            manifest.flush_all()
            return

        start_http_server(settings.metrics_port)
        log.info("capturing %d cameras; metrics on :%d", len(cameras), settings.metrics_port)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        runner = asyncio.create_task(poller.run())
        outbox_task = None
        outbox = None
        if settings.kafka_bootstrap:
            # A sibling task, never a step in the poll loop: the manifest append
            # is capture's commit point, and everything after it is async. With
            # no bootstrap configured, capture runs exactly as before and the
            # manifest accumulates for a later drain.
            from poller.outbox import ManifestOutbox

            outbox = ManifestOutbox(settings)
            outbox_task = asyncio.create_task(outbox.run(), name="outbox")
            log.info("outbox publishing to %s", settings.kafka_bootstrap)
        await stop.wait()
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner
        if outbox_task is not None:
            outbox_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await outbox_task
        if outbox is not None:
            await outbox.close()
        manifest.flush_all()
        log.info("shutdown complete")


LOCAL_ONLY_HELP = (
    "Write frames to local disk only, with no object store. Frames are then kept "
    "indefinitely rather than expired, since there is no second copy."
)


@app.command()
def run(
    local_only: bool = typer.Option(False, "--local-only", help=LOCAL_ONLY_HELP),
) -> None:
    """Capture continuously until SIGTERM."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(_run(get_settings(), local_only=local_only, once=False))
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))


@app.command()
def once(
    local_only: bool = typer.Option(True, "--local-only/--upload", help=LOCAL_ONLY_HELP),
) -> None:
    """Poll every camera exactly once and print the outcome. Use this to verify a
    roster before starting continuous capture."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(_run(get_settings(), local_only=local_only, once=True))
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))


if __name__ == "__main__":
    app()
