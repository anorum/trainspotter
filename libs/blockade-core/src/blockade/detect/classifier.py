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


def model_key(camera_id: str) -> str:
    """S3 key for a camera's classifier, next to its reference model."""
    return f"references/classifier-{camera_id}.onnx"


class ClassifierDetector:
    """Blocked/clear via a per-camera ONNX model."""

    def __init__(self, settings: Settings) -> None:
        self._dir = settings.references_dir
        self._sessions: dict[str, object] = {}
        self.version = "classifier/mobilenetv3s-v1"

    def _session(self, camera_id: str):
        if camera_id not in self._sessions:
            path = self._dir / f"classifier-{camera_id}.onnx"
            if not path.exists():
                self._sessions[camera_id] = None
            else:
                import onnxruntime

                self._sessions[camera_id] = onnxruntime.InferenceSession(
                    str(path), providers=["CPUExecutionProvider"]
                )
        return self._sessions[camera_id]

    def classify(
        self, image: bytes, camera: Camera, captured_at: datetime, object_key: str
    ) -> ObservationRecord:
        session = self._session(camera.camera_id)
        if session is None:
            return self._record(
                camera, captured_at, object_key, CrossingState.UNKNOWN, 0.0,
                "no classifier trained for this camera",
            )
        try:
            from PIL import Image

            with Image.open(io.BytesIO(image)) as img:
                rgb = img.convert("RGB").resize((IMAGE_SIZE, IMAGE_SIZE))
            x = (np.asarray(rgb, dtype=np.float32) / 255.0 - MEAN) / STD
            x = x.transpose(2, 0, 1)[None]
        except Exception as exc:
            return self._record(
                camera, captured_at, object_key, CrossingState.UNKNOWN, 0.0,
                f"image could not be decoded: {type(exc).__name__}",
            )

        logits = session.run(["logits"], {"image": x})[0][0]
        exp = np.exp(logits - logits.max())
        probs = exp / exp.sum()
        blocked_p = float(probs[1])

        if blocked_p >= MIN_CONFIDENCE:
            return self._record(
                camera, captured_at, object_key, CrossingState.BLOCKED, blocked_p,
                f"classifier: blocked p={blocked_p:.2f}",
            )
        if blocked_p <= 1 - MIN_CONFIDENCE:
            return self._record(
                camera, captured_at, object_key, CrossingState.CLEAR, 1 - blocked_p,
                f"classifier: clear p={1 - blocked_p:.2f}",
            )
        return self._record(
            camera, captured_at, object_key, CrossingState.UNKNOWN, 0.0,
            f"classifier undecided (blocked p={blocked_p:.2f})",
        )

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
