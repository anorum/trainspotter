"""The streaming detector's contract.

The property everything else rests on: streaming and scan are the same scoring
path, so the same frame records produce identical observations whichever route
they arrive by. Plus the delivery semantics: offsets commit only after the
produced observations are acked, and a poison message is skipped rather than
allowed to wedge its partition.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from blockade.config import Settings
from blockade.schemas import CapturedAtSource, CrossingState, FetchStatus, FrameRecord
from detector.runner import UNSCORED_VERSION, Scorer, score_frames

FIXTURES = Path(__file__).parent / "fixtures" / "frames"
CLEAR_NIGHT = FIXTURES / "odot-678" / "clear-night.jpg"


def frame(camera_id: str, minute: int, status: FetchStatus = FetchStatus.OK) -> FrameRecord:
    captured = datetime(2026, 8, 10, 18, 0, tzinfo=UTC) + timedelta(minutes=minute)
    return FrameRecord(
        camera_id=camera_id,
        crossing_id="SE_12TH_CLINTON",
        captured_at=captured,
        captured_at_source=CapturedAtSource.LAST_MODIFIED,
        fetched_at=captured,
        fetch_status=status,
        object_key=None if status is FetchStatus.ERROR else f"frames/{camera_id}/x/{minute}.jpg",
        poller_version="test",
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """References built from the committed fixture, cache primed with it."""
    from blockade.detect.reference import ReferenceModel

    refs = tmp_path / "references"
    refs.mkdir()
    ReferenceModel.build("odot-678", [CLEAR_NIGHT.read_bytes()] * 12, min_samples=10).save(refs)
    return Settings(
        odot_api_key=None,
        references_dir=refs,
        local_cache_dir=tmp_path / "cache",
        detector="reference",
    )


def prime_cache(settings: Settings, record: FrameRecord) -> None:
    assert record.object_key is not None
    path = settings.local_cache_dir / record.object_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(CLEAR_NIGHT.read_bytes())


def read_from_cache(settings: Settings):
    def read(object_key: str) -> bytes | None:
        path = settings.local_cache_dir / object_key
        return path.read_bytes() if path.exists() else None

    return read


def test_streaming_and_scan_agree_frame_for_frame(settings: Settings) -> None:
    """Same records, both routes, identical observations - the replay property."""
    records = [frame("odot-678", m) for m in range(3)]
    for r in records:
        prime_cache(settings, r)

    via_scan = score_frames(records, settings)
    scorer = Scorer(settings, read_from_cache(settings))
    via_stream = [obs for r in records if (obs := scorer.score(r)) is not None]

    assert len(via_scan) == len(via_stream) == 3
    for a, b in zip(via_scan, via_stream, strict=True):
        # observed_at is wall-clock processing time and legitimately differs
        # between runs; everything derived from the frame itself must not.
        assert a.model_dump(exclude={"observed_at"}) == b.model_dump(exclude={"observed_at"})


def test_duplicates_and_errors_are_not_scored(settings: Settings) -> None:
    scorer = Scorer(settings, read_from_cache(settings))

    ok = frame("odot-678", 0)
    prime_cache(settings, ok)
    assert scorer.score(ok) is not None
    assert scorer.score(frame("odot-678", 1, FetchStatus.NOT_MODIFIED)) is None
    assert scorer.score(frame("odot-678", 2, FetchStatus.DUPLICATE)) is None
    assert scorer.score(frame("odot-678", 3, FetchStatus.ERROR)) is None


def test_missing_bytes_skip_rather_than_raise(settings: Settings) -> None:
    """A frame in the topic whose bytes are gone must not stall the partition."""
    scorer = Scorer(settings, read_from_cache(settings))
    assert scorer.score(frame("odot-678", 0)) is None  # cache never primed


def test_unknown_camera_is_skipped(settings: Settings) -> None:
    record = frame("odot-9999", 0)
    prime_cache(settings, record)
    scorer = Scorer(settings, read_from_cache(settings))
    assert scorer.score(record) is None


def test_a_scored_fixture_frame_reads_clear(settings: Settings) -> None:
    """End to end through the scorer on real imagery: the frame matches the
    reference built from itself, so anything but CLEAR is a regression."""
    record = frame("odot-678", 0)
    prime_cache(settings, record)
    scorer = Scorer(settings, read_from_cache(settings))

    observation = scorer.score(record)

    assert observation is not None
    assert observation.state is CrossingState.CLEAR
    assert observation.crossing_id == "SE_12TH_CLINTON"


def test_a_non_scoring_camera_emits_an_inert_unknown(settings: Settings) -> None:
    """odot-679 watches the Division intersection, so it must not vote. The
    board still needs its frames, so the scorer mints an UNKNOWN without
    reading bytes or consulting a model, stamped as policy not as failure."""

    def forbidden_read(object_key: str) -> bytes | None:
        raise AssertionError("a non-scoring camera's bytes must never be read")

    scorer = Scorer(settings, forbidden_read)
    obs = scorer.score(frame("odot-679", 0))

    assert obs is not None
    assert obs.state is CrossingState.UNKNOWN
    assert obs.confidence == 0.0
    assert obs.detector_version == UNSCORED_VERSION
    assert obs.object_key == "frames/odot-679/x/0.jpg"


def test_explain_prints_what_the_pod_would_publish_for_a_non_scoring_camera(
    tmp_path: Path, monkeypatch
) -> None:
    """explain promises its output is the record the pod would have published
    for the same bytes. Running the real model on a camera the pod never scores
    would hand the operator debugging that very camera a confident BLOCKED or
    CLEAR the pipeline cannot emit."""
    import detector.runner as runner
    from typer.testing import CliRunner

    roster = tmp_path / "cameras.yaml"
    roster.write_text(
        "cameras:\n"
        "  - camera_id: odot-679\n"
        "    name: Portland - 12th at Division\n"
        "    crossing_id: SE_12TH_CLINTON\n"
        "    image_url: http://example.test/679.jpg\n"
        "    scores: false\n"
    )
    monkeypatch.setattr(
        runner, "get_settings", lambda: Settings(camera_config_path=roster, detector="reference")
    )
    monkeypatch.setattr(
        runner,
        "build_detector",
        lambda **kwargs: pytest.fail("a non-scoring camera must consult no model"),
    )
    image = tmp_path / "odot-679" / "frame.jpg"
    image.parent.mkdir()
    image.write_bytes(b"never read")

    result = CliRunner().invoke(runner.app, ["explain", str(image)])

    assert result.exit_code == 0, result.output
    assert "state=UNKNOWN" in result.stdout
    assert "confidence=0.00" in result.stdout
    assert f"detector_version={UNSCORED_VERSION}" in result.stdout
