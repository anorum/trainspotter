"""Backfill and repair sync."""

from __future__ import annotations

from datetime import UTC, datetime

from blockade.capture.sync import sync
from blockade.config import Settings
from blockade.storage import LocalFrameCache, ManifestWriter, content_hash, frame_key
from tests.test_storage import _record

CAPTURED = datetime(2026, 8, 8, 14, 32, 7, tzinfo=UTC)


class FakeStore:
    """Stands in for S3. The real thing is exercised against MinIO, not mocked."""

    def __init__(self, existing: set[str] | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.existing = existing or set()
        self.put_calls = 0

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data
        self.put_calls += 1

    def get(self, key: str) -> bytes:
        return self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects or key in self.existing

    def list_keys(self, prefix: str) -> set[str]:
        return {k for k in self.existing | set(self.objects) if k.startswith(prefix)}


def _settings(tmp_path) -> Settings:
    return Settings(
        s3_bucket="pdx-trainspotter-test",
        local_cache_dir=tmp_path / "frames",
        manifest_dir=tmp_path / "manifests",
        camera_config_path=tmp_path / "cameras.yaml",
    )


def _seed(settings: Settings, n: int = 3) -> list[str]:
    cache = LocalFrameCache(settings.local_cache_dir, 7)
    writer = ManifestWriter(settings.manifest_dir, store=None)
    keys = []
    for i in range(n):
        data = f"frame-{i}".encode()
        key = frame_key("odot-676", CAPTURED, content_hash(data))
        cache.write(key, data)
        writer.append(_record(CAPTURED, camera_id="odot-676"))
        keys.append(key)
    return keys


def test_uploads_local_frames(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    keys = _seed(settings)
    store = FakeStore()
    monkeypatch.setattr("blockade.capture.sync.S3ObjectStore", lambda s: store)

    stats = sync(settings)

    assert stats["frames_uploaded"] == len(set(keys))
    assert set(keys) <= set(store.objects)


def test_skips_frames_already_in_the_bucket(tmp_path, monkeypatch):
    """Re-running a backfill must not re-upload the corpus. Content-addressed
    keys make this exact rather than heuristic."""
    settings = _settings(tmp_path)
    keys = _seed(settings)
    store = FakeStore(existing=set(keys))
    monkeypatch.setattr("blockade.capture.sync.S3ObjectStore", lambda s: store)

    stats = sync(settings)

    assert stats["frames_uploaded"] == 0
    assert stats["frames_skipped"] == len(set(keys))


def test_dry_run_uploads_nothing(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _seed(settings)
    store = FakeStore()
    monkeypatch.setattr("blockade.capture.sync.S3ObjectStore", lambda s: store)

    stats = sync(settings, dry_run=True)

    assert stats["frames_uploaded"] > 0
    assert store.put_calls == 0


def test_manifests_are_reuploaded_each_run(tmp_path, monkeypatch):
    """The current hour is still being appended to, so skipping it as 'already
    present' would freeze it at whatever it held on the first sync."""
    settings = _settings(tmp_path)
    _seed(settings)
    store = FakeStore()
    monkeypatch.setattr("blockade.capture.sync.S3ObjectStore", lambda s: store)

    first = sync(settings)
    second = sync(settings)

    assert first["manifests_uploaded"] == second["manifests_uploaded"] == 1
    assert second["frames_uploaded"] == 0, "frames are not re-uploaded, manifests are"


def test_sync_is_safe_with_no_local_data(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    store = FakeStore()
    monkeypatch.setattr("blockade.capture.sync.S3ObjectStore", lambda s: store)

    stats = sync(settings)

    assert stats == {
        "frames_uploaded": 0,
        "frames_skipped": 0,
        "manifests_uploaded": 0,
        "bytes": 0,
    }
