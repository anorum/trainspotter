"""Training manifest assembly for the per-camera classifier.

Guarantees the dataset must uphold, all end-user visible through the manifest:
gold labels are the exam and never leak into training; positives come from
session cores shrunk by CORE_MARGIN so uncertain boundaries never become
training labels; quiet-period frames need to be far from every known session;
unclosed sessions keep their frames out of quiet-negatives even though we do
not know when they end.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from detector.dataset import (
    CORE_MARGIN,
    QUIET_MARGIN,
    build_manifest,
    session_windows,
    write_manifest,
)


def _frame(dir_: Path, camera_id: str, ts: datetime) -> Path:
    hour_dir = dir_ / camera_id / f"{ts:%Y/%m/%d/%H}"
    hour_dir.mkdir(parents=True, exist_ok=True)
    ms = int(ts.timestamp() * 1000)
    p = hour_dir / f"{ms}-x.jpg"
    p.write_bytes(b"\xff\xd8\xff\xd9")  # tiny JPEG marker; contents unread
    return p


def _key(camera_id: str, ts: datetime, name: str) -> str:
    return f"frames/{camera_id}/{ts:%Y/%m/%d/%H}/{name}"


def _session_line(
    crossing_id: str,
    session_id: str,
    started_at: datetime,
    ended_at: datetime | None,
) -> str:
    rec = {
        "session_id": session_id,
        "crossing_id": crossing_id,
        "started_at": started_at.isoformat(),
    }
    if ended_at is not None:
        rec["ended_at"] = ended_at.isoformat()
    return json.dumps(rec)


@pytest.fixture
def scene(tmp_path: Path):
    frames_dir = tmp_path / "frames"
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    return frames_dir, sessions_dir


def test_gold_labels_never_leak_into_training(scene, tmp_path):
    """The gold set is the exam. A single leaked frame taints the accuracy
    number that decides whether the classifier ships."""
    frames_dir, sessions_dir = scene
    camera_id = "odot-1234"
    crossing_id = "SE_11TH_MILWAUKIE"
    base = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

    session_start = base
    session_end = base + timedelta(minutes=30)
    core_ts = session_start + CORE_MARGIN + timedelta(minutes=5)  # deep in core
    core_frame = _frame(frames_dir, camera_id, core_ts)
    _frame(frames_dir, camera_id, base + timedelta(hours=3))

    sessions_file = sessions_dir / "sessions.jsonl"
    sessions_file.write_text(_session_line(crossing_id, "s-1", session_start, session_end) + "\n")

    gold = tmp_path / "gold.jsonl"
    gold.write_text(
        json.dumps(
            {
                "object_key": _key(camera_id, core_ts, core_frame.name),
                "state": "BLOCKED",
            }
        )
        + "\n"
    )

    examples = build_manifest(
        camera_id=camera_id,
        crossing_id=crossing_id,
        frames_dir=frames_dir,
        session_files=[sessions_file],
        sweep_file=None,
        gold_labels=gold,
    )

    keys = {ex.object_key for ex in examples}
    assert _key(camera_id, core_ts, core_frame.name) not in keys, (
        "gold-labeled frame must not appear in training or validation"
    )


def test_session_core_frames_become_blocked_positives(scene, tmp_path):
    """Only the middle of a session is a safe positive; the edges the training
    data leaves alone. Otherwise a mislabeled boundary teaches the classifier
    that half a train is a train."""
    frames_dir, sessions_dir = scene
    camera_id = "odot-1234"
    crossing_id = "SE_11TH_MILWAUKIE"
    session_start = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    session_end = session_start + timedelta(minutes=20)

    core_ts = session_start + CORE_MARGIN + timedelta(minutes=5)
    edge_ts = session_start + timedelta(seconds=30)  # inside margin
    _frame(frames_dir, camera_id, core_ts)
    _frame(frames_dir, camera_id, edge_ts)

    sessions_file = sessions_dir / "sessions.jsonl"
    sessions_file.write_text(_session_line(crossing_id, "s-1", session_start, session_end) + "\n")
    gold = tmp_path / "gold.jsonl"
    gold.write_text("")

    examples = build_manifest(
        camera_id=camera_id,
        crossing_id=crossing_id,
        frames_dir=frames_dir,
        session_files=[sessions_file],
        sweep_file=None,
        gold_labels=gold,
    )

    by_time = {ex.object_key: ex for ex in examples}
    core_key = _key(camera_id, core_ts, f"{int(core_ts.timestamp() * 1000)}-x.jpg")
    edge_key = _key(camera_id, edge_ts, f"{int(edge_ts.timestamp() * 1000)}-x.jpg")
    assert core_key in by_time and by_time[core_key].label == "BLOCKED"
    assert by_time[core_key].source == "session-core"
    assert edge_key not in by_time, "edge-of-session frame must not become a label"


def test_quiet_period_frames_become_clear_negatives(scene, tmp_path):
    """A frame far from every known session is a trustworthy CLEAR: no one
    ever thought it was blocked."""
    frames_dir, sessions_dir = scene
    camera_id = "odot-1234"
    crossing_id = "SE_11TH_MILWAUKIE"
    session_start = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    session_end = session_start + timedelta(minutes=15)

    quiet_ts = session_end + QUIET_MARGIN + timedelta(hours=2)
    near_ts = session_end + timedelta(minutes=5)  # inside QUIET_MARGIN
    _frame(frames_dir, camera_id, quiet_ts)
    _frame(frames_dir, camera_id, near_ts)

    sessions_file = sessions_dir / "sessions.jsonl"
    sessions_file.write_text(_session_line(crossing_id, "s-1", session_start, session_end) + "\n")
    gold = tmp_path / "gold.jsonl"
    gold.write_text("")

    examples = build_manifest(
        camera_id=camera_id,
        crossing_id=crossing_id,
        frames_dir=frames_dir,
        session_files=[sessions_file],
        sweep_file=None,
        gold_labels=gold,
    )

    by_key = {ex.object_key: ex for ex in examples}
    quiet_key = _key(camera_id, quiet_ts, f"{int(quiet_ts.timestamp() * 1000)}-x.jpg")
    near_key = _key(camera_id, near_ts, f"{int(near_ts.timestamp() * 1000)}-x.jpg")
    assert quiet_key in by_key and by_key[quiet_key].label == "CLEAR"
    assert by_key[quiet_key].source == "quiet-period"
    assert near_key not in by_key, "frame close to a session must not be a negative"


def test_unclosed_session_still_excludes_frames_from_quiet(scene, tmp_path):
    """An open session has no end. If we let its later frames count as quiet
    negatives, the classifier would learn 'BLOCKED = CLEAR' for a live
    blockage. The window must stretch to the last frame we saw."""
    frames_dir, sessions_dir = scene
    camera_id = "odot-1234"
    crossing_id = "SE_11TH_MILWAUKIE"
    session_start = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

    inside_ts = session_start + timedelta(minutes=10)
    latest_ts = session_start + timedelta(minutes=45)  # this is now open_end
    _frame(frames_dir, camera_id, inside_ts)
    _frame(frames_dir, camera_id, latest_ts)

    sessions_file = sessions_dir / "sessions.jsonl"
    sessions_file.write_text(_session_line(crossing_id, "s-open", session_start, None) + "\n")
    gold = tmp_path / "gold.jsonl"
    gold.write_text("")

    examples = build_manifest(
        camera_id=camera_id,
        crossing_id=crossing_id,
        frames_dir=frames_dir,
        session_files=[sessions_file],
        sweep_file=None,
        gold_labels=gold,
    )

    keys = {ex.object_key for ex in examples}
    inside_key = _key(camera_id, inside_ts, f"{int(inside_ts.timestamp() * 1000)}-x.jpg")
    latest_key = _key(camera_id, latest_ts, f"{int(latest_ts.timestamp() * 1000)}-x.jpg")
    assert inside_key not in keys, "frames inside an open session cannot be labels"
    assert latest_key not in keys, (
        "frames after an open session with no known end must not become quiet negatives"
    )


def test_vlm_sweep_clear_frames_labeled(scene, tmp_path):
    """The VLM sweep verdicts are a second source of CLEAR labels; only high
    confidence entries are trusted."""
    frames_dir, sessions_dir = scene
    camera_id = "odot-1234"
    crossing_id = "SE_11TH_MILWAUKIE"
    base = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    session_start = base
    session_end = base + timedelta(minutes=10)

    # A frame near the session so quiet-negative would not pick it up.
    near_ts = session_end + timedelta(minutes=2)
    unconfident_ts = session_end + timedelta(minutes=3)
    frame_path = _frame(frames_dir, camera_id, near_ts)
    unconfident_path = _frame(frames_dir, camera_id, unconfident_ts)

    sessions_file = sessions_dir / "sessions.jsonl"
    sessions_file.write_text(_session_line(crossing_id, "s-1", session_start, session_end) + "\n")
    sweep_file = tmp_path / "sweep.jsonl"
    sweep_file.write_text(
        json.dumps(
            {
                "camera_id": camera_id,
                "object_key": f"frames/x/{frame_path.name}",
                "label": "CLEAR",
                "confidence": 0.9,
            }
        )
        + "\n"
        + json.dumps(
            {
                "camera_id": camera_id,
                "object_key": f"frames/x/{unconfident_path.name}",
                "label": "CLEAR",
                "confidence": 0.5,
            }
        )
        + "\n"
    )
    gold = tmp_path / "gold.jsonl"
    gold.write_text("")

    examples = build_manifest(
        camera_id=camera_id,
        crossing_id=crossing_id,
        frames_dir=frames_dir,
        session_files=[sessions_file],
        sweep_file=sweep_file,
        gold_labels=gold,
    )

    by_key = {ex.object_key: ex for ex in examples}
    near_key = _key(camera_id, near_ts, frame_path.name)
    unconfident_key = _key(camera_id, unconfident_ts, unconfident_path.name)
    assert near_key in by_key and by_key[near_key].label == "CLEAR"
    assert by_key[near_key].source == "vlm-sweep"
    assert unconfident_key not in by_key, (
        "sweep verdicts below 0.8 confidence must not become training labels"
    )


def test_session_windows_uses_latest_record_per_id(tmp_path):
    """Compacted session topics replay in order; the newest record wins.
    Anything else and a closed session could resurrect as open."""
    crossing_id = "X"
    started = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    ended = datetime(2026, 3, 1, 12, 30, tzinfo=UTC)
    file = tmp_path / "sessions.jsonl"
    file.write_text(
        _session_line(crossing_id, "s-1", started, None)
        + "\n"
        + _session_line(crossing_id, "s-1", started, ended)
        + "\n"
    )

    windows = session_windows([file], crossing_id, open_end=None)

    assert windows == [(started, ended, True)]


def test_manifest_written_as_jsonl(scene, tmp_path):
    frames_dir, sessions_dir = scene
    camera_id = "odot-1234"
    crossing_id = "SE_11TH_MILWAUKIE"
    quiet_ts = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    _frame(frames_dir, camera_id, quiet_ts)

    sessions_file = sessions_dir / "sessions.jsonl"
    sessions_file.write_text("")
    gold = tmp_path / "gold.jsonl"
    gold.write_text("")

    examples = build_manifest(
        camera_id=camera_id,
        crossing_id=crossing_id,
        frames_dir=frames_dir,
        session_files=[sessions_file],
        sweep_file=None,
        gold_labels=gold,
    )
    out = tmp_path / "manifest" / "m.jsonl"
    write_manifest(examples, out)

    lines = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert lines and lines[0]["camera_id"] == camera_id
    assert set(lines[0].keys()) == {"object_key", "camera_id", "label", "source", "split"}
