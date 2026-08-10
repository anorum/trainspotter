from pathlib import Path

from blockade.schemas import CrossingState

from main import classify_image


def test_classify_image_with_repo_sample() -> None:
    image_path = Path("var/frames/frames/odot-676/2026/08/09/04/1786249269000.jpg")

    observation = classify_image(
        image_path=image_path,
        reference_dir=Path("var/references"),
    )

    assert observation.camera_id == "odot-676"
    assert observation.state in {
        CrossingState.CLEAR,
        CrossingState.BLOCKED,
        CrossingState.UNKNOWN,
    }
