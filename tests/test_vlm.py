"""Detector behaviour, especially how it handles not knowing.

These judgements are the dataset. The properties worth pinning are the ones that
keep it honest: an UNKNOWN must never look like a measurement, and an API outage
must never look like a clear crossing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from blockade.detect.vlm import PROMPT_HASH, VlmDetector, _Judgement, detector_version
from blockade.schemas import CrossingState

CAPTURED = datetime(2026, 8, 9, 14, 32, 7, tzinfo=UTC)
KEY = "frames/odot-678/2026/08/09/14/1786249267000-d4665634.jpg"


class FakeMessages:
    """Stands in for `client.messages`. The real API is exercised separately
    against live frames -- these tests pin behaviour, not the wire format."""

    def __init__(self, parsed=None, raises: Exception | None = None) -> None:
        self._parsed = parsed
        self._raises = raises
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace(parsed_output=self._parsed)


def build(settings, parsed=None, raises=None) -> tuple[VlmDetector, FakeMessages]:
    messages = FakeMessages(parsed, raises)
    client = SimpleNamespace(messages=messages)
    return VlmDetector(settings, client=client), messages


def test_blocked_judgement_is_recorded(settings, camera):
    detector, _ = build(
        settings,
        _Judgement(state=CrossingState.BLOCKED, confidence=0.92, reason="rail cars across roadway"),
    )

    obs = detector.classify(b"\xff\xd8jpeg", camera, CAPTURED, KEY)

    assert obs.state is CrossingState.BLOCKED
    assert obs.confidence == 0.92
    assert obs.crossing_id == camera.crossing_id
    assert obs.object_key == KEY
    assert obs.captured_at == CAPTURED
    assert obs.is_confident


def test_unknown_confidence_is_zeroed(settings, camera):
    """A model can report high confidence in not knowing. Carrying that number
    through would let 'confidently unsure' enter statistics as a measurement."""
    detector, _ = build(
        settings,
        _Judgement(state=CrossingState.UNKNOWN, confidence=0.95, reason="too dark to tell"),
    )

    obs = detector.classify(b"\xff\xd8jpeg", camera, CAPTURED, KEY)

    assert obs.state is CrossingState.UNKNOWN
    assert obs.confidence == 0.0
    assert not obs.is_confident


def test_api_error_becomes_unknown_not_an_exception(settings, camera):
    """An outage means the crossing was unobserved -- which is true, and worth
    recording. Raising would drop the tick and leave an unexplained hole."""
    detector, _ = build(
        settings,
        raises=anthropic.APIStatusError(
            "boom",
            response=httpx.Response(500, request=httpx.Request("POST", "https://api.anthropic.com")),
            body=None,
        ),
    )

    obs = detector.classify(b"\xff\xd8jpeg", camera, CAPTURED, KEY)

    assert obs.state is CrossingState.UNKNOWN
    assert not obs.is_confident
    assert "500" in obs.reason


def test_connection_error_becomes_unknown(settings, camera):
    detector, _ = build(
        settings, raises=anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
    )

    obs = detector.classify(b"\xff\xd8jpeg", camera, CAPTURED, KEY)

    assert obs.state is CrossingState.UNKNOWN
    assert "APIConnectionError" in obs.reason


def test_missing_parsed_output_becomes_unknown(settings, camera):
    detector, _ = build(settings, parsed=None)

    obs = detector.classify(b"\xff\xd8jpeg", camera, CAPTURED, KEY)

    assert obs.state is CrossingState.UNKNOWN


def test_detector_version_covers_model_and_prompt(settings):
    """Both change the judgement, so both must be visible in the row. Without
    this, re-running with a better prompt silently mixes incomparable rows."""
    version = detector_version("claude-haiku-4-5")

    assert version == f"claude-haiku-4-5/{PROMPT_HASH}"
    assert detector_version("claude-sonnet-5") != version


def test_image_is_sent_as_base64_jpeg(settings, camera):
    detector, messages = build(
        settings, _Judgement(state=CrossingState.CLEAR, confidence=0.8, reason="road clear")
    )

    detector.classify(b"\xff\xd8jpeg", camera, CAPTURED, KEY)

    content = messages.calls[0]["messages"][0]["content"]
    image_block = next(b for b in content if b["type"] == "image")
    assert image_block["source"]["media_type"] == "image/jpeg"
    assert image_block["source"]["type"] == "base64"


@pytest.mark.parametrize("state", [CrossingState.BLOCKED, CrossingState.CLEAR])
def test_confident_states_count_toward_coverage(settings, camera, state):
    detector, _ = build(settings, _Judgement(state=state, confidence=0.7, reason="x"))

    assert detector.classify(b"\xff\xd8jpeg", camera, CAPTURED, KEY).is_confident
