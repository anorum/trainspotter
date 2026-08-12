"""Reference-differencing detector.

The properties pinned here are the ones the corpus run depends on: a frame that
matches its reference is CLEAR, a frame-spanning mass is BLOCKED, and anything
the model cannot fairly judge is UNKNOWN rather than a guess.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from blockade.config import Camera
from blockade.detect.reference import (
    CHROME_BOTTOM,
    CHROME_TOP,
    ReferenceDetector,
    ReferenceModel,
    Thresholds,
    _brightness_bin,
    _decode,
)
from blockade.schemas import CrossingState
from PIL import Image

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
        # A narrow restless strip: wide enough to be a "mass" if it counted,
        # narrow enough not to move the frame's own brightness percentile.
        scene[60:78, :] += rng.integers(-45, 46)
        frames.append(frame(np.clip(scene, 0, 255).astype(np.uint8)))
    detector = ReferenceDetector({"odot-1234": ReferenceModel.build("odot-1234", frames)})

    swung = empty_scene().astype(np.int16)
    swung[60:78, :] += 44  # within the strip's normal range
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


def blocked_scene(value: int = 62) -> np.ndarray:
    """An empty crossing with a frame-spanning mass across it.

    Deliberately low-contrast (delta ~58 against a 40 floor): a blatant mass is
    found either way, so it cannot show what contamination costs.
    """
    scene = empty_scene()
    scene[60:130, :] = value
    return scene


def test_refinement_removes_blocked_frames_from_the_pool(camera):
    """A long blockage can be a large minority of one brightness bin -- measured
    at 29% of one bin on real data. The median tolerates that, but the pool is
    still describing the crossing partly as "train present", and rebuilding
    without those frames measurably changed detection on the live event
    (17 rows -> 70 for the same train).

    What is asserted here is the mechanism actually verifiable in isolation: the
    blocked frames leave the pool, and detection still works afterwards.
    """
    rng = np.random.default_rng(2)
    frames = []
    for i in range(60):
        scene = (blocked_scene() if i < 20 else empty_scene()).astype(np.int16)
        scene += rng.integers(-3, 4, scene.shape)
        frames.append(frame(np.clip(scene, 0, 255).astype(np.uint8)))

    naive = ReferenceModel.build("odot-1234", frames)
    refined = ReferenceModel.build_refined("odot-1234", frames)

    assert sum(refined.sample_counts.values()) < sum(naive.sample_counts.values())
    assert sum(refined.sample_counts.values()) >= 35, "the clear frames must survive"
    detector = ReferenceDetector({"odot-1234": refined})
    assert detector.classify(frame(blocked_scene()), camera, CAPTURED, KEY).state is (
        CrossingState.BLOCKED
    )
    assert detector.classify(frame(empty_scene()), camera, CAPTURED, KEY).state is (
        CrossingState.CLEAR
    )


def test_refinement_gives_up_rather_than_collapsing(camera):
    """If most frames look blocked the references are wrong, not the crossing.
    Rebuilding from the remainder would lock in that mistake."""
    frames = [frame(blocked_scene())] * 40 + [frame(empty_scene())] * 5

    model = ReferenceModel.build_refined("odot-1234", frames)

    assert model.bins, "must keep the first-pass model rather than emptying itself"


def test_brightness_bin_ignores_a_large_dark_object(camera):
    """The conditioning variable must not move when a train arrives. Using the
    mean, a train dropped a sunlit frame into the night bin and it was scored
    against darkness -- everything differed and it read BLOCKED for the wrong
    reason, as would any dark cloud."""
    empty = _decode(frame(empty_scene()))
    blocked = _decode(frame(blocked_scene()))

    assert blocked.mean() < empty.mean() - 15, "the mass must genuinely darken the frame"
    assert _brightness_bin(blocked) == _brightness_bin(empty)


def test_distant_reference_is_refused(camera):
    """Falling back to any reference however far away is how a midday frame gets
    scored against a morning one. Unfamiliar lighting is a coverage gap, not a
    licence to guess."""
    model = build_model(value=120)
    far = ReferenceDetector({"odot-1234": model})

    obs = far.classify(frame(np.full_like(empty_scene(), 250)), camera, CAPTURED, KEY)

    assert obs.state is CrossingState.UNKNOWN
    assert "no reference" in obs.reason


def test_model_thresholds_round_trip_and_win_over_defaults(tmp_path):
    """Per-camera calibration is a property of the model: it survives save/load,
    the detector prefers it over the global defaults, and each observation's
    detector_version names the calibration that actually judged it."""
    from blockade.detect.reference import (
        ReferenceDetector,
        ReferenceModel,
        Thresholds,
        TrackBand,
    )

    calibrated = Thresholds(pixel_delta=20, spread_multiple=2.0, band_row_fraction=0.35)
    model = build_model()
    model.band = TrackBand(6, 95)
    model.thresholds = calibrated
    model.save(tmp_path)

    loaded = ReferenceModel.load(tmp_path, model.camera_id)
    assert loaded.thresholds == calibrated
    assert loaded.band == TrackBand(6, 95)

    camera = Camera(
        camera_id=model.camera_id, name="t", crossing_id="X", image_url="http://x"
    )
    detector = ReferenceDetector({model.camera_id: loaded})
    obs = detector.classify(frame(empty_scene()), camera, CAPTURED, KEY)
    assert "d20-" in obs.detector_version, "the record names the calibration used"

    bare_detector = ReferenceDetector({model.camera_id: build_model()})
    obs_default = bare_detector.classify(frame(empty_scene()), camera, CAPTURED, KEY)
    assert "d40-" in obs_default.detector_version


# --- Band derivation ---------------------------------------------------------
# The band written here becomes part of the camera's reference metadata, so a
# wrong one silently changes what every later observation for that camera means.

MASS_TOP, MASS_BOTTOM = 60, 130
PAD = 6  # derive_band's default padding either side of the supported rows


def blocked_frames(n: int, rng_seed: int = 3) -> list[bytes]:
    """Frames of the same train sitting across rows MASS_TOP..MASS_BOTTOM."""
    rng = np.random.default_rng(rng_seed)
    frames = []
    for _ in range(n):
        scene = empty_scene().astype(np.int16)
        scene[MASS_TOP:MASS_BOTTOM, :] = 20
        scene += rng.integers(-3, 4, scene.shape)
        frames.append(frame(np.clip(scene, 0, 255).astype(np.uint8)))
    return frames


def test_band_covers_the_rows_the_trains_obstructed():
    """The derived band must bracket where the mass actually sat: too high and
    a low-profile consist falls outside it, too low and road traffic counts."""
    from blockade.detect.reference import derive_band_from_frames

    derived, used = derive_band_from_frames(build_model(), blocked_frames(5))

    assert used == 5
    assert derived is not None
    assert derived.top <= MASS_TOP <= derived.top + 2 * PAD
    assert derived.bottom - 2 * PAD <= MASS_BOTTOM <= derived.bottom


def test_derived_band_is_the_band_the_detector_then_reads():
    """The derivation and the detector must measure the same thing: a band that
    excludes the mass it was derived from would suppress the very blockages the
    camera was seen having."""
    from blockade.detect.reference import derive_band_from_frames

    model = build_model()
    frames = blocked_frames(5)
    derived, _ = derive_band_from_frames(model, frames)

    model.band = derived
    camera = Camera(camera_id="odot-1234", name="t", crossing_id="X", image_url="http://x")
    obs = ReferenceDetector({"odot-1234": model}).classify(frames[0], camera, CAPTURED, KEY)

    assert obs.state is CrossingState.BLOCKED


def test_one_off_obstructions_stay_out_of_the_band():
    """A vehicle stopped below the tracks appears in a frame or two. Widening
    the band to include it is how road traffic starts reading as a train."""
    from blockade.detect.reference import derive_band_from_frames

    frames = blocked_frames(4)
    stray = empty_scene()
    stray[MASS_TOP:MASS_BOTTOM, :] = 20
    stray[180:200, :] = 20  # present in exactly one frame
    frames.append(frame(stray))

    derived, used = derive_band_from_frames(build_model(), frames)

    assert used == 5
    assert derived.bottom < 180


def test_unusable_frames_are_skipped_and_not_counted():
    """Undecodable bytes, lighting the references have never seen, and a frame
    of the wrong shape each cost a profile. Counting them as contributors is
    how a band supported by two frames gets accepted as the verdict of five."""
    from blockade.detect.reference import derive_band_from_frames

    odd = io.BytesIO()
    Image.fromarray(np.full((SIZE[0], SIZE[1] - 20), 120, dtype=np.uint8), mode="L").save(
        odd, format="JPEG", quality=95
    )
    frames = blocked_frames(3) + [
        b"not a jpeg",
        frame(np.full_like(empty_scene(), 250)),  # bin the model has no reference near
        odd.getvalue(),
    ]

    derived, used = derive_band_from_frames(build_model(), frames)

    assert used == 3
    assert derived is not None


def test_no_obstruction_yields_no_band():
    """Clear frames must not produce a band. Inventing one from noise would
    pin the camera's track level to wherever the noise happened to fall."""
    from blockade.detect.reference import derive_band_from_frames

    derived, used = derive_band_from_frames(build_model(), [frame(empty_scene())] * 4)

    assert used == 4
    assert derived is None


