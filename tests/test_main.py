"""The single-image CLI, exercised against real camera frames.

The frames are committed fixtures rather than paths into `var/`, which is
gitignored. The original test read a frame that existed only on the machine that
wrote it, so it passed locally and failed in CI - a test that cannot run
everywhere is worse than no test, because it reports green where nothing ran.

The reference model is built in-test instead of committed. A real one is ~490KB
per camera and would need regenerating whenever the format changed; building it
from the fixture keeps the repo small and the test honest about what it covers.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from blockade.detect.reference import ReferenceModel
from blockade.schemas import CrossingState

from main import classify_image

FIXTURES = Path(__file__).parent / "fixtures" / "frames"
CLEAR_NIGHT = FIXTURES / "odot-678" / "clear-night.jpg"
BLOCKED_NIGHT = FIXTURES / "odot-678" / "blocked-night.jpg"


@pytest.fixture
def reference_dir(tmp_path: Path) -> Path:
    """A reference model for odot-678 built from the known-clear fixture.

    Twelve copies clear the min_samples floor. They are identical, so the
    per-pixel spread is zero and every pixel falls back to the flat threshold -
    which is the strictest the detector ever gets, and exactly right for
    asserting that a frame matches itself.
    """
    frames = [CLEAR_NIGHT.read_bytes()] * 12
    ReferenceModel.build("odot-678", frames, min_samples=10).save(tmp_path)
    return tmp_path


def test_a_frame_matching_its_own_reference_reads_clear(reference_dir: Path) -> None:
    observation = classify_image(image_path=CLEAR_NIGHT, reference_dir=reference_dir)

    assert observation.camera_id == "odot-678"
    assert observation.state is CrossingState.CLEAR


def test_the_night_train_is_detected(reference_dir: Path) -> None:
    """The real 23:51-01:05 blockage, scored against a clear frame from ten
    minutes before it arrived. This is the one end-to-end check on real imagery:
    the same pair of frames a human confirmed as clear and then blocked."""
    observation = classify_image(image_path=BLOCKED_NIGHT, reference_dir=reference_dir)

    assert observation.state is CrossingState.BLOCKED
    assert observation.confidence > 0.5


def test_camera_id_is_inferred_from_the_path(reference_dir: Path) -> None:
    observation = classify_image(image_path=CLEAR_NIGHT, reference_dir=reference_dir)

    assert observation.camera_id == "odot-678"
    assert observation.object_key.endswith(CLEAR_NIGHT.name)


def test_an_unreadable_image_is_unknown(tmp_path: Path, reference_dir: Path) -> None:
    """Never raise on a bad frame. The crossing was unobserved, which is true and
    worth recording; raising would drop the tick entirely."""
    camera_dir = tmp_path / "odot-678"
    camera_dir.mkdir()
    broken = camera_dir / "broken.jpg"
    broken.write_bytes(b"not a jpeg")

    observation = classify_image(image_path=broken, reference_dir=reference_dir)

    assert observation.state is CrossingState.UNKNOWN


def test_no_reference_for_the_camera_is_unknown(tmp_path: Path) -> None:
    """An uncalibrated camera must abstain rather than guess. This is the state
    every newly added camera is in until the nightly builder has seen it."""
    empty = tmp_path / "refs"
    empty.mkdir()

    observation = classify_image(image_path=CLEAR_NIGHT, reference_dir=empty)

    assert observation.state is CrossingState.UNKNOWN
