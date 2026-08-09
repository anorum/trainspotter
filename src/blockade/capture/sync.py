"""Push locally-captured frames and manifests to S3.

Two jobs, both of which the poller deliberately does not do inline:

1. **Backfill.** Capture runs before S3 exists (or while credentials are expired)
   so the corpus starts on time. This uploads what accumulated locally.
2. **Repair.** The poller treats an S3 upload failure as recoverable and keeps
   the bytes locally rather than dropping the frame. This is the sweep that
   makes good on that promise.

Both are safe to re-run. Frame keys are content-addressed, so uploading the same
frame twice is a no-op rather than a duplicate.
"""

from __future__ import annotations

import gzip
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import typer

from blockade.config import Settings, get_settings
from blockade.storage import S3ObjectStore, manifest_key

log = logging.getLogger(__name__)


def _local_frames(cache_root: Path) -> Iterator[tuple[str, Path]]:
    """Yield (s3_key, path). The cache mirrors the S3 layout, so the key is just
    the path relative to the cache root -- there is no second scheme to keep in
    sync, and a mismatch here would be silent."""
    for path in cache_root.rglob("*.jpg"):
        yield str(path.relative_to(cache_root)), path


def _local_manifests(manifest_root: Path) -> Iterator[tuple[str, Path]]:
    """Yield (s3_key, path) for hourly JSONL files."""
    for path in manifest_root.rglob("*.jsonl"):
        try:
            # Written as {camera_id}/{YYYY-MM-DD-HH}.jsonl
            hour = datetime.strptime(path.stem, "%Y-%m-%d-%H").replace(tzinfo=UTC)
        except ValueError:
            log.warning("skipping manifest with unexpected name: %s", path)
            continue
        yield manifest_key(path.parent.name, hour), path


def sync(settings: Settings, dry_run: bool = False) -> dict[str, int]:
    """Upload anything local that is not already in the bucket."""
    store = S3ObjectStore(settings)
    stats = {"frames_uploaded": 0, "frames_skipped": 0, "manifests_uploaded": 0, "bytes": 0}

    cache_root = settings.local_cache_dir
    if cache_root.exists():
        remote = store.list_keys("frames/")
        for key, path in _local_frames(cache_root):
            if key in remote:
                stats["frames_skipped"] += 1
                continue
            data = path.read_bytes()
            if not dry_run:
                store.put(key, data, "image/jpeg")
            stats["frames_uploaded"] += 1
            stats["bytes"] += len(data)

    # Manifests are always re-uploaded: the current hour is still being appended
    # to, so a "already present" check would freeze it at its first upload.
    if settings.manifest_dir.exists():
        for key, path in _local_manifests(settings.manifest_dir):
            if not dry_run:
                store.put(key, gzip.compress(path.read_bytes()), "application/gzip")
            stats["manifests_uploaded"] += 1

    return stats


app = typer.Typer(help="Sync local capture output to S3.", no_args_is_help=True)


@app.command()
def run(
    dry_run: bool = typer.Option(False, help="Report what would upload, without uploading."),
) -> None:
    """Upload local frames and manifests to S3. Safe to re-run."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = get_settings()
    try:
        stats = sync(settings, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - surfaced as a message, not a traceback
        typer.secho(f"Sync failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    verb = "would upload" if dry_run else "uploaded"
    typer.echo(
        f"{verb} {stats['frames_uploaded']} frames "
        f"({stats['bytes'] / 1024 / 1024:.1f} MB), "
        f"{stats['manifests_uploaded']} manifests; "
        f"{stats['frames_skipped']} already present"
    )


if __name__ == "__main__":
    app()
