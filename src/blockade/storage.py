"""Frame and manifest storage: local cache plus S3, behind one write path.

Every frame is written twice on purpose. S3 is the durable replay corpus; the
local cache is what the Phase 1 detector actually reads. With compute in k3s at
home and storage in AWS, reading frames back from S3 is billed egress, and the
detector re-reads the whole corpus every time thresholds are re-tuned. The cache
makes recurring egress ~zero while S3 keeps replay possible from anywhere.

S3 access goes through an endpoint-configurable client, so the demo stack and
integration tests point at MinIO and exercise this exact code.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import boto3
from botocore.config import Config as BotoConfig

from blockade.config import Settings
from blockade.schemas import FrameRecord

log = logging.getLogger(__name__)


def content_hash(data: bytes) -> str:
    """Hash used for dedupe. Prefixed so the algorithm is visible in the manifest."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def frame_key(camera_id: str, captured_at: datetime, digest: str) -> str:
    """S3 key for a frame.

    Hour is a separate path component so no single prefix accumulates a day's
    worth of objects, which keeps listing cheap during backfill.

    The key includes a content-hash suffix because the timestamp alone is not
    unique: ``captured_at`` comes from the image server's Last-Modified header,
    which has one-second granularity, so two genuinely different frames can share
    a timestamp. Without the suffix the second would overwrite the first and the
    manifest would then reference a key holding bytes it never recorded -- silent
    corruption of the one artifact that cannot be recaptured.

    Content-addressing also makes writes idempotent: replaying the same frame
    produces the same key rather than a duplicate object.
    """
    ts = captured_at.astimezone(UTC)
    epoch_ms = int(ts.timestamp() * 1000)
    short = digest.removeprefix("sha256:")[:8]
    return f"frames/{camera_id}/{ts:%Y}/{ts:%m}/{ts:%d}/{ts:%H}/{epoch_ms}-{short}.jpg"


def manifest_key(camera_id: str, captured_at: datetime) -> str:
    """S3 key for the hourly-rolled, gzipped JSONL manifest."""
    ts = captured_at.astimezone(UTC)
    return f"manifests/{camera_id}/{ts:%Y}/{ts:%m}/{ts:%d}/{ts:%H}.jsonl.gz"


class ObjectStore(Protocol):
    """Narrow interface so tests can substitute a local double for S3."""

    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...


class S3ObjectStore:
    """S3-compatible object store. Real AWS when ``s3_endpoint_url`` is None."""

    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.s3_bucket
        self._client = boto3.client(
            "s3",
            region_name=settings.s3_region,
            endpoint_url=settings.s3_endpoint_url,
            config=BotoConfig(
                retries={"max_attempts": 5, "mode": "adaptive"},
                connect_timeout=5,
                read_timeout=15,
            ),
        )

    def put(self, key: str, data: bytes, content_type: str = "image/jpeg") -> None:
        self._client.put_object(
            Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
        )

    def get(self, key: str) -> bytes:
        return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True


class LocalFrameCache:
    """Mirror of the S3 frame layout on a PVC, swept on a TTL.

    Keyed identically to S3 so a reader can try local first and fall back to the
    bucket using the same key, with no second path scheme to keep in sync.
    """

    def __init__(self, root: Path, ttl_days: int) -> None:
        self._root = root
        self._ttl_seconds = ttl_days * 86_400

    def path_for(self, key: str) -> Path:
        return self._root / key

    def write(self, key: str, data: bytes) -> Path:
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a crash mid-write must not leave a truncated JPEG
        # that the detector would silently score.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return path

    def read(self, key: str) -> bytes | None:
        path = self.path_for(key)
        return path.read_bytes() if path.exists() else None

    def sweep(self) -> int:
        """Delete cached frames past the TTL. Returns the number removed."""
        if not self._root.exists():
            return 0
        cutoff = time.time() - self._ttl_seconds
        removed = 0
        for path in self._root.rglob("*.jpg"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except FileNotFoundError:
                continue
        return removed

    def disk_usage_bytes(self) -> int:
        return shutil.disk_usage(self._root).used if self._root.exists() else 0


class ManifestWriter:
    """Append-only JSONL manifest, rolled hourly and gzipped to S3 on roll.

    The manifest is the backfill source for the whole pipeline, so it is written
    locally first and only then uploaded: a failed upload must never cost a
    record. Lines are exactly ``FrameRecord`` JSON, which is exactly the
    ``crossing.frames.v1`` Kafka payload -- no translation at replay time.
    """

    def __init__(self, root: Path, store: ObjectStore | None = None) -> None:
        self._root = root
        self._store = store
        self._open_hour: dict[str, str] = {}

    def _local_path(self, camera_id: str, captured_at: datetime) -> Path:
        ts = captured_at.astimezone(UTC)
        return self._root / camera_id / f"{ts:%Y-%m-%d-%H}.jsonl"

    def append(self, record: FrameRecord) -> Path:
        path = self._local_path(record.camera_id, record.captured_at)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = record.model_dump_json() + "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)

        hour = f"{record.captured_at.astimezone(UTC):%Y-%m-%d-%H}"
        previous = self._open_hour.get(record.camera_id)
        if previous is not None and previous != hour:
            self._roll(record.camera_id, previous)
        self._open_hour[record.camera_id] = hour
        return path

    def _roll(self, camera_id: str, hour: str) -> None:
        """Gzip a completed hour and upload it. Local copy is kept as the source of truth."""
        path = self._root / camera_id / f"{hour}.jsonl"
        if not path.exists() or self._store is None:
            return
        try:
            payload = gzip.compress(path.read_bytes())
            ts = datetime.strptime(hour, "%Y-%m-%d-%H").replace(tzinfo=UTC)
            self._store.put(manifest_key(camera_id, ts), payload, "application/gzip")
        except Exception:
            # An upload failure is recoverable -- the local JSONL still holds every
            # record and a later sweep can re-upload. Losing the poll loop is not.
            log.exception("manifest upload failed for %s hour %s", camera_id, hour)

    def flush_all(self) -> None:
        """Roll every open hour. Call on shutdown so the final partial hour lands."""
        for camera_id, hour in list(self._open_hour.items()):
            self._roll(camera_id, hour)
        self._open_hour.clear()
