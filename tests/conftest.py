from __future__ import annotations

from pathlib import Path

import pytest
from blockade.config import Camera, CameraSource, Settings
from blockade.storage import LocalFrameCache, ManifestWriter

# A tiny but structurally valid JPEG (SOI + APP0 + EOI). Real bytes matter here:
# the poller hashes what it receives, so a placeholder string would not exercise
# the dedupe path the way an actual image does.
JPEG_A = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")
JPEG_B = bytes.fromhex("ffd8ffe000104a46494600010101000100010000ffd9")


@pytest.fixture
def camera() -> Camera:
    return Camera(
        camera_id="odot-1234",
        name="Portland - 11th at Milwaukie N",
        crossing_id="SE_11TH_MILWAUKIE",
        image_url="https://tripcheck.example/cams/1234.jpg",
        source=CameraSource.MANUAL,
        poll_interval_seconds=30.0,
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        s3_bucket="blockade-test",
        local_cache_dir=tmp_path / "frames",
        manifest_dir=tmp_path / "manifests",
        camera_config_path=tmp_path / "cameras.yaml",
        max_retries=2,
    )


@pytest.fixture
def cache(settings: Settings) -> LocalFrameCache:
    return LocalFrameCache(settings.local_cache_dir, settings.local_cache_ttl_days)


@pytest.fixture
def manifest(settings: Settings) -> ManifestWriter:
    # store=None: manifest upload is exercised separately against MinIO, not here.
    return ManifestWriter(settings.manifest_dir, store=None)
