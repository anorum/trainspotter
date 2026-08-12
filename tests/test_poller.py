"""Poller behaviour: dedupe, conditional GET, event time, and failure handling.

These are the properties the entire corpus depends on. A dedupe bug wastes
storage; a timeline bug (a dropped duplicate, a bogus event time) corrupts data
that cannot be recaptured, because ODOT overwrites each image with the next.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from blockade.config import Camera, CameraSource, Settings
from blockade.schemas import CapturedAtSource, FetchStatus, FrameRecord
from blockade.storage import LocalFrameCache, ManifestWriter, content_hash, frame_key
from poller.poller import FramePoller
from prometheus_client import REGISTRY

from tests.conftest import JPEG_A, JPEG_B

IMAGE_URL = "https://tripcheck.example/cams/1234.jpg"


def build_poller(
    settings: Settings,
    camera: Camera,
    client: httpx.AsyncClient,
    cache: LocalFrameCache,
    manifest: ManifestWriter,
) -> FramePoller:
    return FramePoller(settings, [camera], client, cache, manifest, store=None)


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


@respx.mock
async def test_first_frame_is_stored_and_recorded(settings, camera, client, cache, manifest):
    respx.get(IMAGE_URL).mock(return_value=httpx.Response(200, content=JPEG_A))
    poller = build_poller(settings, camera, client, cache, manifest)

    record = await poller.poll_once(camera)

    assert record.fetch_status is FetchStatus.OK
    assert record.object_key is not None
    assert record.content_hash is not None and record.content_hash.startswith("sha256:")
    assert record.image_bytes == len(JPEG_A)
    assert cache.read(record.object_key) == JPEG_A


@respx.mock
async def test_identical_bytes_dedupe_but_still_record_the_tick(
    settings, camera, client, cache, manifest
):
    """A repeated image must not be stored twice, but must still appear in the
    timeline. Dropping the record would be indistinguishable downstream from the
    camera having gone dark, and a dark camera is not a clear crossing."""
    respx.get(IMAGE_URL).mock(return_value=httpx.Response(200, content=JPEG_A))
    poller = build_poller(settings, camera, client, cache, manifest)

    first = await poller.poll_once(camera)
    second = await poller.poll_once(camera)

    assert second.fetch_status is FetchStatus.DUPLICATE
    assert second.is_duplicate
    # Points at the first occurrence, so the frame is still retrievable for this tick.
    assert second.object_key == first.object_key
    assert second.content_hash == first.content_hash

    lines = (settings.manifest_dir / camera.camera_id).glob("*.jsonl")
    records = [json.loads(line) for path in lines for line in path.read_text().splitlines()]
    assert len(records) == 2, "both ticks must appear in the manifest"


@respx.mock
async def test_changed_bytes_produce_a_new_object(settings, camera, client, cache, manifest):
    respx.get(IMAGE_URL).mock(
        side_effect=[
            httpx.Response(200, content=JPEG_A),
            httpx.Response(200, content=JPEG_B),
        ]
    )
    poller = build_poller(settings, camera, client, cache, manifest)

    first = await poller.poll_once(camera)
    second = await poller.poll_once(camera)

    assert second.fetch_status is FetchStatus.OK
    assert second.object_key != first.object_key
    assert cache.read(second.object_key) == JPEG_B


@respx.mock
async def test_conditional_get_headers_are_sent_after_first_poll(
    settings, camera, client, cache, manifest
):
    """The polite path: once an ETag is known, every later poll is conditional."""
    route = respx.get(IMAGE_URL).mock(
        side_effect=[
            httpx.Response(200, content=JPEG_A, headers={"ETag": '"abc"'}),
            httpx.Response(304),
        ]
    )
    poller = build_poller(settings, camera, client, cache, manifest)

    await poller.poll_once(camera)
    record = await poller.poll_once(camera)

    assert route.calls[1].request.headers["If-None-Match"] == '"abc"'
    assert record.fetch_status is FetchStatus.NOT_MODIFIED
    assert record.is_duplicate
    assert record.image_bytes is None, "304 transfers no bytes"


@respx.mock
async def test_event_time_comes_from_last_modified(settings, camera, client, cache, manifest):
    captured = datetime(2026, 8, 8, 14, 32, 7, tzinfo=UTC)
    respx.get(IMAGE_URL).mock(
        return_value=httpx.Response(
            200,
            content=JPEG_A,
            headers={"Last-Modified": "Sat, 08 Aug 2026 14:32:07 GMT"},
        )
    )
    poller = build_poller(settings, camera, client, cache, manifest)

    record = await poller.poll_once(camera)

    assert record.captured_at == captured
    assert record.captured_at_source is CapturedAtSource.LAST_MODIFIED
    assert record.captured_at < record.fetched_at


@respx.mock
async def test_future_last_modified_is_rejected(settings, camera, client, cache, manifest):
    """A clock-skewed header must not push event time into the future. Phase 2
    watermarks advance on the max seen event time, so one bogus future timestamp
    would stall the whole job and drop every genuinely-timed record after it."""
    future = datetime.now(UTC) + timedelta(days=1)
    respx.get(IMAGE_URL).mock(
        return_value=httpx.Response(
            200,
            content=JPEG_A,
            headers={"Last-Modified": future.strftime("%a, %d %b %Y %H:%M:%S GMT")},
        )
    )
    poller = build_poller(settings, camera, client, cache, manifest)

    record = await poller.poll_once(camera)

    assert record.captured_at_source is CapturedAtSource.FETCHED_AT
    assert record.captured_at == record.fetched_at


@respx.mock
async def test_http_error_records_rather_than_raises(settings, camera, client, cache, manifest):
    respx.get(IMAGE_URL).mock(return_value=httpx.Response(503))
    poller = build_poller(settings, camera, client, cache, manifest)

    record = await poller.poll_once(camera)

    assert record.fetch_status is FetchStatus.ERROR
    assert record.object_key is None
    assert record.error is not None and "503" in record.error


@respx.mock
async def test_network_error_records_rather_than_raises(settings, camera, client, cache, manifest):
    respx.get(IMAGE_URL).mock(side_effect=httpx.ConnectError("no route to host"))
    poller = build_poller(settings, camera, client, cache, manifest)

    record = await poller.poll_once(camera)

    assert record.fetch_status is FetchStatus.ERROR
    assert "ConnectError" in record.error


@respx.mock
async def test_errors_back_off_and_recover(settings, camera, client, cache, manifest):
    respx.get(IMAGE_URL).mock(
        side_effect=[httpx.Response(500), httpx.Response(500), httpx.Response(200, content=JPEG_A)]
    )
    poller = build_poller(settings, camera, client, cache, manifest)
    cursor = poller._cursors[camera.camera_id]

    await poller.poll_once(camera)
    assert cursor.consecutive_errors == 1
    first_backoff = cursor.backoff_until

    await poller.poll_once(camera)
    assert cursor.consecutive_errors == 2
    assert cursor.backoff_until > first_backoff, "backoff must grow"

    record = await poller.poll_once(camera)
    assert record.fetch_status is FetchStatus.OK
    assert cursor.consecutive_errors == 0, "a success resets the backoff"


@respx.mock
async def test_empty_body_is_an_error_not_a_frame(settings, camera, client, cache, manifest):
    """A zero-byte 200 is a camera fault. Storing it would feed the detector an
    unreadable file and score it as though it were a real observation."""
    respx.get(IMAGE_URL).mock(return_value=httpx.Response(200, content=b""))
    poller = build_poller(settings, camera, client, cache, manifest)

    record = await poller.poll_once(camera)

    assert record.fetch_status is FetchStatus.ERROR


async def test_cache_is_never_swept_without_a_durable_copy(
    settings, camera, client, cache, manifest, tmp_path
):
    """The TTL sweeper assumes S3 holds the archive and the local copy is a
    read cache. Running local-only, there is no second copy -- sweeping would
    permanently destroy frames ODOT has long since overwritten. The sweeper must
    refuse to run rather than quietly delete the corpus a week later."""
    key = frame_key("odot-1234", datetime.now(UTC), content_hash(b"old"))
    path = cache.write(key, b"old")
    stale = time.time() - 400 * 86_400
    os.utime(path, (stale, stale))

    poller = FramePoller(settings, [camera], client, cache, manifest, store=None)
    await asyncio.wait_for(poller._sweep_cache_periodically(), timeout=1.0)

    assert path.exists(), "a frame with no second copy must never be deleted"


@respx.mock
async def test_manifest_lines_roundtrip_as_frame_records(
    settings, camera, client, cache, manifest
):
    """The manifest is the Phase 2 backfill source and its lines are replayed
    straight onto the crossing.frames.v1 topic, so every line must parse back
    into the schema with no translation."""
    respx.get(IMAGE_URL).mock(return_value=httpx.Response(200, content=JPEG_A))
    poller = build_poller(settings, camera, client, cache, manifest)
    written = await poller.poll_once(camera)

    path = next((settings.manifest_dir / camera.camera_id).glob("*.jsonl"))
    parsed = [FrameRecord.model_validate_json(line) for line in path.read_text().splitlines()]

    assert len(parsed) == 1
    assert parsed[0] == written


def test_every_camera_is_alertable_before_its_first_poll(settings, client, cache, manifest):
    """A camera whose first poll never reaches a counter -- a bug in the store
    path, an unwritable cache -- would otherwise have no series at all, and the
    stall alert would evaluate to no data and stay silent about the camera most
    worth alerting on. The exported series must exist from construction."""
    before = datetime.now(UTC).timestamp()
    cam = Camera(
        camera_id="odot-unpolled",
        name="Portland - never answers",
        crossing_id="SE_12TH_CLINTON",
        image_url=IMAGE_URL,
        source=CameraSource.MANUAL,
        poll_interval_seconds=30.0,
    )

    FramePoller(settings, [cam], client, cache, manifest, store=None)

    labels = {"camera_id": "odot-unpolled"}
    stalled_since = REGISTRY.get_sample_value("blockade_last_new_frame_timestamp", labels)
    assert stalled_since is not None, "no series means the stall threshold cannot fire"
    assert before <= stalled_since <= datetime.now(UTC).timestamp()
    assert REGISTRY.get_sample_value("blockade_consecutive_errors", labels) == 0.0
    for status in FetchStatus:
        counted = REGISTRY.get_sample_value(
            "blockade_frames_total", {"camera_id": "odot-unpolled", "status": status.value}
        )
        assert counted == 0.0, f"{status.value} has no series to rate against"
