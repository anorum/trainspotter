"""Detector service entrypoints.

Sits between the poller and Kafka: it reads frames and
publishes raw detections. The sessionizer consumes those, and never sees an image.

Two entrypoints carry the traffic, over one code path:

- ``scan`` scores frames already on disk. This is the backfill and replay route,
  and the one that regenerates the whole record after a detector change.
- ``run`` consumes frame metadata from Kafka and publishes detections
  continuously, committing offsets only after the broker acks them.

They differ only in where frames arrive from, so a detector improvement reaches
live traffic and the historical record through the same code.

``explain``, ``band``, and ``spotcheck`` are the operator tools beside them:
score a single frame, derive a camera's track band, and grow the label set.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import typer
from blockade.config import Camera, Settings, get_settings, load_roster
from blockade.detect.registry import build_detector
from blockade.schemas import CrossingState, FrameRecord, ObservationRecord
from prometheus_client import Counter, start_http_server

log = logging.getLogger(__name__)

SCORED = Counter(
    "blockade_detector_scored_total",
    "Observations published, by resulting state",
    ["camera_id", "state"],
)
SKIPPED_FRAMES = Counter(
    "blockade_detector_skipped_total",
    "Frame records consumed but not scored (duplicates, errors, missing bytes)",
    ["camera_id"],
)
app = typer.Typer(help="Detection: frames to observations.", no_args_is_help=True)
DEFAULT_OUTPUT = Path("var/observations/observations.jsonl")


def _utc(stamp: str) -> datetime:
    """Frame timestamps are always UTC-aware. A naive bound would raise on
    comparison, and silently assuming local time would quietly select the
    wrong window."""
    cutoff = datetime.fromisoformat(stamp)
    return cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=UTC)


class Scorer:
    """One scoring path for both entrypoints.

    ``scan`` and ``run`` differ only in where image bytes come from - the local
    cache versus S3 - so the byte source is the one injected piece. Everything
    that decides what a frame means lives here once, and a detector change
    reaches live traffic and the historical record through the same code.
    """

    def __init__(
        self,
        settings: Settings,
        read_bytes: Callable[[str], bytes | None],
    ) -> None:
        self.roster = {c.camera_id: c for c in load_roster(settings.camera_config_path).enabled()}
        self.detector = build_detector(settings=settings)
        self._read_bytes = read_bytes

    def score(self, record: FrameRecord) -> ObservationRecord | None:
        """One frame to one observation, or None when there is nothing to score.

        Duplicate and error records carry no new image, so they are skipped
        rather than re-scored: the manifest records that the tick happened, and
        inventing a second identical observation for it would double-count in
        every statistic.
        """
        camera = self.roster.get(record.camera_id)
        if camera is None or record.object_key is None or record.is_duplicate:
            return None
        image = self._read_bytes(record.object_key)
        if image is None:
            return None
        return self.detector.classify(image, camera, record.captured_at, record.object_key)


def score_frames(
    records: list[FrameRecord], settings: Settings | None = None
) -> list[ObservationRecord]:
    """Score frames whose image bytes are in the local cache. The scan path."""
    settings = settings or get_settings()
    cache = settings.local_cache_dir

    def read_local(object_key: str) -> bytes | None:
        path = cache / object_key
        return path.read_bytes() if path.exists() else None

    scorer = Scorer(settings, read_local)
    return [obs for record in records if (obs := scorer.score(record)) is not None]


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
    until: str = typer.Option("", help="ISO timestamp; only frames strictly before it."),
    output: Path = typer.Option(DEFAULT_OUTPUT, help="Where to write observations."),
) -> None:
    """Score frames from the manifest and write observations as JSONL.

    The replay path: rerun this after any detector change to regenerate the
    record from frames that were kept precisely so it could be regenerated.
    --since/--until bound the window, which is what makes the output loadable
    as a backfill: `blockade-api backfill` refuses windows that reach into
    the live streaming edge.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = get_settings()
    records = read_manifests(settings.manifest_dir, camera or None)
    if since:
        records = [r for r in records if r.captured_at >= _utc(since)]
    if until:
        records = [r for r in records if r.captured_at < _utc(until)]

    observations = score_frames(records, settings)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(o.model_dump_json() for o in observations) + "\n")

    counts = {s.value: sum(1 for o in observations if o.state is s) for s in CrossingState}
    typer.echo(f"{len(observations)} observations -> {output}")
    typer.echo(f"  {json.dumps(counts)}")


