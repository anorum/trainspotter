"""Per-camera learned classifier runtime.

The properties pinned here are the guarantees the runtime contract makes:
missing models and inference failures abstain rather than guess, honest
UNKNOWN carries zero confidence, and the detector_version records the exact
model bytes that judged a frame so rows from different weights never mix.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime

import numpy as np
import pytest
from blockade.config import Camera, CameraSource, Settings
from blockade.detect.classifier import (
    LABELS,
    MIN_CONFIDENCE,
    ClassifierDetector,
    _VERSION_BASE,
)
from blockade.schemas import CrossingState
from PIL import Image

CAPTURED = datetime(2026, 8, 9, 7, 23, 10, tzinfo=UTC)
KEY = "frames/odot-1234/2026/08/09/07/x.jpg"


def _jpeg(color: tuple[int, int, int] = (120, 120, 120)) -> bytes:
    img = Image.new("RGB", (32, 32), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


class _FakeSession:
    """Stand-in for an onnxruntime.InferenceSession.

    Runtime code only calls `.run(["logits"], {"image": x})` and consumes
    logits[0]. A tiny fake keeps the softmax + threshold logic under test
    without needing torch to export a real ONNX file.
    """

    def __init__(self, logits: list[float]) -> None:
        self._logits = np.array([logits], dtype=np.float32)

    def run(self, output_names, inputs):
        return [self._logits]


def _detector(tmp_path, settings_factory) -> ClassifierDetector:
    return ClassifierDetector(settings_factory(tmp_path))


def _settings_factory(tmp_path):
    def make(td):
        return Settings(
            s3_bucket="blockade-test",
            local_cache_dir=td / "frames",
            manifest_dir=td / "manifests",
            camera_config_path=td / "cameras.yaml",
            references_dir=td / "references",
        )
    return make(tmp_path)


@pytest.fixture
def classifier_camera() -> Camera:
    return Camera(
        camera_id="odot-1234",
        name="Portland - 11th at Milwaukie N",
        crossing_id="SE_11TH_MILWAUKIE",
        image_url="https://tripcheck.example/cams/1234.jpg",
        source=CameraSource.MANUAL,
    )


@pytest.fixture
def classifier_settings(tmp_path) -> Settings:
    return _settings_factory(tmp_path)


def test_missing_model_is_unknown(classifier_settings, classifier_camera):
    """A camera without a trained model must abstain rather than guess. A
    coin-flip into the dataset corrupts a record that cannot be recaptured."""
    classifier_settings.references_dir.mkdir(parents=True, exist_ok=True)
    detector = ClassifierDetector(classifier_settings)

    obs = detector.classify(_jpeg(), classifier_camera, CAPTURED, KEY)

    assert obs.state is CrossingState.UNKNOWN
    assert obs.confidence == 0.0
    assert "no classifier" in obs.reason.lower()
    assert obs.detector_version.startswith(_VERSION_BASE)


def test_undecodable_image_is_unknown(classifier_settings, classifier_camera):
    """Junk bytes from a poll timeout must not fabricate a judgement."""
    classifier_settings.references_dir.mkdir(parents=True, exist_ok=True)
    detector = ClassifierDetector(classifier_settings)
    # Seed a session so we bypass the missing-model branch and exercise decode.
    detector._sessions[classifier_camera.camera_id] = (
        _FakeSession([0.0, 5.0]), "seeded"
    )

    obs = detector.classify(b"not a jpeg", classifier_camera, CAPTURED, KEY)

    assert obs.state is CrossingState.UNKNOWN
    assert obs.confidence == 0.0


def test_inference_failure_is_unknown(classifier_settings, classifier_camera):
    class Boom:
        def run(self, *_):
            raise RuntimeError("kernel exploded")

    classifier_settings.references_dir.mkdir(parents=True, exist_ok=True)
    detector = ClassifierDetector(classifier_settings)
    detector._sessions[classifier_camera.camera_id] = (Boom(), "seeded")

    obs = detector.classify(_jpeg(), classifier_camera, CAPTURED, KEY)

    assert obs.state is CrossingState.UNKNOWN
    assert obs.confidence == 0.0
    assert "inference failed" in obs.reason


def test_confident_blocked_wins(classifier_settings, classifier_camera):
    classifier_settings.references_dir.mkdir(parents=True, exist_ok=True)
    detector = ClassifierDetector(classifier_settings)
    # Logits ordered per LABELS: (CLEAR, BLOCKED). Big positive on BLOCKED
    # gives a softmax probability well above MIN_CONFIDENCE.
    logits = [0.0, 0.0]
    logits[LABELS.index("BLOCKED")] = 6.0
    detector._sessions[classifier_camera.camera_id] = (_FakeSession(logits), "seeded")

    obs = detector.classify(_jpeg(), classifier_camera, CAPTURED, KEY)

    assert obs.state is CrossingState.BLOCKED
    assert obs.confidence >= MIN_CONFIDENCE
    assert obs.detector_version == "seeded"


def test_confident_clear_wins(classifier_settings, classifier_camera):
    classifier_settings.references_dir.mkdir(parents=True, exist_ok=True)
    detector = ClassifierDetector(classifier_settings)
    logits = [0.0, 0.0]
    logits[LABELS.index("CLEAR")] = 6.0
    detector._sessions[classifier_camera.camera_id] = (_FakeSession(logits), "seeded")

    obs = detector.classify(_jpeg(), classifier_camera, CAPTURED, KEY)

    assert obs.state is CrossingState.CLEAR
    assert obs.confidence >= MIN_CONFIDENCE


def test_undecided_softmax_abstains(classifier_settings, classifier_camera):
    """Between the two-sided thresholds the honest answer is UNKNOWN, and its
    confidence must be zeroed so downstream code cannot rank it as a guess."""
    classifier_settings.references_dir.mkdir(parents=True, exist_ok=True)
    detector = ClassifierDetector(classifier_settings)
    # Even logits: softmax = 0.5, squarely in the abstain band for any
    # MIN_CONFIDENCE strictly greater than 0.5.
    detector._sessions[classifier_camera.camera_id] = (
        _FakeSession([0.0, 0.0]), "seeded"
    )

    obs = detector.classify(_jpeg(), classifier_camera, CAPTURED, KEY)

    assert obs.state is CrossingState.UNKNOWN
    assert obs.confidence == 0.0
    assert "undecided" in obs.reason


def test_model_version_hashes_the_bytes(
    classifier_settings, classifier_camera, monkeypatch
):
    """Two different model files for the same camera must produce two different
    detector_version strings so rows from different weights never mix in the
    dataset."""
    classifier_settings.references_dir.mkdir(parents=True, exist_ok=True)
    a_path = classifier_settings.references_dir / "classifier-odot-1234.onnx"
    a_path.write_bytes(b"model-bytes-A")

    import onnxruntime

    def fake_session(*_, **__):
        return _FakeSession([0.0, 0.0])

    monkeypatch.setattr(onnxruntime, "InferenceSession", fake_session)

    detector_a = ClassifierDetector(classifier_settings)
    obs_a = detector_a.classify(_jpeg(), classifier_camera, CAPTURED, KEY)
    assert "-h" in obs_a.detector_version
    version_a = obs_a.detector_version

    a_path.write_bytes(b"model-bytes-B")
    detector_b = ClassifierDetector(classifier_settings)
    obs_b = detector_b.classify(_jpeg(), classifier_camera, CAPTURED, KEY)

    assert obs_b.detector_version != version_a
    assert "-h" in obs_b.detector_version


def test_corrupt_model_file_falls_back_to_unknown(
    classifier_settings, classifier_camera
):
    """A file that exists but is not a valid ONNX must abstain rather than
    crashing the runner mid-stream. The Protocol forbids raising."""
    classifier_settings.references_dir.mkdir(parents=True, exist_ok=True)
    (classifier_settings.references_dir / "classifier-odot-1234.onnx").write_bytes(
        b"not a real onnx model"
    )
    detector = ClassifierDetector(classifier_settings)

    obs = detector.classify(_jpeg(), classifier_camera, CAPTURED, KEY)

    assert obs.state is CrossingState.UNKNOWN
    assert obs.confidence == 0.0


def test_session_cache_scoped_per_camera(classifier_settings):
    """Two cameras must each get their own model; a hit on one must not serve
    the other's frames from the wrong weights."""
    classifier_settings.references_dir.mkdir(parents=True, exist_ok=True)
    detector = ClassifierDetector(classifier_settings)
    detector._sessions["odot-A"] = (_FakeSession([6.0, 0.0]), "seeded-A")
    detector._sessions["odot-B"] = (_FakeSession([0.0, 6.0]), "seeded-B")

    def cam(cid: str) -> Camera:
        return Camera(
            camera_id=cid,
            name=cid,
            crossing_id="X",
            image_url="https://x.example/y.jpg",
        )

    obs_a = detector.classify(_jpeg(), cam("odot-A"), CAPTURED, KEY)
    obs_b = detector.classify(_jpeg(), cam("odot-B"), CAPTURED, KEY)

    assert obs_a.state is CrossingState.CLEAR
    assert obs_b.state is CrossingState.BLOCKED
    assert obs_a.detector_version == "seeded-A"
    assert obs_b.detector_version == "seeded-B"


def test_registry_builds_classifier(tmp_path):
    """The detector is wired into the registry so a config switch, not an edit,
    puts it in the runner. Verify the name resolves to the right class without
    dragging torch into the process."""
    from blockade.detect.registry import build_detector

    settings = Settings(
        s3_bucket="blockade-test",
        local_cache_dir=tmp_path / "frames",
        manifest_dir=tmp_path / "manifests",
        camera_config_path=tmp_path / "cameras.yaml",
        references_dir=tmp_path / "references",
    )
    settings.references_dir.mkdir(parents=True, exist_ok=True)

    detector = build_detector("classifier", settings)

    assert isinstance(detector, ClassifierDetector)
    assert detector.version.startswith(_VERSION_BASE)
