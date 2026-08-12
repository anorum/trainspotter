"""The auto detector: per-camera routing between classifier and reference.

The knob is global but the truth is per camera - these pin that a trained
camera goes to the classifier, an untrained one to the reference, and that a
references directory with no classifiers collapses to plain reference (no
wrapper in the way).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from blockade.config import Camera
from blockade.detect.registry import AutoDetector, _build_auto

T = datetime(2026, 8, 12, 6, 0, tzinfo=UTC)


def _camera(camera_id: str) -> Camera:
    return Camera(
        camera_id=camera_id,
        crossing_id="SE_12TH_CLINTON",
        name="test",
        image_url="http://example.test/img.jpg",
    )


@dataclass
class Probe:
    name: str
    version: str = "probe/1"
    calls: int = 0

    def classify(self, image, camera, captured_at, object_key):  # noqa: ANN001
        self.calls += 1
        return self.name


def test_trained_camera_routes_to_classifier_and_others_to_reference() -> None:
    clf, ref = Probe("classifier"), Probe("reference")
    auto = AutoDetector(clf, ref, trained={"odot-678"})

    assert auto.classify(b"", _camera("odot-678"), T, "k") == "classifier"
    assert auto.classify(b"", _camera("odot-681"), T, "k") == "reference"
    assert (clf.calls, ref.calls) == (1, 1)


def test_build_auto_without_any_classifier_is_plain_reference(tmp_path: Path) -> None:
    class Settings:
        references_dir = tmp_path
        camera_config_path = tmp_path / "missing.yaml"

    detector = _build_auto(Settings())
    assert not isinstance(detector, AutoDetector), "no models, no wrapper"
