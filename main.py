from __future__ import annotations

import argparse
import re
from datetime import UTC, datetime
from pathlib import Path

from blockade.config import Camera, CameraSource
from blockade.detect.reference import ReferenceDetector, ReferenceModel, _brightness_bin, _decode
from blockade.schemas import ObservationRecord

CAMERA_ID = re.compile(r"^odot-\d+$")


def _infer_camera_id(image_path: Path, reference_dir: Path) -> str | None:
    """Recover the camera id from the frame's directory.

    Only directory components are considered, and the shape is checked. Scanning
    every path part matched the filename too, so a frame saved as
    "odot-678-clear-night.jpg" reported its whole filename as the camera id and
    then found no reference for it.
    """
    for part in image_path.parent.parts:
        if CAMERA_ID.match(part):
            return part

    reference_files = sorted(reference_dir.glob("*.npz"))
    if len(reference_files) == 1:
        return reference_files[0].stem
    return None


def load_camera(camera_id: str) -> Camera:
    return Camera(
        camera_id=camera_id,
        name=camera_id,
        crossing_id="manual",
        image_url="https://example.invalid/",
        source=CameraSource.MANUAL,
    )


def load_detector(reference_dir: Path | None = None) -> ReferenceDetector:
    reference_dir = reference_dir or Path("var/references")
    models: dict[str, ReferenceModel] = {}
    for path in sorted(reference_dir.glob("*.npz")):
        camera_id = path.stem
        model = ReferenceModel.load(reference_dir, camera_id)
        if model is not None:
            models[camera_id] = model
    return ReferenceDetector(models)


def classify_image(
    image_path: Path | str,
    camera_id: str | None = None,
    reference_dir: Path | None = None,
) -> ObservationRecord:
    observation, _ = classify_image_with_debug(
        image_path=image_path,
        camera_id=camera_id,
        reference_dir=reference_dir,
    )
    return observation


def classify_image_with_debug(
    image_path: Path | str,
    camera_id: str | None = None,
    reference_dir: Path | None = None,
) -> tuple[ObservationRecord, dict[str, object]]:
    reference_dir = reference_dir or Path("var/references")
    detector = load_detector(reference_dir=reference_dir)
    image_path = Path(image_path)
    resolved_camera_id = camera_id or _infer_camera_id(image_path, reference_dir)
    if resolved_camera_id is None:
        raise ValueError(
            "Could not infer a camera ID from the image path. Pass --camera-id explicitly."
        )

    camera = load_camera(resolved_camera_id)
    image_bytes = image_path.read_bytes()
    observation = detector.classify(
        image=image_bytes,
        camera=camera,
        captured_at=datetime.now(UTC),
        object_key=str(image_path),
    )

    debug: dict[str, object] = {
        "image_path": str(image_path),
        "camera_id": resolved_camera_id,
        "available_reference_models": sorted(detector._models),
        "reference_model_loaded": resolved_camera_id in detector._models,
    }

    scene = _decode(image_bytes)
    if scene is None:
        debug["decoded"] = False
        debug["decode_reason"] = "image could not be decoded"
        return observation, debug

    debug["decoded"] = True
    debug["image_shape"] = scene.shape
    debug["brightness_bin"] = _brightness_bin(scene)
    debug["luminance"] = float(scene.mean())

    model = detector._models.get(resolved_camera_id)
    debug["model_band"] = None if model is None or model.band is None else {
        "top": model.band.top,
        "bottom": model.band.bottom,
    }

    if model is None:
        debug["reference_reason"] = "no reference model for this camera"
        return observation, debug

    reference = model.nearest(_brightness_bin(scene))
    if reference is None:
        debug["reference_reason"] = "no suitable reference for this brightness"
        return observation, debug

    debug["reference_shape"] = reference.median.shape
    debug["reference_bin"] = _brightness_bin(scene)
    evidence = detector.examine(scene, reference, model.band)
    debug["changed_fraction"] = float(evidence.changed_fraction)
    debug["band_rows"] = int(evidence.band_rows)
    debug["thresholds"] = {
        "pixel_delta": detector.thresholds.pixel_delta,
        "spread_multiple": detector.thresholds.spread_multiple,
        "band_row_fraction": detector.thresholds.band_row_fraction,
        "min_band_rows": detector.thresholds.min_band_rows,
        "clear_max_changed": detector.thresholds.clear_max_changed,
        "min_luminance": detector.thresholds.min_luminance,
    }

    return observation, debug


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the reference detector manually on one image")
    parser.add_argument("image", type=Path, help="Path to a JPEG file")
    parser.add_argument(
        "--camera-id",
        default=None,
        help="Optional camera ID; inferred from the image path when possible",
    )
    parser.add_argument(
        "--reference-dir",
        type=Path,
        default=Path("var/references"),
        help="Directory that contains .npz/.json reference files",
    )
    args = parser.parse_args()

    observation, debug = classify_image_with_debug(
        image_path=args.image,
        camera_id=args.camera_id,
        reference_dir=args.reference_dir,
    )

    print(f"state={observation.state.value}")
    print(f"confidence={observation.confidence:.2f}")
    print(f"reason={observation.reason}")
    print(f"detector_version={observation.detector_version}")
    print("debug:")
    for key, value in debug.items():
        print(f"  {key}={value}")


if __name__ == "__main__":
    raise SystemExit(main())