def test_band_derivation_honours_the_thresholds_it_is_given():
    """Cameras carry their own calibration, and the band has to be derived with
    the thresholds that will later judge the camera, not the global defaults."""
    from blockade.detect.reference import derive_band_from_frames

    frames = blocked_frames(4)
    model = build_model()

    with_defaults, _ = derive_band_from_frames(model, frames)
    unreachable, _ = derive_band_from_frames(model, frames, Thresholds(band_row_fraction=1.1))

    assert with_defaults is not None
    assert unreachable is None

    model.thresholds = Thresholds(band_row_fraction=1.1)
    from_model, _ = derive_band_from_frames(model, frames)
    assert from_model is None, "the model's own calibration must win over the defaults"


# --- Real imagery ------------------------------------------------------------
# Committed fixtures from the confirmed 23:51-01:05 blockage on odot-678. These
# two tests are the end-to-end check on real camera frames; everything above
# runs on synthetic scenes. The reference model is built in-test from the clear
# fixture: twelve identical copies clear the min_samples floor with zero
# per-pixel spread, the strictest the detector ever gets.

NIGHT_CAMERA = "odot-678"
NIGHT_FRAMES = Path(__file__).parent / "fixtures" / "frames" / NIGHT_CAMERA
CLEAR_NIGHT = NIGHT_FRAMES / "clear-night.jpg"
BLOCKED_NIGHT = NIGHT_FRAMES / "blocked-night.jpg"


@pytest.fixture
def night_detector() -> ReferenceDetector:
    frames = [CLEAR_NIGHT.read_bytes()] * 12
    model = ReferenceModel.build(NIGHT_CAMERA, frames, min_samples=10)
    return ReferenceDetector({NIGHT_CAMERA: model})


@pytest.fixture
def night_camera() -> Camera:
    return Camera(
        camera_id=NIGHT_CAMERA,
        name="12th at Clinton",
        crossing_id="SE_12TH_CLINTON",
        image_url="https://example/678.jpg",
    )


def test_a_frame_matching_its_own_reference_reads_clear(night_detector, night_camera):
    obs = night_detector.classify(CLEAR_NIGHT.read_bytes(), night_camera, CAPTURED, KEY)

    assert obs.state is CrossingState.CLEAR


def test_the_night_train_is_detected(night_detector, night_camera):
    """The real 23:51-01:05 blockage, scored against a clear frame from ten
    minutes before it arrived: the same pair a human confirmed as clear and
    then blocked."""
    obs = night_detector.classify(BLOCKED_NIGHT.read_bytes(), night_camera, CAPTURED, KEY)

    assert obs.state is CrossingState.BLOCKED
    assert obs.confidence > 0.5
