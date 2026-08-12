"""Free detector: compare each frame against what an empty crossing looks like.

The signal this exploits is the one the first observed blockage revealed - a
train is a long horizontal mass spanning most of the frame that hides the road
markings and the far side of the intersection behind it. That is enormous and
structural, so it does not need a learned model to find. It needs something to
compare against.

Two design problems, and the solutions are what make this work:

**A stopped train defeats frame-to-frame differencing.** After the first frame
it stops moving, so consecutive frames are identical and the difference vanishes
exactly when the blockage is longest. So the comparison is against a *reference*
image of the empty crossing, not against the previous frame.

**An adaptive background absorbs the thing you are looking for.** A background
model averaged over the last hour would quietly learn a 69-minute train as
scenery. So the reference is a median over a long pool of frames: the crossing
is empty the large majority of the time, so the median of a big enough sample is
the empty scene, and a blockage never dominates it.

Lighting is handled by bucketing the pool on frame brightness rather than on
clock time - no sunrise table, no timezone handling, and it adapts to overcast
days and street-lighting changes on its own.
"""

from __future__ import annotations

import io
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image

from blockade.config import Camera
from blockade.schemas import CrossingState, ObservationRecord

log = logging.getLogger(__name__)

# The frames carry a burned-in title bar and caption bar. Cropping them is not
# cosmetic: the header contains a clock that changes every frame, so leaving it
# in makes every comparison register a difference in the same place forever.
CHROME_TOP = 50
CHROME_BOTTOM = 44

BRIGHTNESS_BINS = 8
"""Frames are grouped into this many brightness levels, and compared only
against a reference built from similarly-lit frames."""


@dataclass(frozen=True)
class Thresholds:
    """Tuned against the labelled set. Kept as data so retuning does not mean
    editing logic, and so the values appear in `version`."""

    pixel_delta: int = 40
    """Floor for the per-pixel intensity change counted as 'different'. Applies
    where the scene is reliably static."""

    spread_multiple: float = 4.0
    """Restless pixels must change this many times their usual deviation to
    count. Stops moving shadows and swaying foliage from reading as an object,
    which is what a flat threshold cannot distinguish."""

    band_row_fraction: float = 0.45
    """A row counts as obstructed when this fraction of it differs from the
    reference. A train spans the frame; a car does not."""

    min_band_rows: int = 10
    """How many obstructed rows make a mass. Filters out a single vehicle or a
    shadow edge.

    Set from measurement, not intuition: across two days of odot-678 frames,
    no CLEAR frame exceeded 8 obstructed rows (p99 was 0), while a confirmed
    train that the original value of 12 missed scored 11. Ten sits two rows
    above the observed noise ceiling and safely below the weakest real train
    seen so far. Re-derive this margin whenever the reference model changes."""

    clear_max_changed: float = 0.10
    """Below this overall changed fraction the crossing is called clear."""

    min_luminance: int = 12
    """Below this mean brightness the frame carries no usable information and
    the honest answer is UNKNOWN rather than a guess."""


def _decode(image: bytes) -> np.ndarray | None:
    """JPEG bytes to a cropped grayscale array, or None if unreadable."""
    try:
        with Image.open(io.BytesIO(image)) as img:
            gray = np.asarray(img.convert("L"), dtype=np.uint8)
    except Exception:
        return None
    if gray.shape[0] <= CHROME_TOP + CHROME_BOTTOM:
        return None
    return gray[CHROME_TOP : gray.shape[0] - CHROME_BOTTOM, :]


BRIGHTNESS_PERCENTILE = 75
"""Which percentile of pixel values stands in for 'how lit is this scene'.

Not the mean, and the reason is a bug this cost us. A train is a large dark
object, so it drags the frame mean down -- enough to move a sunlit frame out of
its daylight bin and into a night one, where it was then compared against a
reference built from actual darkness. Everything differed, and the detector
called BLOCKED confidently for the wrong reason. Any dark cloud would have done
the same.

The conditioning variable must not be disturbed by the thing being detected. A
high percentile reflects the brightest part of the scene, which a train sitting
on the tracks does not cover, so it tracks real illumination and ignores the
object. Measured across a real blockage, the mean fell 135 -> 126 the moment the
train arrived while the 75th percentile rose smoothly with the approaching noon
sun, undisturbed.
"""

MAX_BIN_DISTANCE = 1
"""How far the nearest reference may be before it is unusable.

Falling back to any reference however distant is how a midday frame gets scored
against a morning one. Better to admit the lighting has never been seen.
"""