@app.command()
def explain(
    image: Path = typer.Argument(
        exists=True,
        dir_okay=False,
        help="Frame to score; camera id inferred from the path.",
    ),
    camera: str = typer.Option("", help="Camera id when the path does not contain one."),
) -> None:
    """Score one frame and print the judgement. The debugging path.

    Builds the same detector the deployment builds (BLOCKADE_DETECTOR), so
    what this prints is what the pod would have published for the same bytes.
    The reason line carries the evidence; every detector writes one for
    exactly this audit.
    """
    settings = get_settings()
    roster = {c.camera_id: c for c in load_roster(settings.camera_config_path).enabled()}
    camera_id = camera or next((cid for cid in roster if cid in image.resolve().parts), "")
    cam = roster.get(camera_id)
    if cam is None:
        typer.secho(
            f"Cannot tell which camera this frame belongs to. Pass --camera; "
            f"roster has: {', '.join(roster)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    detector = build_detector(settings=settings)
    captured_at = datetime.fromtimestamp(image.stat().st_mtime, tz=UTC)
    observation = detector.classify(image.read_bytes(), cam, captured_at, image.name)
    typer.echo(f"state={observation.state.value}")
    typer.echo(f"confidence={observation.confidence:.2f}")
    typer.echo(f"reason={observation.reason}")
    typer.echo(f"detector_version={observation.detector_version}")


@app.command()
def band(
    camera: str = typer.Argument(help="Camera id, e.g. odot-676."),
    blocked_dir: Path = typer.Option(
        ...,
        exists=True,
        file_okay=False,
        help="Directory of known-BLOCKED frames for this camera, e.g. data/blocks/odot-676.",
    ),
    write: bool = typer.Option(False, help="Write the band into the camera's reference metadata."),
) -> None:
    """Derive a camera's track band from hand-labeled blocked frames.

    A new camera runs bandless (whole frame counts) until it has been seen
    blocked; this is how it earns its band from data instead of a hand-drawn
    ROI. Rerun with more frames any time; --write updates the local reference
    metadata, which then ships to S3 like any reference change.
    """
    from blockade.detect.reference import ReferenceModel, derive_band_from_frames

    settings = get_settings()
    model = ReferenceModel.load(settings.references_dir, camera)
    if model is None:
        typer.secho(
            f"No reference model for {camera} in {settings.references_dir}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    frames = [
        p.read_bytes()
        for p in sorted(blocked_dir.iterdir())
        if p.suffix.lower() in (".jpg", ".jpeg", ".png")
    ]
    derived, used = derive_band_from_frames(model, frames)
    if derived is None:
        typer.secho(
            f"No band derivable from {len(frames)} frames - {used} usable "
            "(the rest unreadable, unfamiliar lighting, or wrong shape).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    previous = f"{model.band.top}..{model.band.bottom}" if model.band else "none"
    typer.echo(
        f"{camera}: band rows {derived.top}..{derived.bottom} "
        f"(was {previous}, from {used} of {len(frames)} blocked frames)"
    )
    if write:
        model.band = derived
        model.save(settings.references_dir)
        typer.echo(f"rewrote {camera}.json and {camera}.npz in {settings.references_dir}")


def _ensure_references(settings: Settings) -> None:
    """Pull reference models from S3 when the local directory lacks them.

    The models are built offline (today by hand, eventually by the nightly
    CronJob) and published to the bucket; every detector pod starts by syncing
    them down. Absent references are not fatal - the detector then answers
    UNKNOWN for that camera, which is honest and visible rather than a crash.
    """
    from blockade.storage import S3ObjectStore

    store = S3ObjectStore(settings)
    settings.references_dir.mkdir(parents=True, exist_ok=True)
    for key in store.list_keys("references/"):
        target = settings.references_dir / Path(key).name
        if not target.exists():
            target.write_bytes(store.get(key))
            log.info("fetched %s", key)


async def _stream(settings: Settings) -> None:
    from blockade.bus import RecordConsumer, RecordProducer
    from blockade.storage import S3ObjectStore

    store = S3ObjectStore(settings)
    cache = settings.local_cache_dir

    def read_frame(object_key: str) -> bytes | None:
        # Local cache first - free when this pod happens to share a volume with
        # capture - then S3. A key that is in the topic but not in the bucket is
        # logged and skipped, not raised: one lost frame must not stall the
        # partition behind it.
        path = cache / object_key
        if path.exists():
            return path.read_bytes()
        try:
            return store.get(object_key)
        except Exception:  # noqa: BLE001
            log.warning("frame bytes unavailable for %s", object_key)
            return None

    scorer = Scorer(settings, read_frame)
    assert settings.kafka_bootstrap is not None
    consumer = RecordConsumer(
        settings.kafka_bootstrap,
        settings.kafka_frames_topic,
        group_id=settings.kafka_group_id,
        client_id="blockade-detector",
    )
    producer = RecordProducer(settings.kafka_bootstrap, client_id="blockade-detector")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    await consumer.start()
    await producer.start()
    log.info(
        "streaming %s -> %s as group %s, detector %s",
        settings.kafka_frames_topic,
        settings.kafka_observations_topic,
        settings.kafka_group_id,
        scorer.detector.version,
    )
    try:
        while not stop.is_set():
            batch = await consumer.get_batch()
            if not batch:
                continue
            futures = []
            for message in batch:
                try:
                    record = FrameRecord.model_validate_json(message.value)
                except ValueError:
                    # A poison message blocks its partition forever if we crash
                    # on it. Log and move on; the bytes stay in the topic for
                    # inspection.
                    log.error(
                        "unparseable frame record at %s:%s", message.partition, message.offset
                    )
                    continue
                observation = scorer.score(record)
                if observation is None:
                    SKIPPED_FRAMES.labels(record.camera_id).inc()
                    continue
                SCORED.labels(observation.camera_id, observation.state.value).inc()
                futures.append(
                    await producer.send(
                        settings.kafka_observations_topic,
                        observation.crossing_id,
                        observation.model_dump_json().encode(),
                    )
                )
            # The at-least-once barrier: observations are durable in the broker
            # before the consumed offsets are committed. A crash between the two
            # re-scores this batch; deterministic identity absorbs the repeats.
            await RecordProducer.await_acks(futures)
            await consumer.commit()
    finally:
        await producer.stop()
        await consumer.stop()
    log.info("shutdown complete")


@app.command()
def spotcheck(
    frames_dir: Path = typer.Option(..., help="Root holding {camera_id}/**/*.jpg frames."),
    stride_minutes: int = typer.Option(15, help="Coarse sampling stride per camera."),
    camera: str = typer.Option("", help="Limit to one camera id."),
    labels: Path = typer.Option(Path("data/labels/labels.jsonl"), help="Label file to append to."),
) -> None:
    """Grow the label set: VLM spot-checks at a stride, walks blockage edges.

    Needs ANTHROPIC_API_KEY. Labels are appended with the model+prompt version
    as labeller, so machine labels never masquerade as human ones.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from blockade.detect.vlm import VlmDetector

    from detector.spotcheck import (
        Judged,
        append_labels,
        frame_time,
        label_record,
        list_frames,
        sweep_camera,
    )

    settings = get_settings()
    roster = [
        c
        for c in load_roster(settings.camera_config_path).enabled()
        if not camera or c.camera_id == camera
    ]
    vlm = VlmDetector(settings)

    already_labeled: set[str] = set()
    if labels.exists():
        for line in labels.read_text().splitlines():
            if line.strip():
                already_labeled.add(json.loads(line)["object_key"])

    total_added = total_calls = 0

    def judge(path: Path, cam: Camera) -> Judged:
        nonlocal total_calls
        total_calls += 1
        obs = vlm.classify(path.read_bytes(), cam, frame_time(path), path.name)
        return Judged(path, obs.captured_at, obs.state, obs.confidence, obs.reason)

    def object_key_for(path: Path, cam: Camera) -> str:
        return f"frames/{cam.camera_id}/{frame_time(path):%Y/%m/%d/%H}/{path.name}"

    for cam in roster:
        camera_dir = frames_dir / cam.camera_id
        frames = list_frames(camera_dir) if camera_dir.exists() else []
        if not frames:
            continue

        verdicts = sweep_camera(
            cam,
            frames,
            partial(judge, cam=cam),
            stride_minutes,
            already_labeled=already_labeled,
            object_key_for=partial(object_key_for, cam=cam),
        )
        records = [label_record(cam, v, object_key_for(v.path, cam), vlm.version) for v in verdicts]
        added = append_labels(records, labels)
        blocked = sum(1 for v in verdicts if v.state.value == "BLOCKED")
        typer.echo(
            f"{cam.camera_id}: {len(frames)} frames, {len(verdicts)} judged, "
            f"{blocked} blocked, {added} new labels"
        )
        total_added += added
    typer.echo(f"total: {total_calls} API calls, {total_added} labels added -> {labels}")


@app.command()
def run() -> None:
    """Consume frame metadata from Kafka and publish detections continuously.

    Crashes are allowed: this process is stateless, the consumer group holds
    its position server-side, and Kubernetes is the retry loop. That is the
    opposite trade from the poller, which retries in-process forever because
    a dead capture loop loses frames that cannot be recaptured.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    if not settings.kafka_bootstrap:
        typer.secho("BLOCKADE_KAFKA_BOOTSTRAP is not set.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    _ensure_references(settings)
    start_http_server(settings.metrics_port)
    asyncio.run(_stream(settings))


if __name__ == "__main__":
    app()
