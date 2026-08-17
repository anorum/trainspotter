"""The live-state reducer's judgment calls, pinned.

The reducer decides what "right now" means for the UI. These tests feed it the
awkward record sequences the bus actually produces - late arrivals, compaction
duplicates, dead detectors - and pin the honest answer for each.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from blockade.api.state import DEFAULT_STALE_AFTER, LiveState
from blockade.schemas import BlockageSession, CrossingState, ObservationRecord

T0 = datetime(2026, 8, 11, 6, 0, tzinfo=UTC)

STALE_AFTER = timedelta(minutes=6)
"""The bound these tests reason against, stated rather than inherited.

What is pinned here is the *rule* - what a bound does to consensus - not the policy
number, which moves with the camera cadence. Passing it in keeps each scenario's
arithmetic readable on the page and stops a policy change from silently retiming every
test below. ``test_the_default_bound_is_the_shipped_policy`` pins the number itself.
"""

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
    return LiveState(ROSTER, stale_after=STALE_AFTER)


def test_the_default_bound_is_the_shipped_policy() -> None:
    """The one test that cares about the number. Its twin is STALE_AFTER_MS in
    web/src/lib/scrub.ts: the scrubbed board and the live board answer "did this
    instant have a witness" separately, so the two constants have to agree."""
    bound_minutes = DEFAULT_STALE_AFTER.total_seconds() / 60
    assert bound_minutes == 15


def test_a_fresh_board_is_unknown_and_stale() -> None:
    board = fresh().snapshot(now=T0)
    assert {c.crossing_id for c in board.crossings} == set(ROSTER)
    assert all(c.state is CrossingState.UNKNOWN and c.stale for c in board.crossings)
    # Cameras render from the roster even before any frame exists.
    assert all(len(c.cameras) == 2 for c in board.crossings)
    assert all(cam.object_key is None for c in board.crossings for cam in c.cameras)


def test_a_dead_detector_downgrades_to_unknown() -> None:
    """BLOCKED must never freeze on screen. Once the bound passes with no word
    from any camera, the board says UNKNOWN with the stale flag up."""
    state = fresh()
    state.apply_observation(obs(0, CrossingState.BLOCKED))

    live = state.snapshot(now=T0 + timedelta(minutes=3))
    dead = state.snapshot(now=T0 + timedelta(minutes=10))

    clinton_live = next(c for c in live.crossings if c.crossing_id == "SE_12TH_CLINTON")
    clinton_dead = next(c for c in dead.crossings if c.crossing_id == "SE_12TH_CLINTON")
    assert clinton_live.state is CrossingState.BLOCKED and not clinton_live.stale
    assert clinton_dead.state is CrossingState.UNKNOWN and clinton_dead.stale
    assert clinton_dead.latest_observation is None, (
        "no fresh observation exists; the stale flag says why"
    )


def test_late_arrivals_never_regress_a_camera() -> None:
    """Per camera, an older record must not overwrite a newer one - but a
    late BLOCKED from the *other* camera still joins consensus, because each
    camera's latest fresh word counts."""
    state = fresh()
    state.apply_observation(obs(10, CrossingState.CLEAR, "odot-678"))
    state.apply_observation(obs(8, CrossingState.BLOCKED, "odot-679"))  # late, other camera

    board = state.snapshot(now=T0 + timedelta(minutes=11))
    clinton = next(c for c in board.crossings if c.crossing_id == "SE_12TH_CLINTON")
    assert clinton.state is CrossingState.BLOCKED, "679's fresh BLOCKED joins the vote"

    state.apply_observation(obs(6, CrossingState.BLOCKED, "odot-678"))  # older than 678's own
    board = state.snapshot(now=T0 + timedelta(minutes=11))
    clinton = next(c for c in board.crossings if c.crossing_id == "SE_12TH_CLINTON")
    assert clinton.latest_observation is not None
    assert clinton.latest_observation.captured_at == T0 + timedelta(minutes=8), (
        "678's own older record must not replace its newer one"
    )


def test_one_camera_seeing_a_train_outranks_one_seeing_nothing() -> None:
    """The 2026-08-10 incident, pinned: 678 confirmed a train at :37 and
    679, glare-blind, said CLEAR two seconds later. Latest-wins showed CLEAR
    while a train crossed. Blocked-biased consensus holds BLOCKED."""
    state = fresh()
    state.apply_observation(obs(45.62, CrossingState.BLOCKED, "odot-678"))
    state.apply_observation(obs(45.65, CrossingState.CLEAR, "odot-679"))

    board = state.snapshot(now=T0 + timedelta(minutes=46))
    clinton = next(c for c in board.crossings if c.crossing_id == "SE_12TH_CLINTON")
    assert clinton.state is CrossingState.BLOCKED
    assert clinton.latest_observation is not None
    assert clinton.latest_observation.camera_id == "odot-678"


def test_a_stale_blocked_does_not_hold_the_crossing_hostage() -> None:
    """A camera that said BLOCKED and then went silent drops out of the vote
    once stale; a fresh CLEAR from the other camera then carries."""
    state = fresh()
    state.apply_observation(obs(0, CrossingState.BLOCKED, "odot-678"))
    state.apply_observation(obs(9, CrossingState.CLEAR, "odot-679"))

    board = state.snapshot(now=T0 + timedelta(minutes=10))
    clinton = next(c for c in board.crossings if c.crossing_id == "SE_12TH_CLINTON")
    assert clinton.state is CrossingState.CLEAR, "the 10-minute-old BLOCKED is a memory"
    assert not clinton.stale, "a fresh CLEAR keeps the crossing fresh"


def test_unknown_does_not_veto_clear() -> None:
    """Absence of evidence from one camera is not evidence against the other:
    682 cannot resolve the tracks at night, and its UNKNOWN must not drag a
    fresh CLEAR down."""
    state = fresh()
    state.apply_observation(obs(0, CrossingState.UNKNOWN, "odot-681"))
    state.apply_observation(obs(1, CrossingState.CLEAR, "odot-682"))

    board = state.snapshot(now=T0 + timedelta(minutes=2))
    division = next(c for c in board.crossings if c.crossing_id == "SE_8TH_DIVISION")
    assert division.state is CrossingState.CLEAR


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


def test_a_non_scoring_cameras_heartbeat_cannot_hide_a_dead_witness():
    """677/679 emit permanent zero-inference UNKNOWNs so the board keeps their
    pictures. Those heartbeats must not count as fresh witnesses: with the
    real camera dead, the crossing must read stale, or a dead detector could
    hide behind the blind camera's ticking forever."""
    state = LiveState(
        {"SE_12TH_CLINTON": [("odot-678", "Clinton"), ("odot-679", "Division view")]},
        stale_after=STALE_AFTER,
        scoring={"odot-678"},
    )
    state.apply_observation(obs(0, CrossingState.BLOCKED, "odot-678"))
    # The real witness dies; the blind camera keeps ticking UNKNOWN.
    for m in (2, 4, 6, 8, 10):
        state.apply_observation(obs(m, CrossingState.UNKNOWN, "odot-679"))

    board = state.snapshot(now=T0 + timedelta(minutes=11))
    clinton = next(c for c in board.crossings if c.crossing_id == "SE_12TH_CLINTON")
    assert clinton.state is CrossingState.UNKNOWN
    assert clinton.stale, "the heartbeat is not a witness; the crossing is genuinely dark"
    # The pictures still flow: the blind camera's frames stay on the panel.
    division_view = next(c for c in clinton.cameras if c.camera_id == "odot-679")
    assert division_view.object_key is not None
