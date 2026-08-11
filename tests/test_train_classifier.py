"""Frame lookup for the training runner.

The trainer resolves manifest object_keys to on-disk paths and must never let
a filename collision across cameras hand one camera's frame bytes to another
camera's model - that is silent label corruption.
"""

from __future__ import annotations

import json
from pathlib import Path

from detector.train_classifier import load_examples


def _make_frame(root: Path, camera_id: str, name: str) -> Path:
    d = root / camera_id / "2026" / "03" / "01" / "12"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"\xff\xd8\xff\xd9")
    return p


def test_frame_lookup_is_scoped_to_manifest_camera(tmp_path):
    """Two cameras happen to publish frames with the same filename. The
    trainer must pick the file that lives under the manifest row's camera.
    Otherwise camera B's images end up in camera A's training set."""
    root = tmp_path / "frames"
    name = "1740835200000-x.jpg"
    a_path = _make_frame(root, "odot-A", name)
    b_path = _make_frame(root, "odot-B", name)

    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        json.dumps({
            "object_key": f"frames/odot-A/2026/03/01/12/{name}",
            "camera_id": "odot-A",
            "label": "BLOCKED",
            "source": "session-core",
            "split": "train",
        }) + "\n"
        + json.dumps({
            "object_key": f"frames/odot-B/2026/03/01/12/{name}",
            "camera_id": "odot-B",
            "label": "CLEAR",
            "source": "quiet-period",
            "split": "train",
        }) + "\n"
    )

    items = load_examples(manifest, [root], "train")

    paths = {p for p, _ in items}
    assert a_path in paths
    assert b_path in paths
    for path, label in items:
        if "odot-A" in path.parts:
            assert label == 1  # BLOCKED index
        if "odot-B" in path.parts:
            assert label == 0  # CLEAR index


def test_load_examples_filters_by_split(tmp_path):
    root = tmp_path / "frames"
    _make_frame(root, "odot-A", "1000-x.jpg")
    _make_frame(root, "odot-A", "2000-x.jpg")

    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        json.dumps({
            "object_key": "frames/odot-A/2026/03/01/12/1000-x.jpg",
            "camera_id": "odot-A",
            "label": "CLEAR",
            "source": "quiet-period",
            "split": "train",
        }) + "\n"
        + json.dumps({
            "object_key": "frames/odot-A/2026/03/01/12/2000-x.jpg",
            "camera_id": "odot-A",
            "label": "BLOCKED",
            "source": "session-core",
            "split": "val",
        }) + "\n"
    )

    train_items = load_examples(manifest, [root], "train")
    val_items = load_examples(manifest, [root], "val")

    assert len(train_items) == 1 and train_items[0][1] == 0
    assert len(val_items) == 1 and val_items[0][1] == 1
