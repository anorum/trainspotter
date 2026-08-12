"""Per-camera learned classifier: the detector that was trained on this view.

Runs the ONNX models the training script exports - one per camera, published
to the references prefix in S3 alongside the reference models and synced down
the same way. onnxruntime only at runtime: no torch, no GPU, ~10ms per frame
on a Pi.

A camera without a model answers UNKNOWN, the same honest-abstain contract as
an uncalibrated reference camera: a new camera contributes nothing until it is
trained, and says so, rather than guessing.
"""

from __future__ import annotations

import hashlib
import io
import logging
from datetime import UTC, datetime

import numpy as np

from blockade.config import Camera, Settings
from blockade.schemas import CrossingState, ObservationRecord

log = logging.getLogger(__name__)

IMAGE_SIZE = 224
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

MIN_CONFIDENCE = 0.7
"""Below this softmax probability the honest answer is UNKNOWN. The dataset
must not accumulate coin flips: a confidently wrong reading corrupts a record
that cannot be recaptured, while UNKNOWN is merely a gap."""

LABELS: tuple[str, str] = ("CLEAR", "BLOCKED")
"""Output index -> class name. Positive class is BLOCKED at index 1. Shared with
the trainer so a reordering on either side cannot silently invert the detector."""

_BLOCKED_INDEX = LABELS.index("BLOCKED")

_VERSION_BASE = "classifier/mobilenetv3s-v1"


class ClassifierDetector:
    """Blocked/clear via a per-camera ONNX model."""

    def __init__(self, settings: Settings) -> None:
        self._dir = settings.references_dir
        self._sessions: dict[str, tuple[object | None, str]] = {}
        self.version = f"{_VERSION_BASE}-c{MIN_CONFIDENCE}"

    def _session(self, camera_id: str) -> tuple[object | None, str]:
        if camera_id not in self._sessions:
            self._sessions[camera_id] = self._load(camera_id)
        return self._sessions[camera_id]

    def _load(self, camera_id: str) -> tuple[object | None, str]:
        path = self._dir / f"classifier-{camera_id}.onnx"
        if not path.exists():
            return None, self.version
        try:
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()[:12]
            version = f"{_VERSION_BASE}-c{MIN_CONFIDENCE}-h{digest}"
            import onnxruntime

            session = onnxruntime.InferenceSession(str(path), providers=["CPUExecutionProvider"])
            return session, version
        except Exception as exc:
            log.warning("classifier load failed for %s: %s", camera_id, exc)
            return None, self.version

    def classify(
        self, image: bytes, camera: Camera, captured_at: datetime, object_key: str
    ) -> ObservationRecord:
        session, version = self._session(camera.camera_id)
        if session is None:
            return self._record(
                camera,
                captured_at,
                object_key,
                CrossingState.UNKNOWN,
                0.0,
                "no classifier trained for this camera",
                version,
            )
        try:
            from PIL import Image

            with Image.open(io.BytesIO(image)) as img:
                rgb = img.convert("RGB").resize((IMAGE_SIZE, IMAGE_SIZE))
            x = (np.asarray(rgb, dtype=np.float32) / 255.0 - MEAN) / STD
            x = x.transpose(2, 0, 1)[None]
        except Exception as exc:
            return self._record(
                camera,
                captured_at,
                object_key,
                CrossingState.UNKNOWN,
                0.0,
                f"image could not be decoded: {type(exc).__name__}",
                version,
            )

        try:
            logits = session.run(["logits"], {"image": x})[0][0]
        except Exception as exc:
            log.warning("classifier inference failed for %s: %s", object_key, exc)
            return self._record(
                camera,
                captured_at,
                object_key,
                CrossingState.UNKNOWN,
                0.0,
                f"inference failed: {type(exc).__name__}",
                version,
            )
        exp = np.exp(logits - logits.max())
        probs = exp / exp.sum()
        blocked_p = float(probs[_BLOCKED_INDEX])

        if blocked_p >= MIN_CONFIDENCE:
            return self._record(
                camera,
                captured_at,
                object_key,
                CrossingState.BLOCKED,
                blocked_p,
                f"classifier: blocked p={blocked_p:.2f}",
                version,
            )
        if blocked_p <= 1 - MIN_CONFIDENCE:
            return self._record(
                camera,
                captured_at,
                object_key,
                CrossingState.CLEAR,
                1 - blocked_p,
                f"classifier: clear p={1 - blocked_p:.2f}",
                version,
            )
        return self._record(
            camera,
            captured_at,
            object_key,
            CrossingState.UNKNOWN,
            0.0,
            f"classifier undecided (blocked p={blocked_p:.2f})",
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
        version: str,
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
            detector_version=version,
        )
