"""Key layout, cache behaviour, and manifest rolling."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta, timezone

from blockade.schemas import CapturedAtSource, FetchStatus, FrameRecord
from blockade.storage import LocalFrameCache, ManifestWriter, content_hash, frame_key, manifest_key

CAPTURED = datetime(2026, 8, 8, 14, 32, 7, tzinfo=UTC)


DIGEST_A = content_hash(b"frame-a")
DIGEST_B = content_hash(b"frame-b")


def test_frame_key_layout_includes_hour():
    """Hour is its own path component so no prefix accumulates a day of objects,
    which keeps listing cheap during backfill."""
    key = frame_key("odot-1234", CAPTURED, DIGEST_A)
    assert key.startswith("frames/odot-1234/2026/08/08/14/")
    assert key.endswith(".jpg")


def test_frame_key_normalises_to_utc():
    """The same instant in another offset must land in the same partition, or a
    replay would scatter one hour of frames across two prefixes."""
    pacific = CAPTURED.astimezone(timezone(timedelta(hours=-7)))
    assert frame_key("odot-1234", pacific, DIGEST_A) == frame_key("odot-1234", CAPTURED, DIGEST_A)


def test_different_frames_sharing_a_timestamp_get_different_keys():
    """captured_at comes from Last-Modified, which has one-second granularity, so
    two distinct frames can share a timestamp. If they shared a key the second
    would overwrite the first and the manifest would reference bytes it never
    recorded -- silent corruption of a corpus that cannot be recaptured."""
    assert frame_key("odot-1234", CAPTURED, DIGEST_A) != frame_key("odot-1234", CAPTURED, DIGEST_B)


def test_frame_key_is_idempotent_for_identical_content():
    """Replaying the same frame must land on the same key rather than duplicating
    the object."""
    assert frame_key("odot-1234", CAPTURED, DIGEST_A) == frame_key("odot-1234", CAPTURED, DIGEST_A)


def test_manifest_key_layout():
    assert manifest_key("odot-1234", CAPTURED) == "manifests/odot-1234/2026/08/08/14.jsonl.gz"


def test_content_hash_is_prefixed_and_stable():
    assert content_hash(b"abc").startswith("sha256:")
    assert content_hash(b"abc") == content_hash(b"abc")
    assert content_hash(b"abc") != content_hash(b"abd")


def test_cache_write_is_atomic(tmp_path):
    """Write-then-rename: a crash mid-write must not leave a truncated JPEG that
    the detector would silently score as a real observation."""
    cache = LocalFrameCache(tmp_path, ttl_days=7)
    key = frame_key("odot-1234", CAPTURED, DIGEST_A)
    path = cache.write(key, b"\xff\xd8data")

    assert path.read_bytes() == b"\xff\xd8data"
    assert not list(tmp_path.rglob("*.tmp")), "no temp files left behind"


def test_cache_sweep_removes_only_expired_frames(tmp_path):
    cache = LocalFrameCache(tmp_path, ttl_days=7)
    fresh = cache.write(frame_key("odot-1234", CAPTURED, DIGEST_A), b"fresh")
    stale = cache.write(frame_key("odot-5678", CAPTURED, DIGEST_B), b"stale")
    old = time.time() - 8 * 86_400
    os.utime(stale, (old, old))

    removed = cache.sweep()

    assert removed == 1
    assert fresh.exists()
    assert not stale.exists()


def _record(captured_at: datetime, camera_id: str = "odot-1234") -> FrameRecord:
    return FrameRecord(
        camera_id=camera_id,
        crossing_id="SE_11TH_MILWAUKIE",
        captured_at=captured_at,
        captured_at_source=CapturedAtSource.LAST_MODIFIED,
        fetched_at=captured_at,
        fetch_status=FetchStatus.OK,
        object_key=frame_key(camera_id, captured_at, content_hash(b"x")),
        content_hash=content_hash(b"x"),
        image_bytes=1,
        poller_version="0.1.0",
    )


def test_manifest_appends_one_line_per_record(tmp_path):
    writer = ManifestWriter(tmp_path, store=None)
    writer.append(_record(CAPTURED))
    writer.append(_record(CAPTURED + timedelta(seconds=30)))

    path = next((tmp_path / "odot-1234").glob("*.jsonl"))
    assert len(path.read_text().splitlines()) == 2


def test_manifest_rolls_on_the_hour(tmp_path):
    writer = ManifestWriter(tmp_path, store=None)
    writer.append(_record(CAPTURED))
    writer.append(_record(CAPTURED + timedelta(hours=1)))

    files = sorted(p.name for p in (tmp_path / "odot-1234").glob("*.jsonl"))
    assert files == ["2026-08-08-14.jsonl", "2026-08-08-15.jsonl"]


def test_manifest_upload_failure_does_not_lose_records(tmp_path):
    """The manifest is the backfill source for the whole pipeline. A failed
    upload must cost nothing -- the local JSONL is the source of truth."""

    class FailingStore:
        def put(self, key, data, content_type):
            raise RuntimeError("S3 unavailable")

        def get(self, key):
            raise NotImplementedError

        def exists(self, key):
            return False

    writer = ManifestWriter(tmp_path, store=FailingStore())
    writer.append(_record(CAPTURED))
    writer.append(_record(CAPTURED + timedelta(hours=1)))  # triggers a roll + failed upload

    path = tmp_path / "odot-1234" / "2026-08-08-14.jsonl"
    assert len(path.read_text().splitlines()) == 1, "record survives the upload failure"
