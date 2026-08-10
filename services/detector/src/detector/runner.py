"""Detector service entrypoints.

Sits between the poller and Kafka, per DESIGN2 section 3: it reads frames and
publishes raw detections. Flink consumes those, and never sees an image.

Two entrypoints over one code path:

- ``scan`` scores frames already on disk. This is the backfill and replay route,
  and the one that regenerates the whole record after a detector change.
- ``run`` will consume frame metadata from Kafka and publish detections
  continuously. It lands with the broker.

They differ only in where frames arrive from, so a detector improvement reaches
live traffic and the historical record through the same code.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import typer
from blockade.config import Settings, get_settings, load_roster
from blockade.detect.registry import build_detector
from blockade.schemas import CrossingState, FrameRecord, ObservationRecord

log = logging.getLogger(__name__)
app = typer.Typer(help="Detection: frames to observations.", no_args_is_help=True)
DEFAULT_OUTPUT = Path("var/observations/observations.jsonl")


def score_frames(
    records: list[FrameRecord], settings: Settings | None = None
) -> list[ObservationRecord]:
    """Score frames that have image bytes available locally.

    Duplicate and error records carry no new image, so they are skipped rather
    than re-scored: the manifest records that the tick happened, and inventing a
    second identical observation for it would double-count in every statistic.
    """
    settings = settings or get_settings()
    roster = {c.camera_id: c for c in load_roster(settings.camera_config_path).enabled()}
    detector = build_detector(settings=settings)
    cache = settings.local_cache_dir

    observations: list[ObservationRecord] = []
    for record in records:
        camera = roster.get(record.camera_id)
        if camera is None or record.object_key is None or record.is_duplicate:
            continue
        path = cache / record.object_key
        if not path.exists():
            continue
        observations.append(
            detector.classify(path.read_bytes(), camera, record.captured_at, record.object_key)
        )
    return observations


def read_manifests(manifest_dir: Path, camera_id: str | None = None) -> list[FrameRecord]:
    records: list[FrameRecord] = []
    for path in sorted(manifest_dir.rglob("*.jsonl")):
        if camera_id and path.parent.name != camera_id:
            continue
        for line in path.read_text().splitlines():
            if line.strip():
                records.append(FrameRecord.model_validate_json(line))
    records.sort(key=lambda r: r.captured_at)
    return records


@app.command()
def scan(
    camera: str = typer.Option("", help="Limit to one camera id."),
    since: str = typer.Option("", help="ISO timestamp; only frames at or after it."),
    output: Path = typer.Option(DEFAULT_OUTPUT, help="Where to write observations."),
) -> None:
    """Score frames from the manifest and write observations as JSONL.

    The replay path: rerun this after any detector change to regenerate the
    record from frames that were kept precisely so it could be regenerated.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = get_settings()
    records = read_manifests(settings.manifest_dir, camera or None)
    if since:
        cutoff = datetime.fromisoformat(since)
        if cutoff.tzinfo is None:
            # Frame timestamps are always UTC-aware. A naive --since would raise
            # on comparison, and silently assuming local time would quietly
            # select the wrong window.
            cutoff = cutoff.replace(tzinfo=UTC)
        records = [r for r in records if r.captured_at >= cutoff]

    observations = score_frames(records, settings)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(o.model_dump_json() for o in observations) + "\n")

    counts = {s.value: sum(1 for o in observations if o.state is s) for s in CrossingState}
    typer.echo(f"{len(observations)} observations -> {output}")
    typer.echo(f"  {json.dumps(counts)}")


@app.command()
def run() -> None:
    """Consume frame metadata from Kafka and publish detections continuously."""
    raise typer.Exit(
        typer.echo(
            "Streaming mode needs the Kafka broker, which is not deployed yet. "
            "Use `blockade-detect scan` to score frames already captured.",
            err=True,
        )
        or 1
    )


if __name__ == "__main__":
    app()
