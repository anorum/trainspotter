"""The spot-check sweep's logic, driven by a scripted model.

The property that matters: a coarse stride plus edge-walking recovers the full
extent of a blockage the stride only clipped, spends no extra calls on quiet
stretches, and never re-judges a frame. UNKNOWN inside a blockage must not end
the walk - the same absence-of-evidence rule the alerter uses.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from blockade.config import Camera
from blockade.schemas import CrossingState
from detector.spotcheck import (
    CLEAR_STREAK_TO_STOP,
    Judged,
    append_labels,
    label_record,
    stride_indices,
    sweep_camera,
)

T0 = datetime(2026, 8, 11, 5, 0, tzinfo=UTC)
CAMERA = Camera(camera_id="odot-681", name="t", crossing_id="SE_8TH_DIVISION", image_url="http://example.test/x.jpg")


def timeline(tmp_path: Path, n: int = 30, cadence_minutes: int = 2) -> list[Path]:
    """n fake frames at a fixed cadence. Files exist but are never read: the
    scripted judge answers from the timeline, not the bytes."""
    frames = []
    for i in range(n):
        t = T0 + timedelta(minutes=i * cadence_minutes)
        p = tmp_path / f"{int(t.timestamp() * 1000)}-{i:08x}.jpg"
        p.write_bytes(b"x")
        frames.append(p)
    return frames


def scripted(blocked: set[int], unknown: set[int] = frozenset()):
    """A judge that answers from index membership and counts its calls."""
    calls: list[int] = []

    def judge(path: Path) -> Judged:
        from detector.spotcheck import frame_time

        i = round((frame_time(path) - T0).total_seconds() / 120)
        calls.append(i)
        state = (
            CrossingState.BLOCKED
            if i in blocked
            else CrossingState.UNKNOWN
            if i in unknown
            else CrossingState.CLEAR
        )
        return Judged(path, frame_time(path), state, 0.9, "scripted")

    judge.calls = calls
    return judge


def states(verdicts: list[Judged]) -> dict[int, str]:
    return {
        round((v.captured_at - T0).total_seconds() / 120): v.state.value for v in verdicts
    }


def test_edge_walk_recovers_the_full_blockage(tmp_path: Path) -> None:
    """Train spans indices 10-17; the 15-minute stride samples every ~8th
    frame, so at most one sample lands inside. The walk must still label the
    whole extent plus CLEAR shoulders."""
    frames = timeline(tmp_path)
    judge = scripted(blocked=set(range(10, 18)))

    verdicts = sweep_camera(CAMERA, frames, judge, stride_minutes=15)

    got = states(verdicts)
    for i in range(10, 18):
        assert got.get(i) == "BLOCKED", f"index {i} missing from the recovered extent"
    below = [i for i in got if i < 10]
    above = [i for i in got if i >= 18]
    assert sum(1 for i in below if got[i] == "CLEAR") >= CLEAR_STREAK_TO_STOP
    assert sum(1 for i in above if got[i] == "CLEAR") >= CLEAR_STREAK_TO_STOP


def test_quiet_corpus_spends_only_the_stride(tmp_path: Path) -> None:
    frames = timeline(tmp_path)
    judge = scripted(blocked=set())

    verdicts = sweep_camera(CAMERA, frames, judge, stride_minutes=15)

    assert len(judge.calls) == len(set(judge.calls)), "no frame judged twice"
    assert len(verdicts) <= 5, "a quiet timeline costs only the coarse samples"


def test_unknown_inside_a_blockage_does_not_stop_the_walk(tmp_path: Path) -> None:
    """A glare-ruined frame mid-train is absence of evidence, not a boundary."""
    frames = timeline(tmp_path)
    # The stride samples index 8; the walk crosses the UNKNOWN at 10 and must
    # keep going to find the far edge of the blockage at 12.
    judge = scripted(blocked={8, 9, 11, 12}, unknown={10})

    verdicts = sweep_camera(CAMERA, frames, judge, stride_minutes=15)

    got = states(verdicts)
    assert got.get(8) == "BLOCKED" and got.get(12) == "BLOCKED"
    assert got.get(10) == "UNKNOWN", "the unreadable frame is recorded honestly"


def test_no_frame_is_judged_twice_even_when_walks_overlap(tmp_path: Path) -> None:
    frames = timeline(tmp_path)
    judge = scripted(blocked=set(range(8, 20)))

    sweep_camera(CAMERA, frames, judge, stride_minutes=15)

    assert len(judge.calls) == len(set(judge.calls))


def test_append_labels_is_idempotent(tmp_path: Path) -> None:
    frames = timeline(tmp_path, n=2)
    judge = scripted(blocked=set())
    verdicts = sweep_camera(CAMERA, frames, judge, stride_minutes=1)
    records = [
        label_record(CAMERA, v, f"frames/odot-681/x/{v.path.name}", "haiku/test")
        for v in verdicts
    ]
    labels = tmp_path / "labels.jsonl"

    first = append_labels(records, labels)
    second = append_labels(records, labels)

    assert first == len(records) and second == 0


def test_blockage_strictly_between_stride_samples_is_missed(tmp_path: Path) -> None:
    """Honest limitation: a blockage shorter than the stride can fall between
    samples. With a 15-minute stride on the 2-minute-cadence timeline the
    stride lands on indices 0, 8, 16, 24; a blockage at {2, 3} lies strictly
    between the first two samples and both endpoints read CLEAR, so no walk is
    triggered and those frames are never judged."""
    frames = timeline(tmp_path)
    judge = scripted(blocked={2, 3})

    verdicts = sweep_camera(CAMERA, frames, judge, stride_minutes=15)

    got = states(verdicts)
    assert 2 not in got and 3 not in got, "between-stride blockage is not recovered"
    assert 2 not in judge.calls and 3 not in judge.calls, "and no API call is spent on it"


def test_already_labeled_frames_are_skipped_before_judging(tmp_path: Path) -> None:
    """Repeat sweeps must not re-spend calls on frames the label file already
    covers. Object_keys the caller declares already-labeled are short-circuited
    before the judge is invoked."""
    frames = timeline(tmp_path)
    judge = scripted(blocked=set(range(10, 18)))

    def object_key_for(path: Path) -> str:
        return path.name

    prior = {frames[i].name for i in range(10, 18)}
    verdicts = sweep_camera(
        CAMERA,
        frames,
        judge,
        stride_minutes=15,
        already_labeled=prior,
        object_key_for=object_key_for,
    )

    got = states(verdicts)
    for i in range(10, 18):
        assert i not in got, f"already-labeled index {i} must not be re-emitted"
        assert i not in judge.calls, f"already-labeled index {i} must not be re-judged"


def test_stride_handles_irregular_cadence(tmp_path: Path) -> None:
    """Real cameras skip beats; the stride keys off time, not index."""
    sparse = [p for i, p in enumerate(timeline(tmp_path)) if i % 3 != 1]
    picks = stride_indices(sparse, stride_minutes=15)
    assert picks[0] == 0
    times = [sparse[i] for i in picks]
    assert len(times) >= 3


ROSTER_YAML = """
cameras:
  - camera_id: odot-678
    name: Portland - 12th at Clinton
    crossing_id: SE_12TH_CLINTON
    image_url: http://example.test/678.jpg
  - camera_id: odot-681
    name: Portland - 8th at Division
    crossing_id: SE_8TH_DIVISION
    image_url: http://example.test/681.jpg
