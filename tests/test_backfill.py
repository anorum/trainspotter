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
from blockade.schemas import CrossingState, ObservationRecord  # noqa: E402

START = datetime(2026, 8, 9, 6, 0, tzinfo=UTC)
NOW = START + timedelta(hours=6)


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
