"""FrameImages cache and path-traversal guard behavior."""

from __future__ import annotations

import asyncio
import os
import pathlib
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from api.images import FrameImages
from blockade.config import Settings
from blockade.storage import frame_key

VALID_KEY = "frames/odot-678/2026/08/11/06/1720000000000-abcd1234.jpg"
FRAME_BYTES = b"\xff\xd8\xff\xe0fake-jpeg"


def key_at(i: int) -> str:
    """Distinct frame keys spread over hours, as a real cache holds them."""
    return frame_key(
        "odot-678", datetime(2026, 8, 11, tzinfo=UTC) + timedelta(minutes=i), f"{i:08x}"
    )


def on_disk(images: FrameImages) -> int:
    return sum(p.stat().st_size for p in images._root.rglob("*.jpg"))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        s3_bucket="blockade-test",
        local_cache_dir=tmp_path / "frames",
        kafka_bootstrap=None,
    )


@pytest.fixture
def frame_images(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> FrameImages:
    store = MagicMock()
    store.get.return_value = FRAME_BYTES
    monkeypatch.setattr("api.images.S3ObjectStore", lambda s: store)
    fi = FrameImages(settings)
    fi._mock_store = store
    return fi


async def test_invalid_key_returns_none(frame_images: FrameImages) -> None:
    assert await frame_images.get("../../etc/passwd") is None
    assert await frame_images.get("frames/../secret.jpg") is None
    assert await frame_images.get("notframes/odot-678/2026/08/11/06/123-abcd1234.jpg") is None


async def test_cache_miss_fetches_from_s3_and_returns_bytes(
    frame_images: FrameImages,
) -> None:
    result = await frame_images.get(VALID_KEY)
    assert result == FRAME_BYTES
    frame_images._mock_store.get.assert_called_once_with(VALID_KEY)


async def test_cache_hit_returns_bytes_without_s3(
    frame_images: FrameImages,
) -> None:
    cache_path = frame_images._root / VALID_KEY
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(b"cached-bytes")

    result = await frame_images.get(VALID_KEY)

    assert result == b"cached-bytes"
    frame_images._mock_store.get.assert_not_called()


async def test_cache_read_race_falls_through_to_s3(
    frame_images: FrameImages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that vanishes between exists() and read_bytes() must not raise
    FileNotFoundError - the code falls through to S3 and returns valid bytes."""
    cache_path = frame_images._root / VALID_KEY
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(b"will-be-evicted")

    original_read_bytes = pathlib.Path.read_bytes

    def evicted_read_bytes(self: pathlib.Path) -> bytes:
        if self == cache_path:
            raise FileNotFoundError(f"simulated eviction race: {self}")
        return original_read_bytes(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", evicted_read_bytes)

    result = await frame_images.get(VALID_KEY)

    assert result == FRAME_BYTES
    frame_images._mock_store.get.assert_called_once_with(VALID_KEY)


# --------------------------------------------------------------- eviction
#
# The pod's cache is an emptyDir with a hard budget, and every frame it serves
# is a miss (it shares no volume with the poller), so a scrub drag is a burst
# of writes. What matters to the deployment is that the directory stays inside
# its budget however many of those arrive, and that a re-fetch of a frame
# already on disk is not counted as growth.

BIG = b"x" * 4096


async def test_a_burst_of_misses_stays_inside_the_budget(
    frame_images: FrameImages, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("api.images.CACHE_LIMIT_BYTES", 20 * len(BIG))
    monkeypatch.setattr("api.images.CACHE_TARGET_BYTES", 18 * len(BIG))
    frame_images._mock_store.get.return_value = BIG

    for i in range(60):
        assert await frame_images.get(key_at(i)) == BIG
        assert on_disk(frame_images) <= 20 * len(BIG), f"over budget after {i + 1} misses"

    assert on_disk(frame_images) <= 18 * len(BIG), "eviction goes to the low-water mark"
    assert on_disk(frame_images) > 0, "the cache is trimmed, not emptied"


async def test_eviction_takes_the_least_recently_read_first(
    frame_images: FrameImages, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("api.images.CACHE_LIMIT_BYTES", 4 * len(BIG))
    monkeypatch.setattr("api.images.CACHE_TARGET_BYTES", 2 * len(BIG))
    frame_images._mock_store.get.return_value = BIG

    for i in range(3):
        await frame_images.get(key_at(i))
        # atime resolution is coarse; age the files explicitly so "least
        # recently read" is a fact about the files, not about clock luck.
        path = frame_images._root / key_at(i)
        os.utime(path, (1_000_000 + i, 1_000_000 + i))

    for i in range(3, 6):
        await frame_images.get(key_at(i))

    assert not (frame_images._root / key_at(0)).exists(), "the oldest read goes first"
    assert (frame_images._root / key_at(5)).exists(), "the newest arrival stays"


async def test_concurrent_misses_on_one_key_are_one_frame_of_growth(
    frame_images: FrameImages, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scrub drag puts several requests for the same frame in flight at once:
    they all miss, all fetch, and all write the same bytes, but only the first
    grew the disk. Counting each write would evict a cache that never filled."""
    monkeypatch.setattr("api.images.CACHE_LIMIT_BYTES", 5 * len(BIG))
    monkeypatch.setattr("api.images.CACHE_TARGET_BYTES", 3 * len(BIG))
    frame_images._mock_store.get.side_effect = lambda key: (time.sleep(0.05), BIG)[1]

    for i in range(3):
        await frame_images.get(key_at(i))

    # six requests for one frame, all in flight before the first write lands
    await asyncio.gather(*(frame_images.get(key_at(99)) for _ in range(6)))

    assert frame_images._mock_store.get.call_count == 9, "all six really missed"
    for i in (0, 1, 2, 99):
        assert (frame_images._root / key_at(i)).exists(), f"key_at({i}) was evicted"
    assert on_disk(frame_images) == 4 * len(BIG)


async def test_every_key_the_capture_side_mints_is_servable(
    frame_images: FrameImages,
) -> None:
    """The guard's contract: it refuses anything that is not a frame key, and
    accepts every key blockade.storage.frame_key produces. A layout change that
    only one side learns about makes the whole board 404."""
    minted = [
        frame_key("odot-678", datetime(2026, 8, 11, 6, 5, tzinfo=UTC), "sha256:abcd1234ef567890"),
        frame_key("odot-681", datetime(2026, 1, 1, 0, 0, 0, 500_000, tzinfo=UTC), "00112233aabb"),
        frame_key(
            "odot-682",
            datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone(timedelta(hours=-8))),
            "ffffffff0000",
        ),
    ]

    for object_key in minted:
        assert await frame_images.get(object_key) == FRAME_BYTES, object_key
