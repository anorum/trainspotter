"""Frame bytes for the UI: S3 behind a disk LRU, keys strictly validated.

Frames are content-addressed (the key embeds a hash of the bytes), which buys
two things here: an object either exists immutably or not at all, so the
browser may cache forever; and the disk cache never needs invalidation, only
eviction.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from blockade.config import Settings
from blockade.storage import S3ObjectStore

log = logging.getLogger(__name__)

FRAME_KEY = re.compile(r"^frames/[\w-]+/\d{4}/\d{2}/\d{2}/\d{2}/\d+-[0-9a-f]{8}\.jpg$")
"""The only shape a frame key can have. Anything else is refused before it
reaches the filesystem or S3 - the path traversal guard."""

CACHE_LIMIT_BYTES = 200 * 1024 * 1024
"""Fits comfortably in the /tmp emptyDir; ~8000 frames at ~25KB each."""


class FrameImages:
    def __init__(self, settings: Settings) -> None:
        self._store = S3ObjectStore(settings)
        self._root = settings.local_cache_dir
        self._lock = asyncio.Lock()

    async def get(self, object_key: str) -> bytes | None:
        """Frame bytes, or None when the key is invalid or the object is gone."""
        if not FRAME_KEY.match(object_key):
            return None
        path = self._root / object_key
        if path.exists():
            return path.read_bytes()
        try:
            data = await asyncio.to_thread(self._store.get, object_key)
        except Exception:  # noqa: BLE001 - a missing frame is a 404, not a 500
            log.warning("frame unavailable: %s", object_key)
            return None
        await self._write_through(path, data)
        return data

    async def _write_through(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        async with self._lock:
            await asyncio.to_thread(self._evict)

    def _evict(self) -> None:
        """Oldest-accessed files out first once over budget. Called rarely and
        the cache is small, so a full walk is fine."""
        files = sorted(
            (p for p in self._root.rglob("*.jpg")),
            key=lambda p: p.stat().st_atime,
        )
        total = sum(p.stat().st_size for p in files)
        while total > CACHE_LIMIT_BYTES and files:
            victim = files.pop(0)
            total -= victim.stat().st_size
            victim.unlink(missing_ok=True)
