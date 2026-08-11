"""FrameImages cache and path-traversal guard behavior."""

from __future__ import annotations

import pathlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from blockade.config import Settings

from api.images import FRAME_KEY, FrameImages

VALID_KEY = "frames/odot-678/2026/08/11/06/1720000000000-abcd1234.jpg"
FRAME_BYTES = b"\xff\xd8\xff\xe0fake-jpeg"


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
