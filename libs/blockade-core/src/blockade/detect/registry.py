"""Pick a detector by name.

Detectors are interchangeable on purpose. Which one is best is an empirical
question the label set keeps re-answering, and the answer changes per camera:
reference differencing is free and runs everywhere, per-camera classifiers win
where enough labels exist, Haiku reads a scene rather than a difference, and
`auto` mixes them. Swapping must be a config change, never an edit.

Everything downstream is insulated from the choice because each one returns the
same ObservationRecord, stamped with its own `detector_version` - so rows from
different detectors are never silently mixed, and two detectors can be run over
the same frames and compared.

Imports are deferred: choosing `reference` must not drag in onnxruntime or an
API client.
"""

from __future__ import annotations

import logging

from blockade.config import Settings
from blockade.detect.base import Detector

log = logging.getLogger(__name__)

DETECTOR_NAMES = ("reference", "vlm", "classifier", "auto")


def _build_reference(settings: Settings) -> Detector:
    from blockade.detect.reference import ReferenceDetector, ReferenceModel

    roster_ids = _camera_ids(settings)
    models = {}
    for camera_id in roster_ids:
        model = ReferenceModel.load(settings.references_dir, camera_id)
        if model is not None:
            models[camera_id] = model
    return ReferenceDetector(models)


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


def _camera_ids(settings: Settings) -> list[str]:
    from blockade.config import load_roster

    try:
        return [c.camera_id for c in load_roster(settings.camera_config_path).enabled()]
    except (FileNotFoundError, ValueError) as exc:
        # An empty roster builds a detector with no models, which answers
        # UNKNOWN for every camera forever - indistinguishable in metrics from
        # a healthy detector watching dark cameras. Say so, loudly.
        log.warning("camera roster unusable (%s); every camera will score UNKNOWN", exc)
        return []


def build_detector(name: str | None = None, settings: Settings | None = None) -> Detector:
    """Construct the configured detector.

    Fails loudly on an unknown name rather than falling back to a default: a
    typo that silently selects a different detector would quietly change what
    the dataset means.
    """
    from blockade.config import get_settings

    settings = settings or get_settings()
    match (name or settings.detector).lower():
        case "reference":
            return _build_reference(settings)
        case "vlm":
            from blockade.detect.vlm import VlmDetector

            return VlmDetector(settings)
        case "classifier":
            return _build_classifier(settings)
        case "auto":
            return _build_auto(settings)
        case unknown:
            raise ValueError(
                f"Unknown detector {unknown!r}. Available: {', '.join(DETECTOR_NAMES)}"
            )
