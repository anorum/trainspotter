"""YOLO-World detector: open-vocabulary object detection, no training required.

DESIGN2 section 4 makes fine-tuned YOLO the endpoint and notes open-vocab is
"good enough" zero-shot. That ordering is right, because fine-tuning needs a few
thousand labelled frames and the pipeline is what produces them. YOLO-World
fills the gap: it takes text prompts instead of labels, so it runs today.

The geometric sanity check is what makes it usable rather than merely plausible.
On a fixed camera the tracks never move, so a box that does not overlap the
track band is not a train however confident the model is -- DESIGN2 section 4
calls this a free, powerful false-positive killer, and it is the same TrackBand
already derived for reference differencing.

Confidence thresholds are deliberately low. Open-vocab models score
domain-mismatched targets poorly, and a grainy 328x240 traffic-camera still at
night is about as mismatched as it gets. Filtering on geometry rather than score
is what keeps recall without inviting the whole frame in.
"""

from __future__ import annotations

import io
import logging
from datetime import UTC, datetime

import numpy as np

from blockade.config import Camera, Settings
from blockade.detect.reference import CHROME_BOTTOM, CHROME_TOP, ReferenceModel, TrackBand
from blockade.schemas import CrossingState, ObservationRecord

log = logging.getLogger(__name__)


class YoloWorldDetector:
    """Open-vocabulary detection, constrained to the track band."""

    def __init__(self, settings: Settings, model=None) -> None:
        self._settings = settings
        self._prompts = list(settings.yolo_prompts)
        self._confidence = settings.yolo_confidence
        self._model = model
        self._bands: dict[str, TrackBand | None] = {}
        self.version = (
            f"yolo-world/{settings.yolo_weights}"
            f"-c{settings.yolo_confidence}-p{len(self._prompts)}"
        )

    def _load(self):
        """Deferred so that importing this module does not pull in torch."""
        if self._model is None:
            from ultralytics import YOLOWorld

            self._model = YOLOWorld(self._settings.yolo_weights)
            self._model.set_classes(self._prompts)
        return self._model

    def _band(self, camera_id: str) -> TrackBand | None:
        """The track band for this camera, reused from reference differencing.

        Shared deliberately: the band is a property of where the camera points,
        not of how a given detector works, so every detector should agree on it.
        """
        if camera_id not in self._bands:
            model = ReferenceModel.load(self._settings.references_dir, camera_id)
            self._bands[camera_id] = model.band if model else None
        return self._bands[camera_id]

    def classify(
        self, image: bytes, camera: Camera, captured_at: datetime, object_key: str
    ) -> ObservationRecord:
        try:
            from PIL import Image

            with Image.open(io.BytesIO(image)) as img:
                full = img.convert("RGB")
                cropped = full.crop((0, CHROME_TOP, full.width, full.height - CHROME_BOTTOM))
        except Exception as exc:
            return self._record(
                camera, captured_at, object_key, CrossingState.UNKNOWN, 0.0,
                f"image could not be decoded: {type(exc).__name__}",
            )

        try:
            results = self._load().predict(
                np.asarray(cropped), conf=self._confidence, verbose=False
            )
        except Exception as exc:
            # An inference failure means the crossing was unobserved, which is
            # true and worth recording. Dropping the tick would leave a hole
            # indistinguishable from a capture outage.
            log.warning("yolo inference failed for %s: %s", object_key, exc)
            return self._record(
                camera, captured_at, object_key, CrossingState.UNKNOWN, 0.0,
                f"inference failed: {type(exc).__name__}",
            )

        boxes = self._boxes(results)
        band = self._band(camera.camera_id)
        on_track = [b for b in boxes if _overlaps(b, band)]

        if on_track:
            best = max(on_track, key=lambda b: b[4])
            return self._record(
                camera, captured_at, object_key, CrossingState.BLOCKED, float(best[4]),
                f"{len(on_track)} detection(s) on track, best {best[4]:.2f}",
            )

        if boxes:
            # Something was found, but not where a train can be. Reporting CLEAR
            # is the right call and the geometric check is the reason it is safe.
            return self._record(
                camera, captured_at, object_key, CrossingState.CLEAR, 0.8,
                f"{len(boxes)} detection(s), none on track",
            )

        return self._record(
            camera, captured_at, object_key, CrossingState.CLEAR, 0.85, "no detections"
        )

    @staticmethod
    def _boxes(results) -> list[tuple[float, float, float, float, float]]:
        out: list[tuple[float, float, float, float, float]] = []
        for result in results:
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            for xyxy, conf in zip(
                np.asarray(boxes.xyxy), np.asarray(boxes.conf), strict=False
            ):
                x1, y1, x2, y2 = (float(v) for v in xyxy[:4])
                out.append((x1, y1, x2, y2, float(conf)))
        return out

    def _record(
        self,
        camera: Camera,
        captured_at: datetime,
        object_key: str,
        state: CrossingState,
        confidence: float,
        reason: str,
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
            detector_version=self.version,
        )


def _overlaps(box: tuple[float, float, float, float, float], band: TrackBand | None) -> bool:
    """Whether a detection sits where a train can be.

    With no band derived yet -- a camera that has never been seen blocked -- every
    detection counts. That is the permissive setting on purpose: a new camera
    should risk a false positive rather than silently miss its first blockage,
    which is also what earns it a band.
    """
    if band is None:
        return True
    _, y1, _, y2, _ = box
    return y1 < band.bottom and y2 > band.top
