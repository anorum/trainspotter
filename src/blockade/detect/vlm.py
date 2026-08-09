"""Classify a crossing frame with a vision model.

Replaces the ONNX-plus-hand-labelled-thresholds path in DESIGN.md section 3. That
design existed to compensate for a detector that cannot interpret a scene; a
vision model can, which removes the ROI editor, the labelling task, and the
threshold tuning along with it.

The single most important property here is calibration, not accuracy. This
produces the dataset, and a confidently wrong reading at 2am corrupts a record
that cannot be recaptured, while an honest UNKNOWN is merely a gap that coverage
reporting will describe truthfully. The prompt is written accordingly.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from datetime import UTC, datetime

import anthropic
from pydantic import BaseModel, Field

from blockade.config import Camera, Settings
from blockade.schemas import CrossingState, ObservationRecord

log = logging.getLogger(__name__)

# The frames are ~328x334 with roughly 90px of burned-in chrome (a title/timestamp
# header and a "Camera courtesy of PBOT" footer), leaving about 328x240 of usable
# roadway. The prompt has to say so, or the model reads the caption as scene.
PROMPT = """\
You are looking at a still frame from a City of Portland traffic camera pointed at a \
street near a Union Pacific rail line in inner SE Portland.

Your job is to judge whether the roadway is currently BLOCKED by a train.

Ignore the image chrome: the title bar across the top (camera name and timestamp) and \
the caption bar across the bottom are overlays, not part of the scene.

Answer BLOCKED if you can see any of:
- rail cars or a locomotive standing across the roadway
- crossing gates lowered across the traffic lanes
- a queue of stopped vehicles backed up toward the crossing with no movement through it

Answer CLEAR if the roadway through the crossing is open and passable, whether or not \
vehicles are present.

Answer UNKNOWN if you genuinely cannot tell. Darkness, headlight glare, rain, fog, a \
frozen or corrupted image, or a view that does not show the crossing are all good \
reasons to answer UNKNOWN.

UNKNOWN is a useful answer and is strongly preferred over a guess. These judgements \
accumulate into a public dataset about how often this crossing is blocked and for how \
long. A wrong confident answer corrupts that record; an UNKNOWN is simply recorded as \
a gap. When you are unsure, say UNKNOWN.

Set confidence to how sure you are of the state you chose, from 0.0 to 1.0. Give a \
reason of at most 15 words describing what you actually see."""

PROMPT_HASH = hashlib.sha256(PROMPT.encode()).hexdigest()[:8]


class _Judgement(BaseModel):
    """Schema the model fills in. Kept separate from ObservationRecord so the
    model is asked for judgement only, never for bookkeeping it cannot know."""

    state: CrossingState = Field(description="BLOCKED, CLEAR, or UNKNOWN.")
    confidence: float = Field(ge=0.0, le=1.0, description="Certainty in the chosen state.")
    reason: str = Field(max_length=200, description="At most 15 words on what is visible.")


def detector_version(model: str) -> str:
    """Identifies model and prompt together.

    Both change the judgement, so rows produced under different values are not
    directly comparable and a re-run must be distinguishable from the original.
    """
    return f"{model}/{PROMPT_HASH}"


class VlmDetector:
    """Classifies frames. One API call per frame."""

    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None) -> None:
        self._settings = settings
        self._model = settings.detector_model
        self._client = client or anthropic.Anthropic()
        self.version = detector_version(self._model)

    def classify(
        self,
        image: bytes,
        camera: Camera,
        captured_at: datetime,
        object_key: str,
    ) -> ObservationRecord:
        """Judge one frame. Never raises for an API failure.

        A failed call produces an UNKNOWN observation rather than an exception:
        the dataset should record that the crossing was unobserved at this
        instant, which is true and useful, rather than losing the tick entirely.
        """
        try:
            response = self._client.messages.parse(
                model=self._model,
                max_tokens=200,
                output_format=_Judgement,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": base64.standard_b64encode(image).decode(),
                                },
                            },
                            {"type": "text", "text": PROMPT},
                        ],
                    }
                ],
            )
            judgement = response.parsed_output
            if judgement is None:
                return self._unknown(camera, captured_at, object_key, "model returned no judgement")
            return self._record(camera, captured_at, object_key, judgement)

        except anthropic.APIStatusError as exc:
            log.warning("classification failed for %s: %s", object_key, exc)
            return self._unknown(camera, captured_at, object_key, f"api error {exc.status_code}")
        except (anthropic.APIConnectionError, anthropic.APIError) as exc:
            log.warning("classification failed for %s: %s", object_key, exc)
            return self._unknown(camera, captured_at, object_key, f"{type(exc).__name__}")

    def _record(
        self, camera: Camera, captured_at: datetime, object_key: str, judgement: _Judgement
    ) -> ObservationRecord:
        return ObservationRecord(
            crossing_id=camera.crossing_id,
            camera_id=camera.camera_id,
            captured_at=captured_at,
            observed_at=datetime.now(UTC),
            state=judgement.state,
            # An UNKNOWN carries no confidence in a state, only in the refusal.
            # Recording the model's self-reported number here would let a
            # "confident UNKNOWN" leak into statistics as though it were a
            # measurement.
            confidence=0.0 if judgement.state is CrossingState.UNKNOWN else judgement.confidence,
            reason=judgement.reason[:200],
            object_key=object_key,
            detector_version=self.version,
        )

    def _unknown(
        self, camera: Camera, captured_at: datetime, object_key: str, reason: str
    ) -> ObservationRecord:
        return ObservationRecord(
            crossing_id=camera.crossing_id,
            camera_id=camera.camera_id,
            captured_at=captured_at,
            observed_at=datetime.now(UTC),
            state=CrossingState.UNKNOWN,
            confidence=0.0,
            reason=reason[:200],
            object_key=object_key,
            detector_version=self.version,
        )
