"""The interface both detectors implement.

Two exist deliberately. `reference.ReferenceDetector` is free, local, and free to
re-run over the whole corpus, which is what makes re-deriving the dataset at a
finer resolution cost nothing. `vlm.VlmDetector` costs money per frame but reads
a scene rather than a difference, and is there for when accuracy is worth paying
for.

Keeping them behind one signature means they can be scored against the same
label set and swapped by configuration rather than by editing the runner.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from blockade.config import Camera
from blockade.schemas import ObservationRecord


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
