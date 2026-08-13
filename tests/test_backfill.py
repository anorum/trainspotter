"""Backfill planning: scan output to a loadable history window.

The pure half of the backfill path - windows, sessions, and the live-edge
guard - with no database involved. What the load does to Postgres is pinned
in test_history_db.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("asyncpg")

from api import backfill  # noqa: E402
from blockade.config import Camera, CameraRoster  # noqa: E402
from blockade.schemas import CrossingState, ObservationRecord  # noqa: E402

START = datetime(2026, 8, 9, 6, 0, tzinfo=UTC)
NOW = START + timedelta(hours=6)


def roster_of(*cameras: tuple[str, str, bool]) -> CameraRoster:
    """(camera_id, crossing_id, scores) triples as a roster."""
    return CameraRoster(
        cameras=[
            Camera(
                camera_id=camera_id,
                name=camera_id,
                crossing_id=crossing_id,
                image_url="https://tripcheck.example/cam.jpg",
                scores=scores,
            )
            for camera_id, crossing_id, scores in cameras
        ]
    )


CORRIDOR = roster_of(
    ("odot-678", "SE_12TH_CLINTON", True),
    ("odot-679", "SE_12TH_CLINTON", False),
    ("odot-681", "SE_8TH_DIVISION", True),
    ("odot-682", "SE_8TH_DIVISION", True),
)


def obs(
    minutes: float,
    state=CrossingState.BLOCKED,
    crossing="SE_12TH_CLINTON",
    camera="odot-678",
    version="classifier/abc123",
):
    return ObservationRecord(
        crossing_id=crossing,
        camera_id=camera,
        captured_at=START + timedelta(minutes=minutes),
        observed_at=START + timedelta(minutes=minutes),
        state=state,
        confidence=0.0 if state is CrossingState.UNKNOWN else 0.9,
        reason="x",
        object_key=f"frames/{camera}/{minutes}.jpg",
        detector_version=version,
    )


def test_window_spans_all_states_per_crossing():
    """The rebuild window is what the scan saw, not just what it flagged:
    a CLEAR frame at the edge widens the window because that span was
    genuinely re-judged."""
    records = [
        obs(0, state=CrossingState.CLEAR),
        obs(10),
        obs(15),
        obs(30, state=CrossingState.CLEAR),
        obs(5, crossing="SE_8TH_DIVISION", camera="odot-681"),
        obs(12, crossing="SE_8TH_DIVISION", camera="odot-681"),
    ]

    p = backfill.plan(records, now=NOW)

    windows = {w.crossing_id: w for w in p.windows}
    assert windows["SE_12TH_CLINTON"].start == START
    assert windows["SE_12TH_CLINTON"].end == START + timedelta(minutes=30)
    assert windows["SE_8TH_DIVISION"].start == START + timedelta(minutes=5)
    assert windows["SE_8TH_DIVISION"].end == START + timedelta(minutes=12)


def test_sessions_derive_with_streaming_parameters():
    """A blocked run that the streaming job would call a session must come
    out identical here - same gap, same minimums."""
    records = [obs(m) for m in range(0, 12, 2)] + [obs(40, state=CrossingState.CLEAR)]

    p = backfill.plan(records, now=NOW)

    assert len(p.sessions) == 1
    assert p.sessions[0].started_at == START
    assert p.sessions[0].duration_seconds == 10 * 60
    assert p.sessions[0].detector_version == "classifier/abc123"


def test_live_edge_is_refused():
    """A window reaching into the streaming sessionizer's horizon would close
    a genuinely open session; the plan refuses rather than warns."""
    records = [obs(0), obs(6)]

    with pytest.raises(backfill.BackfillError, match="live edge"):
        backfill.plan(records, now=START + timedelta(minutes=8))


def test_empty_input_is_an_error():
    with pytest.raises(backfill.BackfillError, match="no observations"):
        backfill.plan([], now=NOW)


def test_a_scan_missing_one_of_the_crossings_cameras_is_refused():
    """SE_8TH_DIVISION is watched by 681 and 682. A scan of 681 alone still
    builds a window spanning 681's rows, and the load would delete every
    session inside it - including the ones only 682 witnessed - replacing them
    with a derivation that never saw 682's frames."""
    records = [
        obs(m, crossing="SE_8TH_DIVISION", camera="odot-681") for m in range(0, 12, 2)
    ]

    with pytest.raises(backfill.BackfillError, match="odot-682"):
        backfill.plan(records, now=NOW, roster=CORRIDOR)


