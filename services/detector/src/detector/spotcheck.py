"""Grow the label set with a vision model: spot-check, then walk the edges.

Hand labels do not scale and the corpus does. The sweep samples each camera's
frames at a coarse stride and asks the VLM about each sample; wherever it sees
BLOCKED, the sweep walks neighboring frames at full cadence in both directions
until the crossing reads CLEAR for a few consecutive frames. That recovers the
whole blockage extent plus clean CLEAR shoulders on both sides - session-level
ground truth - while spending API calls mostly where trains actually are.

Machine labels never masquerade as human ones: every record names the model and
prompt version as its labeller, and downstream consumers can filter on that.

Plain functions on purpose; the only state is the frame listing and the output
file.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from blockade.config import Camera
from blockade.schemas import CrossingState

log = logging.getLogger(__name__)

CLEAR_STREAK_TO_STOP = 3
"""Consecutive CLEAR frames that end an expansion walk. One CLEAR can be a gap
between railcars; three at camera cadence (~6 minutes) is a real boundary."""

MAX_WALK = 40
"""Hard cap on frames walked in one direction from a hit, so a model stuck
answering BLOCKED cannot walk away with the budget."""


@dataclass(frozen=True)
class Judged:
    """One frame's judgement, ready to become a label record."""

    path: Path
    captured_at: datetime
    state: CrossingState
    confidence: float
    reason: str


def frame_time(path: Path) -> datetime:
    """Frames are named {epoch_ms}-{hash8}.jpg (older ones lack the hash)."""
    return datetime.fromtimestamp(int(path.stem.split("-")[0]) / 1000, tz=UTC)


def list_frames(camera_dir: Path) -> list[Path]:
    return sorted(camera_dir.rglob("*.jpg"), key=frame_time)


def stride_indices(frames: list[Path], stride_minutes: int) -> list[int]:
    """Indices of the coarse sample: the first frame at or past each stride."""
    if not frames:
        return []
    picks, next_at = [], frame_time(frames[0])
    for i, path in enumerate(frames):
        t = frame_time(path)
        if t >= next_at:
            picks.append(i)
            from datetime import timedelta

            next_at = t + timedelta(minutes=stride_minutes)
    return picks


def sweep_camera(
    camera: Camera,
    frames: list[Path],
    judge,
    stride_minutes: int = 15,
    already_labeled: set[str] | None = None,
    object_key_for=None,
) -> list[Judged]:
    """Sample at the stride; expand around every BLOCKED hit.

    ``judge(path) -> Judged`` is injected so tests drive the logic with a
    scripted model and the real sweep drives it with the VLM.

    On a repeat sweep, ``already_labeled`` (a set of object_keys) paired with
    ``object_key_for(path) -> str`` short-circuits frames the label file
    already covers before any API call is made. In walks those frames behave
    like UNKNOWN: absence of new evidence neither extends nor ends the walk.
    """
    already = already_labeled or set()
    judged: dict[int, Judged] = {}

    def is_already_labeled(i: int) -> bool:
        return object_key_for is not None and object_key_for(frames[i]) in already

    def judge_index(i: int) -> Judged | None:
        if i in judged:
            return judged[i]
        if is_already_labeled(i):
            return None
        judged[i] = judge(frames[i])
        return judged[i]

    def walk(start: int, step: int) -> None:
        streak, walked, i = 0, 0, start + step
        while 0 <= i < len(frames) and walked < MAX_WALK and streak < CLEAR_STREAK_TO_STOP:
            verdict = judge_index(i)
            if verdict is not None:
                if verdict.state is CrossingState.CLEAR:
                    streak += 1
                elif verdict.state is CrossingState.BLOCKED:
                    streak = 0
                # UNKNOWN neither extends nor resets: an unreadable frame
                # inside a blockage is not a boundary, the same rule the
                # alerter uses. Already-labeled frames follow the same rule.
            i += step
            walked += 1
        if walked >= MAX_WALK and streak < CLEAR_STREAK_TO_STOP:
            log.warning(
                "spotcheck walk from index %d step %+d hit MAX_WALK=%d without a CLEAR shoulder for camera %s; labels for this side end mid-blockage",
                start,
                step,
                MAX_WALK,
                camera.camera_id,
            )

    for i in stride_indices(frames, stride_minutes):
        verdict = judge_index(i)
        if verdict is not None and verdict.state is CrossingState.BLOCKED:
            walk(i, -1)
            walk(i, +1)

    return [judged[i] for i in sorted(judged)]


def label_record(camera: Camera, verdict: Judged, object_key: str, labeller: str) -> dict:
    return {
        "object_key": object_key,
        "camera_id": camera.camera_id,
        "crossing_id": camera.crossing_id,
        "captured_at": verdict.captured_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "local_time": verdict.captured_at.strftime("%H:%M UTC"),
        "label": verdict.state.value,
        "confidence": verdict.confidence,
        "reason": verdict.reason[:200],
        "lighting": "unrecorded",
        "source": "vlm-spot-check",
        "labeller": labeller,
        "labelled_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "visually_resolvable": verdict.state is not CrossingState.UNKNOWN,
    }


def append_labels(records: list[dict], labels_path: Path) -> int:
    """Append records whose object_key is not already present. Returns count."""
    existing = set()
    if labels_path.exists():
        for line in labels_path.read_text().splitlines():
            if line.strip():
                existing.add(json.loads(line)["object_key"])
    added = 0
    with labels_path.open("a", encoding="utf-8") as fh:
        for rec in records:
            if rec["object_key"] not in existing:
                fh.write(json.dumps(rec) + "\n")
                existing.add(rec["object_key"])
                added += 1
    return added
