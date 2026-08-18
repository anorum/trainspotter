"""Assemble a training manifest for the per-camera classifier.

The label sources, by trust:

- gold: data/labels/labels.jsonl - human-anchored adjudications. Held out of
  training entirely; they are the exam, not the textbook.
- weak positives: the cores of known blockage sessions (batch-oracle sessions
  plus the compacted sessions topic), shrunk by a margin at both ends so
  boundary uncertainty never becomes a training label.
- weak negatives: VLM sweep frames judged CLEAR with confidence, plus frames
  far from any known session.

Weak labels carry their provenance so a bad source can be cut later without
re-assembling anything. Plain functions; the only state is files.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from blockade.storage import frame_key_prefix, frame_time

CORE_MARGIN = timedelta(minutes=2)
"""Cut from each end of a session before its frames count as positives."""

QUIET_MARGIN = timedelta(minutes=30)
"""A frame must be this far from every known session to count as a quiet
negative."""

VAL_FRACTION = 0.15
"""Per class, held out for validation during training (gold labels are a
separate test set entirely)."""


@dataclass(frozen=True)
class Example:
    object_key: str
    camera_id: str
    label: str  # BLOCKED | CLEAR
    source: str
    split: str  # train | val


def session_windows(
    session_files: list[Path],
    crossing_id: str,
    open_end: datetime | None = None,
) -> list[tuple[datetime, datetime, bool]]:
    """Merged windows for one crossing from session JSONL files.

    Later records for the same session_id win, matching topic compaction.

    Each window carries a `closed` flag. An unclosed session extends to
    `open_end` (typically the last observed frame time) so its frames are still
    excluded from quiet-negatives - a live blockage is not a quiet period. It
    is marked closed=False so callers can keep it out of positive cores, whose
    real end we do not know and must not guess.
    """
    by_id: dict[str, dict] = {}
    for file in session_files:
        for line in file.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("crossing_id") == crossing_id:
                by_id[rec["session_id"]] = rec
    windows: list[tuple[datetime, datetime, bool]] = []
    for rec in by_id.values():
        start = datetime.fromisoformat(rec["started_at"])
        if rec.get("ended_at"):
            windows.append((start, datetime.fromisoformat(rec["ended_at"]), True))
        elif open_end is not None and open_end >= start:
            windows.append((start, open_end, False))
    return sorted(windows)


def build_manifest(
    camera_id: str,
    crossing_id: str,
    frames_dir: Path,
    session_files: list[Path],
    sweep_file: Path | None,
    gold_labels: Path,
    seed: int = 11,
) -> list[Example]:
    frames = sorted(frames_dir.rglob("*.jpg"), key=lambda p: frame_time(p.name))
    frames_end = frame_time(frames[-1].name) if frames else None
    windows = session_windows(session_files, crossing_id, open_end=frames_end)
    closed_windows = [(s, e) for s, e, closed in windows if closed]
    near_windows = [(s, e) for s, e, _ in windows]
    gold_keys = {
        json.loads(line)["object_key"]
        for line in gold_labels.read_text().splitlines()
        if line.strip()
    }
    sweep_clear_keys = set()
    if sweep_file is not None and sweep_file.exists():
        for line in sweep_file.read_text().splitlines():
            rec = json.loads(line)
            if (
                rec["camera_id"] == camera_id
                and rec["label"] == "CLEAR"
                and rec["confidence"] >= 0.8
            ):
                sweep_clear_keys.add(Path(rec["object_key"]).name)

    def in_core(t: datetime) -> bool:
        return any(s + CORE_MARGIN <= t <= e - CORE_MARGIN for s, e in closed_windows)

    def near_any_session(t: datetime) -> bool:
        return any(s - QUIET_MARGIN <= t <= e + QUIET_MARGIN for s, e in near_windows)

    examples: list[Example] = []
    for path in frames:
        key = f"{frame_key_prefix(camera_id, frame_time(path.name))}/{path.name}"
        if key in gold_keys:
            continue  # gold is the exam, never the textbook
        t = frame_time(path.name)
        if in_core(t):
            examples.append(Example(key, camera_id, "BLOCKED", "session-core", "train"))
        elif path.name in sweep_clear_keys:
            examples.append(Example(key, camera_id, "CLEAR", "vlm-sweep", "train"))
        elif not near_any_session(t):
            examples.append(Example(key, camera_id, "CLEAR", "quiet-period", "train"))

    # Deterministic validation split, stratified by label.
    rng = random.Random(seed)
    by_label: dict[str, list[int]] = {}
    for i, ex in enumerate(examples):
        by_label.setdefault(ex.label, []).append(i)
    val_indices = set()
    for indices in by_label.values():
        rng.shuffle(indices)
        val_indices.update(indices[: max(1, int(len(indices) * VAL_FRACTION))])
    return [
        replace(ex, split="val" if i in val_indices else "train") for i, ex in enumerate(examples)
    ]


def write_manifest(examples: list[Example], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for ex in examples:
            fh.write(json.dumps(ex.__dict__) + "\n")
