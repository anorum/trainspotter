"""Session derivation - the logic the streaming sessionizer must reproduce."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from blockade.schemas import CrossingState, ObservationRecord
from blockade.sessions import DEFAULT_GAP, SessionParams, derive_sessions, suggest_gap

START = datetime(2026, 8, 9, 18, 56, tzinfo=UTC)


def obs(minutes: float, state=CrossingState.BLOCKED, crossing="SE_12TH_CLINTON"):
    return ObservationRecord(
        crossing_id=crossing,
        camera_id="odot-678",
        captured_at=START + timedelta(minutes=minutes),
        observed_at=START + timedelta(minutes=minutes),
        state=state,
        confidence=0.0 if state is CrossingState.UNKNOWN else 0.9,
        reason="x",
        object_key=f"frames/odot-678/{minutes}.jpg",
        detector_version="reference/test",
    )


def test_a_continuous_blockage_is_one_session():
    """Frames arrive every ~2.2 min through a 55-minute blockage."""
    sessions = derive_sessions([obs(m) for m in range(0, 56, 2)])

    assert len(sessions) == 1
    assert sessions[0].duration_seconds == 54 * 60


def test_a_sampling_gap_does_not_split_a_session():
    """The bug this exists to prevent: the camera's own refresh reached 319s
    while the gap was 5 min, so a pause in sampling split a real 55-minute
    blockage into three sessions."""
    times = [0, 2, 4, 5.3, 10.6, 12, 14, 16, 20, 25.3, 30, 35, 40, 45, 50, 55]
    sessions = derive_sessions([obs(m) for m in times])

    assert len(sessions) == 1, "a slow camera is not a cleared crossing"
    assert sessions[0].duration_seconds == 55 * 60


def test_genuinely_separate_blockages_stay_separate():
    early = [obs(m) for m in range(0, 20, 2)]
    late = [obs(m) for m in range(60, 80, 2)]

    sessions = derive_sessions(early + late)

    assert len(sessions) == 2


def test_short_detections_are_dropped():
    """Eight of eighteen sessions found on one camera in nineteen hours were
    under five minutes, and none were real. docs/history/design.md scopes them
    out."""
    sessions = derive_sessions([obs(0), obs(2), obs(3)])

    assert sessions == []


def test_a_single_blocked_frame_is_never_a_session():
    assert derive_sessions([obs(0)]) == []


def test_unknown_neither_extends_nor_ends_a_session():
    """An unreadable frame is absence of evidence. Treating it as CLEAR would
    close sessions during weather; as BLOCKED would invent them."""
    with_gap = [obs(0), obs(2), obs(4)] + [obs(6, CrossingState.UNKNOWN)] + [obs(8), obs(10)]

    sessions = derive_sessions(with_gap)

    assert len(sessions) == 1
    assert sessions[0].duration_seconds == 10 * 60


def test_clear_readings_do_not_end_a_session_early():
    """Only elapsed time closes a session, so one bad CLEAR mid-blockage cannot
    truncate it."""
    mixed = [obs(0), obs(2), obs(4, CrossingState.CLEAR), obs(6), obs(8)]

    sessions = derive_sessions(mixed)

    assert len(sessions) == 1
    assert sessions[0].duration_seconds == 8 * 60


def test_crossings_are_independent():
    a = [obs(m, crossing="SE_12TH_CLINTON") for m in range(0, 20, 2)]
    b = [obs(m, crossing="SE_11TH_MILWAUKIE") for m in range(0, 20, 2)]

    sessions = derive_sessions(a + b)

    assert {s.crossing_id for s in sessions} == {"SE_12TH_CLINTON", "SE_11TH_MILWAUKIE"}


def test_session_ids_are_stable_across_reruns():
    """Sessions are a derived view rebuilt whenever parameters change, so the
    same input must always produce the same identity or every downstream upsert
    duplicates."""
    rows = [obs(m) for m in range(0, 20, 2)]

    assert [s.session_id for s in derive_sessions(rows)] == [
        s.session_id for s in derive_sessions(rows)
    ]


def test_suggested_gap_clears_the_worst_observed_interval():
    """Derived from cadence, not chosen from blockage semantics."""
    rows = [obs(m) for m in [0, 2, 4, 9.3, 11, 13]]  # a 5.3-minute sampling gap

    gap = suggest_gap(rows)

    assert gap >= timedelta(minutes=10.6), "must clear the worst interval with margin"
    assert suggest_gap([obs(0)]) == DEFAULT_GAP, "too little data falls back to the default"


def test_tighter_parameters_rebuild_differently():
    """Parameters live in data precisely so they can be changed and replayed."""
    rows = [obs(m) for m in [0, 2, 4, 20, 22, 24]]

    wide = derive_sessions(rows, SessionParams(gap=timedelta(minutes=30)))
    narrow = derive_sessions(rows, SessionParams(gap=timedelta(minutes=10)))

    assert len(wide) == 1
    assert len(narrow) == 0, "both halves fall under the 5-minute minimum once split"
