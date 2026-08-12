"""Pick a detector by name.

Detectors are interchangeable on purpose. Which one is best is an open question
that only real data answers, and the answer will change: reference differencing
is free and running today, YOLO-World needs no training, a fine-tuned YOLO is
DESIGN2's endpoint, and Haiku reads a scene rather than a difference. Swapping
between them must be a config change, never an edit.

Everything downstream is insulated from the choice because each one returns the
same ObservationRecord, stamped with its own `detector_version` - so rows from
different detectors are never silently mixed, and two detectors can be run over
the same frames and compared.

Imports are deferred: choosing `reference` must not drag in torch.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from blockade.config import Settings
from blockade.detect.base import Detector

DETECTOR_NAMES = ("reference", "yolo", "vlm", "classifier", "auto")


def _build_reference(settings: Settings) -> Detector:
    from blockade.detect.reference import ReferenceDetector, ReferenceModel

    roster_ids = _camera_ids(settings)
    models = {}
    for camera_id in roster_ids:
        model = ReferenceModel.load(settings.references_dir, camera_id)
        if model is not None:
            models[camera_id] = model
    return ReferenceDetector(models)


def _build_yolo(settings: Settings) -> Detector:
    from blockade.detect.yolo import YoloWorldDetector

    return YoloWorldDetector(settings)


def _build_vlm(settings: Settings) -> Detector:
    from blockade.detect.vlm import VlmDetector

    return VlmDetector(settings)


def _build_classifier(settings: Settings) -> Detector:
    from blockade.detect.classifier import ClassifierDetector

    return ClassifierDetector(settings)


class AutoDetector:
    """Route per camera: the trained classifier where a model exists, the
    reference detector everywhere else.

    The detector choice is per camera in truth - 678 has a trained classifier
    while 681 runs a hand-calibrated reference - but the config knob is
    global, and flipping it wholesale would turn every untrained camera into
    permanent UNKNOWN. Each observation is stamped by the detector that
    actually scored it, so mixed fleets stay auditable row by row.
    """

    def __init__(self, classifier: Detector, reference: Detector, trained: set[str]) -> None:
        self._classifier = classifier
        self._reference = reference
        self._trained = trained
        self.version = f"auto({classifier.version}|{reference.version})"

    def classify(self, image, camera, captured_at, object_key):  # noqa: ANN001
        chosen = self._classifier if camera.camera_id in self._trained else self._reference
        return chosen.classify(image, camera, captured_at, object_key)


def _build_auto(settings: Settings) -> Detector:
    trained = {
        p.stem.removeprefix("classifier-")
        for p in settings.references_dir.glob("classifier-*.onnx")
    }
    reference = _build_reference(settings)
    if not trained:
        return reference
    return AutoDetector(_build_classifier(settings), reference, trained)


_BUILDERS: dict[str, Callable[[Settings], Detector]] = {
    "reference": _build_reference,
    "yolo": _build_yolo,
    "vlm": _build_vlm,
    "classifier": _build_classifier,
    "auto": _build_auto,
}


def _camera_ids(settings: Settings) -> list[str]:
    from blockade.config import load_roster

    try:
        return [c.camera_id for c in load_roster(settings.camera_config_path).enabled()]
    except (FileNotFoundError, ValueError):
        return []


def build_detector(name: str | None = None, settings: Settings | None = None) -> Detector:
    """Construct the configured detector.

    Fails loudly on an unknown name rather than falling back to a default: a
    typo that silently selects a different detector would quietly change what
    the dataset means.
    """
    from blockade.config import get_settings

    settings = settings or get_settings()
    name = (name or settings.detector).lower()
    if name not in _BUILDERS:
        raise ValueError(f"Unknown detector {name!r}. Available: {', '.join(DETECTOR_NAMES)}")
    return _BUILDERS[name](settings)


def references_dir_default() -> Path:
    from blockade.config import REPO_ROOT

    return REPO_ROOT / "var" / "references"