"""


def test_the_cli_labels_each_camera_under_its_own_id(tmp_path, monkeypatch) -> None:
    """A sweep over several cameras must attribute every label to the camera
    whose directory the frame came from.

    The judge and the object-key builder are per-camera functions the sweep
    calls back into, and the camera they carry is what lands in the label
    file. Bind that camera once for the whole loop and every label - crossing
    id, camera id, object key - is filed under whichever camera happened to be
    last, silently poisoning the training set.
    """
    import blockade.detect.vlm as vlm_module
    from blockade.schemas import ObservationRecord
    from detector.runner import app
    from typer.testing import CliRunner

    class FakeVlm:
        version = "fake-model/deadbeef"

        def __init__(self, settings) -> None:
            self.seen: list[str] = []

        def classify(self, image, camera, captured_at, object_key) -> ObservationRecord:
            self.seen.append(camera.camera_id)
            return ObservationRecord(
                crossing_id=camera.crossing_id,
                camera_id=camera.camera_id,
                captured_at=captured_at,
                observed_at=captured_at,
                state=CrossingState.CLEAR,
                confidence=0.9,
                reason=f"scripted for {camera.camera_id}",
                object_key=object_key,
                detector_version=self.version,
            )

    monkeypatch.setattr(vlm_module, "VlmDetector", FakeVlm)
    roster = tmp_path / "cameras.yaml"
    roster.write_text(ROSTER_YAML)
    monkeypatch.setenv("BLOCKADE_CAMERA_CONFIG_PATH", str(roster))

    frames_dir = tmp_path / "frames"
    for camera_id in ("odot-678", "odot-681"):
        camera_dir = frames_dir / camera_id
        camera_dir.mkdir(parents=True)
        timeline(camera_dir, n=8, cadence_minutes=15)
    labels = tmp_path / "labels.jsonl"

    result = CliRunner().invoke(
        app,
        ["spotcheck", "--frames-dir", str(frames_dir), "--labels", str(labels)],
    )

    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in labels.read_text().splitlines() if line.strip()]
    assert records, "the sweep judged nothing"
    for record in records:
        assert record["object_key"].startswith(f"frames/{record['camera_id']}/"), record
        assert record["reason"] == f"scripted for {record['camera_id']}", record
    assert {r["camera_id"] for r in records} == {"odot-678", "odot-681"}
    assert {r["crossing_id"] for r in records} == {"SE_12TH_CLINTON", "SE_8TH_DIVISION"}
