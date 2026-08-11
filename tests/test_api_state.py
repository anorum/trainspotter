"""The live-state reducer's judgment calls, pinned.

The reducer decides what "right now" means for the UI. These tests feed it the
awkward record sequences the bus actually produces - late arrivals, compaction
duplicates, dead detectors - and pin the honest answer for each.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from blockade.api.state import LiveState
from blockade.schemas import BlockageSession, CrossingState, ObservationRecord

T0 = datetime(2026, 8, 11, 6, 0, tzinfo=UTC)
ROSTER = {
    "SE_12TH_CLINTON": [("odot-678", "12th at Clinton"), ("odot-679", "12th at Division")],
    "SE_8TH_DIVISION": [("odot-681", "8th at Division"), ("odot-682", "8th at Division Pl")],
}


def obs(minute: float, state: CrossingState, camera: str = "odot-678") -> ObservationRecord:
    crossing = next(c for c, cams in ROSTER.items() if any(cam == camera for cam, _ in cams))
    return ObservationRecord(
        crossing_id=crossing,
        camera_id=camera,
        captured_at=T0 + timedelta(minutes=minute),
        observed_at=T0 + timedelta(minutes=minute),
        state=state,
        confidence=0.9,
        reason="test",
        object_key=f"frames/{camera}/2026/08/11/06/{int(minute * 60_000)}-abcd1234.jpg",
        detector_version="test/1",
    )


def session(crossing: str, start_min: float, is_open: bool) -> BlockageSession:
    started = T0 + timedelta(minutes=start_min)
    return BlockageSession(
        session_id=BlockageSession.make_session_id(crossing, started),
        crossing_id=crossing,
        started_at=started,
        ended_at=None if is_open else started + timedelta(minutes=20),
        duration_seconds=None if is_open else 1200,
        peak_queue_occupancy=0.9,
        is_open=is_open,
        detector_version="test/1",
    )


def fresh() -> LiveState:
    return LiveState(ROSTER)


def test_a_fresh_board_is_unknown_and_stale() -> None:
    board = fresh().snapshot(now=T0)
    assert {c.crossing_id for c in board.crossings} == set(ROSTER)
    assert all(c.state is CrossingState.UNKNOWN and c.stale for c in board.crossings)
    # Cameras render from the roster even before any frame exists.
    assert all(len(c.cameras) == 2 for c in board.crossings)
    assert all(cam.object_key is None for c in board.crossings for cam in c.cameras)


def test_a_dead_detector_downgrades_to_unknown() -> None:
    """BLOCKED must never freeze on screen. Six minutes of silence and the
    board says UNKNOWN with the stale flag up."""
    state = fresh()
    state.apply_observation(obs(0, CrossingState.BLOCKED))

    live = state.snapshot(now=T0 + timedelta(minutes=3))
    dead = state.snapshot(now=T0 + timedelta(minutes=10))

    clinton_live = next(c for c in live.crossings if c.crossing_id == "SE_12TH_CLINTON")
    clinton_dead = next(c for c in dead.crossings if c.crossing_id == "SE_12TH_CLINTON")
    assert clinton_live.state is CrossingState.BLOCKED and not clinton_live.stale
    assert clinton_dead.state is CrossingState.UNKNOWN and clinton_dead.stale
    # The raw observation is still exposed - the downgrade is honesty, not
    # amnesia.
    assert clinton_dead.latest_observation is not None
    assert clinton_dead.latest_observation.state is CrossingState.BLOCKED


def test_late_arrivals_never_regress_the_present() -> None:
    """Two cameras on one crossing drift; the older record must not overwrite
    the newer state, but it still lands in the history buffer."""
    state = fresh()
    state.apply_observation(obs(10, CrossingState.CLEAR, "odot-678"))
    state.apply_observation(obs(8, CrossingState.BLOCKED, "odot-679"))  # late

    board = state.snapshot(now=T0 + timedelta(minutes=11))
    clinton = next(c for c in board.crossings if c.crossing_id == "SE_12TH_CLINTON")
    assert clinton.state is CrossingState.CLEAR
    joined = state.recent_observations(
        "SE_12TH_CLINTON", T0, T0 + timedelta(minutes=11)
    )
    assert len(joined) == 2, "the late record still serves history joins"


def test_since_tracks_state_transitions() -> None:
    state = fresh()
    state.apply_observation(obs(0, CrossingState.CLEAR))
    state.apply_observation(obs(2, CrossingState.CLEAR))
    state.apply_observation(obs(4, CrossingState.BLOCKED))

    board = state.snapshot(now=T0 + timedelta(minutes=5))
    clinton = next(c for c in board.crossings if c.crossing_id == "SE_12TH_CLINTON")
    assert clinton.since == T0 + timedelta(minutes=4)


def test_compaction_duplicates_converge() -> None:
    """Replaying an uncompacted sessions topic applies the same session id
    several times; the last write wins and nothing duplicates."""
    state = fresh()
    open_emission = session("SE_12TH_CLINTON", 0, is_open=True)
    closed_emission = session("SE_12TH_CLINTON", 0, is_open=False)
    state.apply_session(open_emission)
    state.apply_session(open_emission)
    state.apply_session(closed_emission)

    assert len(state.sessions()) == 1
    assert state.sessions()[0].is_open is False
    board = state.snapshot(now=T0 + timedelta(minutes=1))
    clinton = next(c for c in board.crossings if c.crossing_id == "SE_12TH_CLINTON")
    assert clinton.open_session is None


def test_open_session_surfaces_on_its_crossing_only() -> None:
    state = fresh()
    state.apply_session(session("SE_8TH_DIVISION", 0, is_open=True))

    board = state.snapshot(now=T0 + timedelta(minutes=1))
    division = next(c for c in board.crossings if c.crossing_id == "SE_8TH_DIVISION")
    clinton = next(c for c in board.crossings if c.crossing_id == "SE_12TH_CLINTON")
    assert division.open_session is not None and division.open_session.is_open
    assert clinton.open_session is None


def test_latest_frame_follows_the_newest_scored_frame() -> None:
    state = fresh()
    first, second = obs(0, CrossingState.CLEAR), obs(2, CrossingState.CLEAR)
    state.apply_observation(first)
    state.apply_observation(second)

    board = state.snapshot(now=T0 + timedelta(minutes=3))
    clinton = next(c for c in board.crossings if c.crossing_id == "SE_12TH_CLINTON")
    cam = next(c for c in clinton.cameras if c.camera_id == "odot-678")
    assert cam.object_key == second.object_key
    assert cam.captured_at == second.captured_at


def test_changed_flag_drives_the_sse_fanout() -> None:
    """A repeated identical session is not a change; a state flip is."""
    state = fresh()
    state.apply_observation(obs(0, CrossingState.CLEAR))
    state.changed = False

    state.apply_session(session("SE_12TH_CLINTON", 0, is_open=True))
    assert state.changed
    state.changed = False
    state.apply_session(session("SE_12TH_CLINTON", 0, is_open=True))
    assert not state.changed

    state.apply_observation(obs(2, CrossingState.BLOCKED))
    assert state.changed
