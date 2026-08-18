"""Frame bytes for the UI: S3 behind a disk LRU, keys strictly validated.

Frames are content-addressed (the key embeds a hash of the bytes), which buys
two things here: an object either exists immutably or not at all, so the
browser may cache forever; and the disk cache never needs invalidation, only
eviction.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from blockade.config import Settings
from blockade.storage import S3ObjectStore

log = logging.getLogger(__name__)

FRAME_KEY = re.compile(r"^frames/[\w-]+/\d{4}/\d{2}/\d{2}/\d{2}/\d+-[0-9a-f]{8}\.jpg$")
"""The only shape a frame key can have. Anything else is refused before it
reaches the filesystem or S3 - the path traversal guard. Must accept every key
``blockade.storage.frame_key`` mints; a layout change lands there first."""

CACHE_LIMIT_BYTES = 200 * 1024 * 1024
"""Fits comfortably in the /tmp emptyDir; ~8000 frames at ~25KB each."""

CACHE_TARGET_BYTES = int(CACHE_LIMIT_BYTES * 0.9)
"""Evicting only back to the limit leaves the cache full, so the very next
miss walks and sorts every file again. Going to a low-water mark amortizes
that walk over the thousands of writes it takes to climb the last 10%."""


class FrameImages:
    def __init__(self, settings: Settings) -> None:
        self._store = S3ObjectStore(settings)
        self._root = settings.local_cache_dir
        self._lock = asyncio.Lock()
        # Running total so a cache miss costs one addition, not a full walk:
        # the pod shares no volume with the poller, so every frame it serves
        # is a miss and a scrub drag produces dozens in a burst. None until
        # the first eviction seeds it from disk (survives restarts with a
        # warm emptyDir).
        self._total_bytes: int | None = None

    async def get(self, object_key: str) -> bytes | None:
        """Frame bytes, or None when the key is invalid or the object is gone."""
        if not FRAME_KEY.match(object_key):
            return None
        path = self._root / object_key
        try:
            if path.exists():
                return path.read_bytes()
        except FileNotFoundError:
            pass
        try:
            data = await asyncio.to_thread(self._store.get, object_key)
        except Exception:  # noqa: BLE001 - a missing frame is a 404, not a 500
            log.warning("frame unavailable: %s", object_key)
            return None
        await self._write_through(path, data)
        return data

    async def _write_through(self, path: Path, data: bytes) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write_and_evict, path, data)

    def _write_and_evict(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        existed = path.exists()
        tmp.replace(path)
        if self._total_bytes is None:
            self._total_bytes = sum(size for _, _, size in self._walk())
        elif not existed:
            # Two concurrent misses on the same key both fetch and both write;
            # only the first one grew the disk.
            self._total_bytes += len(data)
        if self._total_bytes > CACHE_LIMIT_BYTES:
            self._evict()

    def _walk(self) -> list[tuple[Path, float, int]]:
        """(path, atime, size) for every cached frame - one stat per file."""
        out: list[tuple[Path, float, int]] = []
        for dirpath, _, filenames in os.walk(self._root):
            for name in filenames:
                if not name.endswith(".jpg"):
                    continue
                p = Path(dirpath, name)
                try:
                    st = p.stat()
                except FileNotFoundError:
                    continue
                out.append((p, st.st_atime, st.st_size))
        return out

    def _evict(self) -> None:
        """Oldest-accessed files out first until back under the low-water mark."""
        files = sorted(self._walk(), key=lambda t: t[1])
        total = sum(size for _, _, size in files)
        for victim, _, size in files:
            if total <= CACHE_TARGET_BYTES:
                break
            victim.unlink(missing_ok=True)
            total -= size
        self._total_bytes = total
