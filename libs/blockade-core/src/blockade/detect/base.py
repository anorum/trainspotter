"""The interface every detector implements.

Several exist deliberately: free local differencing, per-camera trained
classifiers, a paid VLM, and the auto router that mixes them. Which is best is
an empirical question the label set keeps re-answering, so keeping them behind
one signature means they can be scored against the same labels and swapped by
configuration rather than by editing the runner.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from blockade.config import Camera
from blockade.schemas import CrossingState, ObservationRecord


def observation(
    camera: Camera,
    captured_at: datetime,
    object_key: str,
    *,
    state: CrossingState,
    confidence: float,
    reason: str,
    version: str,
) -> ObservationRecord:
    """The one way any detector mints a record.

    Single-sources two invariants every implementation must share: an UNKNOWN
    carries no confidence in a state, only in the refusal - recording a
    model's self-reported number there would let a "confident UNKNOWN" leak
    into statistics as though it were a measurement - and the reason line is
    capped so a chatty model cannot bloat every row.
    """
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


class Detector(Protocol):
    """Turns a frame into a judgement about the crossing."""

    version: str
    """Identifies the detector and its parameters. Recorded on every row so
    observations produced by different detectors are never silently mixed."""

    def classify(
        self,
        image: bytes,
        camera: Camera,
        captured_at: datetime,
        object_key: str,
    ) -> ObservationRecord:
        """Judge one frame.

        Must not raise. A detector that cannot reach its model, or cannot read
        the image, returns UNKNOWN - the crossing genuinely was unobserved, and
        recording that is truthful where dropping the tick leaves a hole
        indistinguishable from a capture outage.
        """
        ...