def test_rescoring_only_a_blind_camera_is_refused():
    """The follow-up this change commits to: re-scoring 679 alone produces
    nothing but unscored UNKNOWNs, derives no sessions, and would wipe 678's
    real trains out of the window with nothing to replace them."""
    records = [
        obs(m, state=CrossingState.UNKNOWN, camera="odot-679", version="unscored/1")
        for m in range(0, 12, 2)
    ]

    with pytest.raises(backfill.BackfillError, match="odot-678"):
        backfill.plan(records, now=NOW, roster=CORRIDOR)


def test_a_camera_that_drops_out_partway_through_the_window_is_refused():
    """Presence is not coverage. `scan` drops frames whose bytes have been swept
    out of the local TTL cache and reports only the survivors, so a multi-day
    re-score can yield 681 across the whole span and 682 for the tail alone.
    The window still spans everything 681 saw, so loading it would delete the
    days only 682 witnessed and re-derive them from a camera 300m away."""
    records = [obs(m, crossing="SE_8TH_DIVISION", camera="odot-681") for m in range(0, 240, 10)]
    records += [
        obs(m, crossing="SE_8TH_DIVISION", camera="odot-682") for m in range(180, 240, 10)
    ]

    with pytest.raises(backfill.BackfillError, match="odot-682 only"):
        backfill.plan(records, now=NOW, roster=CORRIDOR)

    assert backfill.plan(records, now=NOW, roster=CORRIDOR, allow_empty_window=True).windows


def test_a_short_hole_at_a_window_edge_is_tolerated():
    """The bound is one session gap, because a hole shorter than that cannot
    hide or split a session - refusing on a single missing tick would make the
    guard unusable against real corpora."""
    slack = backfill.COVERAGE_SLACK.total_seconds() / 60
    records = [obs(m, crossing="SE_8TH_DIVISION", camera="odot-681") for m in range(0, 240, 10)]
    records += [
        obs(m, crossing="SE_8TH_DIVISION", camera="odot-682")
        for m in range(int(slack) - 1, 240, 10)
    ]

    p = backfill.plan(records, now=NOW, roster=CORRIDOR)

    assert [w.crossing_id for w in p.windows] == ["SE_8TH_DIVISION"]


def test_a_scan_of_every_witness_plans_normally():
    """Both of the crossing's scoring cameras are present, and the blind one is
    not required - it has no judgement to contribute either way."""
    records = [obs(m) for m in range(0, 12, 2)] + [
        obs(m, crossing="SE_8TH_DIVISION", camera=camera)
        for m in range(0, 12, 2)
        for camera in ("odot-681", "odot-682")
    ]

    p = backfill.plan(records, now=NOW, roster=CORRIDOR)

    assert {w.crossing_id for w in p.windows} == {"SE_12TH_CLINTON", "SE_8TH_DIVISION"}


def test_the_override_loads_a_partial_scan_anyway():
    """A window predating a camera has no rows from it and never will; the
    operator says so explicitly rather than being blocked forever."""
    records = [obs(m, crossing="SE_8TH_DIVISION", camera="odot-681") for m in range(0, 12, 2)]

    p = backfill.plan(records, now=NOW, roster=CORRIDOR, allow_empty_window=True)

    assert [w.crossing_id for w in p.windows] == ["SE_8TH_DIVISION"]


def test_plan_rows_speak_the_kafka_dict_shape():
    """The load path is the materializer's load path: iso strings for
    timestamps in observation and session dicts, datetimes in the windows the
    DELETE binds directly."""
    records = [obs(m) for m in range(0, 12, 2)]

    rows_obs, rows_sess, rows_win = backfill.plan_rows(backfill.plan(records, now=NOW))

    assert isinstance(rows_obs[0]["captured_at"], str)
    assert datetime.fromisoformat(rows_obs[0]["captured_at"]) == START
    # Exactly the keys db._upsert binds; a rename in the schema breaks here,
    # not in production.
    assert {
        "session_id",
        "detector_version",
        "crossing_id",
        "started_at",
        "ended_at",
        "duration_seconds",
        "peak_queue_occupancy",
        "is_open",
    } <= rows_sess[0].keys()
    assert isinstance(rows_win[0]["window_start"], datetime)
    assert rows_win[0] == {
        "crossing_id": "SE_12TH_CLINTON",
        "window_start": START,
        "window_end": START + timedelta(minutes=10),
    }
