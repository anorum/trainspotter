"""Reference-differencing detector.

The properties pinned here are the ones the corpus run depends on: a frame that
matches its reference is CLEAR, a frame-spanning mass is BLOCKED, and anything
the model cannot fairly judge is UNKNOWN rather than a guess.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime

import numpy as np
import pytest
from PIL import Image

from blockade.detect.reference import (
    CHROME_BOTTOM,
    CHROME_TOP,
    ReferenceDetector,
    ReferenceModel,
    Thresholds,
)
from blockade.schemas import CrossingState

CAPTURED = datetime(2026, 8, 9, 7, 23, 10, tzinfo=UTC)
KEY = "frames/odot-678/2026/08/09/07/x.jpg"
SIZE = (334, 328)  # height, width -- matches the real frames


def frame(scene: np.ndarray) -> bytes:
    """Wrap a scene array in chrome bands and encode as JPEG, like a real frame."""
    full = np.full(SIZE, 30, dtype=np.uint8)
    full[CHROME_TOP : SIZE[0] - CHROME_BOTTOM, :] = scene
    buf = io.BytesIO()
    Image.fromarray(full, mode="L").save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def empty_scene(value: int = 120) -> np.ndarray:
    """A quiet crossing: mostly uniform with a little fixed texture."""
    h = SIZE[0] - CHROME_TOP - CHROME_BOTTOM
    scene = np.full((h, SIZE[1]), value, dtype=np.uint8)
    scene[::7, :] = value - 12  # static road markings
    return scene


def build_model(camera_id="odot-1234", n=40, value=120) -> ReferenceModel:
    rng = np.random.default_rng(0)
    frames = []
    for _ in range(n):
        scene = empty_scene(value).astype(np.int16)
        scene += rng.integers(-3, 4, scene.shape)  # sensor noise
        frames.append(frame(np.clip(scene, 0, 255).astype(np.uint8)))
    return ReferenceModel.build(camera_id, frames)


@pytest.fixture
def detector() -> ReferenceDetector:
    return ReferenceDetector({"odot-1234": build_model()})


def test_empty_frame_reads_clear(detector, camera):
    obs = detector.classify(frame(empty_scene()), camera, CAPTURED, KEY)

    assert obs.state is CrossingState.CLEAR
    assert obs.is_confident


def test_frame_spanning_mass_reads_blocked(detector, camera):
    """The train signature: a wide horizontal band hiding the scene behind it."""
    scene = empty_scene()
    scene[60:130, :] = 20  # dark mass spanning the full width

    obs = detector.classify(frame(scene), camera, CAPTURED, KEY)

    assert obs.state is CrossingState.BLOCKED
    assert "spans" in obs.reason


def test_single_vehicle_does_not_read_blocked(detector, camera):
    """A car covers a small patch, not a span. Calling this BLOCKED is how the
    dataset would fill with phantom blockages."""
    scene = empty_scene()
    scene[80:100, 40:90] = 20  # small dark blob

    obs = detector.classify(frame(scene), camera, CAPTURED, KEY)

    assert obs.state is not CrossingState.BLOCKED


def test_dark_frame_is_unknown_not_clear(detector, camera):
    """An unlit frame carries no information. Reading it as CLEAR would invent
    observations for hours the camera could not see."""
    obs = detector.classify(frame(np.full_like(empty_scene(), 3)), camera, CAPTURED, KEY)

    assert obs.state is CrossingState.UNKNOWN
    assert obs.confidence == 0.0


def test_missing_reference_is_unknown(camera):
    detector = ReferenceDetector({})

    obs = detector.classify(frame(empty_scene()), camera, CAPTURED, KEY)

    assert obs.state is CrossingState.UNKNOWN
    assert "no reference" in obs.reason


def test_undecodable_image_is_unknown(detector, camera):
    obs = detector.classify(b"not a jpeg", camera, CAPTURED, KEY)

    assert obs.state is CrossingState.UNKNOWN


def test_restless_pixels_need_a_bigger_change(camera):
    """Where the scene naturally varies -- moving shadows, foliage -- the bar
    rises. This is what stopped a sunlit daylight frame reading as a train."""
    rng = np.random.default_rng(1)
    frames = []
    for _ in range(40):
        scene = empty_scene().astype(np.int16)
        scene[60:130, :] += rng.integers(-45, 46)  # that band swings wildly
        frames.append(frame(np.clip(scene, 0, 255).astype(np.uint8)))
    detector = ReferenceDetector({"odot-1234": ReferenceModel.build("odot-1234", frames)})

    swung = empty_scene().astype(np.int16)
    swung[60:130, :] += 44  # within the band's normal range
    obs = detector.classify(
        frame(np.clip(swung, 0, 255).astype(np.uint8)), camera, CAPTURED, KEY
    )

    assert obs.state is not CrossingState.BLOCKED


def test_version_records_thresholds(camera):
    """Thresholds change the judgement, so rows made under different ones must
    be distinguishable in the dataset."""
    a = ReferenceDetector({}, Thresholds(min_band_rows=12)).version
    b = ReferenceDetector({}, Thresholds(min_band_rows=20)).version

    assert a != b
    assert a.startswith("reference/")


def test_model_round_trips_through_disk(tmp_path):
    model = build_model()
    model.save(tmp_path)

    loaded = ReferenceModel.load(tmp_path, "odot-1234")

    assert loaded is not None
    assert set(loaded.bins) == set(model.bins)
    for level, ref in model.bins.items():
        assert np.array_equal(loaded.bins[level].median, ref.median)
        assert np.allclose(loaded.bins[level].spread, ref.spread)


def test_thin_bins_are_dropped():
    """A median over a handful of frames is not a picture of an empty crossing,
    and a bad reference produces confident wrong answers."""
    model = ReferenceModel.build("odot-1234", [frame(empty_scene())] * 5, min_samples=15)

    assert model.bins == {}