def _brightness_bin(scene: np.ndarray) -> int:
    level = float(np.percentile(scene, BRIGHTNESS_PERCENTILE))
    return min(int(level) * BRIGHTNESS_BINS // 256, BRIGHTNESS_BINS - 1)


class Reference:
    """What an empty crossing looks like at one lighting level, and how much it
    naturally varies.

    The variance half is what makes daylight usable. A single median is a poor
    description of a sunlit scene: over ten hours the sun moves, shadows sweep
    across the road, and trees shift, so any individual frame differs from the
    median in places that have nothing to do with a train. Recording per-pixel
    spread lets each pixel carry its own threshold - restless pixels (shadow
    edges, foliage, the traffic lanes) must change a lot to count, while
    reliably-static ones need only change a little.
    """

    def __init__(self, median: np.ndarray, spread: np.ndarray) -> None:
        self.median = median
        self.spread = spread


class ReferenceModel:
    """Empty-crossing references for one camera, one per brightness bin."""

    def __init__(
        self,
        camera_id: str,
        bins: dict[int, Reference],
        sample_counts: dict[int, int],
        band: TrackBand | None = None,
        thresholds: Thresholds | None = None,
    ):
        self.camera_id = camera_id
        self.bins = bins
        self.sample_counts = sample_counts
        self.band = band
        """None until this camera has been seen blocked. Until then the whole
        frame counts, which is the permissive setting -- a new camera should risk
        a false positive rather than silently miss its first blockage."""
        self.thresholds = thresholds
        """Per-camera calibration, or None for the global defaults. Cameras
        differ too much for one set of numbers - odot-681's dim night scenes
        need pixel_delta 20 where 678's brighter view wants 40 - and a
        hand-maintained table of exceptions is exactly what stops scaling.
        Calibrated from the camera's own labeled frames (maximize catches
        subject to zero false positives) and carried with the model, so
        calibration is a property of the data."""

    @classmethod
    def build_refined(
        cls,
        camera_id: str,
        frames: list[bytes],
        min_samples: int = 15,
        passes: int = 2,
        band: TrackBand | None = None,
    ) -> ReferenceModel:
        """Build references, then rebuild excluding frames that look blocked.

        The median tolerates a minority of blocked frames, but not an arbitrary
        one. Measured on real data, a single 70-minute blockage was 29% of one
        brightness bin -- enough to drag the median toward the train and suppress
        the very difference the detector looks for, so the same train scored 18
        rows instead of 70.

        Two passes fix it without any labelling: the first pass is contaminated
        but still good enough to identify the obvious blockages, and the second
        rebuilds from what is left. Self-correcting, so it stays true as the
        corpus grows and needs no human to maintain an exclusion list.

        When the camera has a known band, the refinement pass judges frames
        inside it - measured on real data, a bandless refinement failed to
        exclude a 69-minute night blockage from its brightness bin, and the
        rebuilt median absorbed the train the detector then could not see.
        The band also travels into the returned model, so a rebuild cannot
        silently discard what the camera had already learned.
        """
        model = cls.build(camera_id, frames, min_samples)
        model.band = band
        for _ in range(passes - 1):
            if not model.bins:
                return model
            detector = ReferenceDetector({camera_id: model})
            keep = [
                raw
                for raw in frames
                if (scene := _decode(raw)) is not None
                and (ref := model.nearest(_brightness_bin(scene))) is not None
                and detector.examine(scene, ref, band).band_rows
                < detector.thresholds.min_band_rows
            ]
            # Refuse to rebuild from a pool that has collapsed -- if most frames
            # look blocked, the references are wrong, not the crossing.
            if len(keep) < max(min_samples, len(frames) // 2):
                log.warning(
                    "%s: refinement would drop %d of %d frames; keeping first-pass model",
                    camera_id, len(frames) - len(keep), len(frames),
                )
                return model
            model = cls.build(camera_id, keep, min_samples)
            model.band = band
        return model

    @classmethod
    def build(cls, camera_id: str, frames: list[bytes], min_samples: int = 15) -> ReferenceModel:
        """Build references from a pool of frames.

        Bins with too few samples are dropped rather than kept: a median over
        five frames is not a reliable picture of an empty crossing, and a bad
        reference produces confident wrong answers, which is the failure mode
        this project can least afford.
        """
        buckets: dict[int, list[np.ndarray]] = {}
        for raw in frames:
            scene = _decode(raw)
            if scene is None:
                continue
            buckets.setdefault(_brightness_bin(scene), []).append(scene)

        bins, counts = {}, {}
        for level, group in buckets.items():
            if len(group) < min_samples:
                log.info("bin %d for %s has only %d frames; skipping", level, camera_id, len(group))
                continue
            stack = np.stack(group).astype(np.float32)
            median = np.median(stack, axis=0)
            # Median absolute deviation, not standard deviation: a blockage
            # sitting in the pool would inflate a standard deviation and quietly
            # raise the bar exactly where trains appear. MAD ignores it.
            spread = np.median(np.abs(stack - median), axis=0)
            bins[level] = Reference(median.astype(np.uint8), spread.astype(np.float32))
            counts[level] = len(group)
        return cls(camera_id, bins, counts)

    def nearest(self, level: int) -> Reference | None:
        """Reference for this brightness, or None if none is close enough.

        Returning a distant reference rather than nothing is what let a midday
        frame be scored against a morning one. An unfamiliar lighting condition
        is a gap in coverage, which the dataset records honestly; a comparison
        against the wrong reference is a fabricated observation, which it cannot.
        """
        if not self.bins:
            return None
        closest = min(self.bins, key=lambda b: abs(b - level))
        if abs(closest - level) > MAX_BIN_DISTANCE:
            return None
        return self.bins[closest]

    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        payload: dict[str, np.ndarray] = {}
        for level, ref in self.bins.items():
            payload[f"median_{level}"] = ref.median
            payload[f"spread_{level}"] = ref.spread
        np.savez_compressed(directory / f"{self.camera_id}.npz", **payload)
        meta = {"camera_id": self.camera_id, "samples": self.sample_counts}
        if self.band is not None:
            meta["band"] = {"top": self.band.top, "bottom": self.band.bottom}
        if self.thresholds is not None:
            meta["thresholds"] = asdict(self.thresholds)
        (directory / f"{self.camera_id}.json").write_text(json.dumps(meta, indent=2))

    @classmethod
    def load(cls, directory: Path, camera_id: str) -> ReferenceModel | None:
        path = directory / f"{camera_id}.npz"
        if not path.exists():
            return None
        with np.load(path) as data:
            levels = {int(k.split("_")[1]) for k in data.files if k.startswith("median_")}
            bins = {lv: Reference(data[f"median_{lv}"], data[f"spread_{lv}"]) for lv in levels}
        meta = json.loads((directory / f"{camera_id}.json").read_text()) if (
            directory / f"{camera_id}.json"
        ).exists() else {}
        counts = meta.get("samples", {})
        raw_band = meta.get("band")
        band = TrackBand(raw_band["top"], raw_band["bottom"]) if raw_band else None
        raw_thresholds = meta.get("thresholds")
        thresholds = Thresholds(**raw_thresholds) if raw_thresholds else None
        return cls(
            camera_id, bins, {int(k): v for k, v in counts.items()}, band, thresholds
        )


@dataclass(frozen=True)
class TrackBand:
    """The rows where a train can appear, for one camera.

    Trains are always at track level, so *where* an obstruction sits is a far
    better discriminator than how tall it is. Measured across one real blockage,
    the obstructed height varied fourfold - 17 rows for low-profile well cars,
    72 for loaded double-stacks - while the position stayed put. Counting rows
    anywhere in the frame therefore risks two failures at once: missing an empty
    flatcar, and counting road traffic as a train.

    Restricting the count to this band keeps the row threshold low enough for the
    shortest consist without letting anything below the tracks contribute.
    """

    top: int
    bottom: int

    def mask(self, rows: np.ndarray) -> np.ndarray:
        """Zero out everything outside the band."""
        masked = np.zeros_like(rows)
        masked[self.top : self.bottom] = rows[self.top : self.bottom]
        return masked


def derive_band(
    row_profiles: list[np.ndarray], min_support: float = 0.5, pad: int = 6
) -> TrackBand | None:
    """Infer the track band from the obstruction profiles of blocked frames.

    A row belongs to the band if it is obstructed in at least `min_support` of
    them. That keeps rows where trains actually sit across differing car
    profiles, and drops one-off obstructions such as a vehicle stopped below the
    tracks, which appear in only a frame or two.

    Derived rather than hand-drawn: DESIGN.md called for ROI polygons clicked out
    per camera, which does not survive more cameras. This is the same idea
    obtained from data, so a new camera acquires its band once it has seen a
    blockage and needs no human.
    """
    if not row_profiles:
        return None
    support = np.mean(np.stack(row_profiles), axis=0)
    rows = np.flatnonzero(support >= min_support)
    if rows.size == 0:
        return None
    top = max(0, int(rows.min()) - pad)
    bottom = min(len(support), int(rows.max()) + pad + 1)
    return TrackBand(top, bottom)


@dataclass
class Evidence:
    """What the comparison found. Returned alongside the judgement so a row can
    be explained later without re-running anything."""

    changed_fraction: float
    band_rows: int


def _version_for(t: Thresholds) -> str:
    return (
        f"reference/d{t.pixel_delta}-r{t.band_row_fraction}-"
        f"b{t.min_band_rows}-c{t.clear_max_changed}"
    )


class ReferenceDetector:
    """Judges frames by comparing them to an empty-crossing reference."""

    def __init__(
        self,
        models: dict[str, ReferenceModel],
        thresholds: Thresholds | None = None,
    ) -> None:
        self._models = models
        self.thresholds = thresholds or Thresholds()
        self.version = _version_for(self.thresholds)

    def examine(
        self,
        scene: np.ndarray,
        reference: Reference,
        band: TrackBand | None = None,
        thresholds: Thresholds | None = None,
    ) -> Evidence:
        t = thresholds or self.thresholds
        diff = np.abs(scene.astype(np.float32) - reference.median.astype(np.float32))
        # Each pixel is judged against its own history: the flat floor where the
        # scene is dependable, a multiple of its usual deviation where it is not.
        bar = np.maximum(t.pixel_delta, t.spread_multiple * reference.spread)
        changed = diff > bar
        row_coverage = changed.mean(axis=1)
        obstructed = row_coverage > t.band_row_fraction
        if band is not None:
            # Only obstruction at track level counts. Everything below the rails
            # is road traffic, and counting it is how a bus becomes a train.
            obstructed = band.mask(obstructed)
        return Evidence(
            changed_fraction=float(changed.mean()),
            band_rows=int(obstructed.sum()),
        )

    def classify(
        self, image: bytes, camera: Camera, captured_at: datetime, object_key: str
    ) -> ObservationRecord:
        scene = _decode(image)
        if scene is None:
            return self._record(camera, captured_at, object_key, CrossingState.UNKNOWN, 0.0,
                                "image could not be decoded")

        model = self._models.get(camera.camera_id)
        # The model's own calibration wins over the global defaults: cameras
        # differ too much for one set of numbers, and the calibration was
        # measured on this camera's labeled frames.
        t = (model.thresholds if model and model.thresholds else None) or self.thresholds
        version = _version_for(t)
        if scene.mean() < t.min_luminance:
            return self._record(camera, captured_at, object_key, CrossingState.UNKNOWN, 0.0,
                                f"frame too dark to judge (luminance {scene.mean():.0f})",
                                version)

        reference = model.nearest(_brightness_bin(scene)) if model else None
        if reference is None:
            return self._record(camera, captured_at, object_key, CrossingState.UNKNOWN, 0.0,
                                "no reference image for this camera and lighting",
                                version)
        if reference.median.shape != scene.shape:
            return self._record(camera, captured_at, object_key, CrossingState.UNKNOWN, 0.0,
                                "frame size differs from reference",
                                version)

        ev = self.examine(scene, reference, model.band, t)

        if ev.band_rows >= t.min_band_rows:
            # Confidence scales with how far past the bar it is, so a marginal
            # mass and an unmistakable one are not recorded as equally certain.
            confidence = min(0.95, 0.6 + 0.35 * min(1.0, ev.band_rows / (t.min_band_rows * 3)))
            return self._record(
                camera, captured_at, object_key, CrossingState.BLOCKED, confidence,
                f"mass spans {ev.band_rows} rows, {ev.changed_fraction:.0%} of frame differs",
                version,
            )

        if ev.changed_fraction <= t.clear_max_changed:
            return self._record(
                camera, captured_at, object_key, CrossingState.CLEAR, 0.85,
                f"matches empty reference ({ev.changed_fraction:.0%} differs)",
                version,
            )

        # Something is there, but it is not a frame-spanning mass - most likely
        # vehicles, weather, or a lighting shift. Not a train, and not clearly
        # nothing, so the honest answer is that we do not know.
        return self._record(
            camera, captured_at, object_key, CrossingState.UNKNOWN, 0.0,
            f"{ev.changed_fraction:.0%} differs but no spanning mass ({ev.band_rows} rows)",
            version,
        )

    def _record(
        self,
        camera: Camera,
        captured_at: datetime,
        object_key: str,
        state: CrossingState,
        confidence: float,
        reason: str,
        version: str | None = None,
    ) -> ObservationRecord:
        return ObservationRecord(
            crossing_id=camera.crossing_id,
            camera_id=camera.camera_id,
            captured_at=captured_at,
            observed_at=datetime.now(UTC),
            state=state,
            confidence=0.0 if state is CrossingState.UNKNOWN else confidence,
            reason=reason[:200],
            object_key=object_key,
            detector_version=version or self.version,
        )
