"""The Postgres history store: versioned rows, latest-ingest-wins reads.

Exercises db.py against a real Postgres. The whole point of the store is
that a better detector's word can override an earlier version's: observation
layers resolve latest-ingest-wins per (camera, instant), and a backfill
rebuilds the session projection over the window it re-scored. These tests
write the layers in the wrong order and pin that the newer word always wins
the read.

The Postgres URL comes from BLOCKADE_TEST_DATABASE_URL. Without it the
suite is skipped so the tests never fail on a workstation that lacks the
database; the CI job that owns this store sets it.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("asyncpg")

from api import db  # noqa: E402

DSN = os.environ.get("BLOCKADE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set BLOCKADE_TEST_DATABASE_URL to run history-store tests"
)

T0 = datetime(2026, 8, 11, 6, 0, tzinfo=UTC)


def _obs(minute: float, *, state: str, detector_version: str, camera: str = "odot-678") -> dict:
    return {
        "crossing_id": "SE_12TH_CLINTON",
        "camera_id": camera,
        "captured_at": (T0 + timedelta(minutes=minute)).isoformat(),
        "detector_version": detector_version,
        "state": state,
        "confidence": 0.9,
        "reason": "test",
        "object_key": f"frames/{camera}/2026/08/11/06/{int(minute * 60_000)}-abcd1234.jpg",
    }


def _sess(
    session_id: str,
    *,
    detector_version: str,
    is_open: bool,
    started_min: float = 0,
    crossing_id: str = "SE_12TH_CLINTON",
) -> dict:
    started = T0 + timedelta(minutes=started_min)
    ended = None if is_open else started + timedelta(minutes=20)
    return {
        "session_id": session_id,
        "detector_version": detector_version,
        "crossing_id": crossing_id,
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat() if ended else None,
        "duration_seconds": None if is_open else 1200,
        "peak_queue_occupancy": 0.7,
        "is_open": is_open,
    }


@pytest.fixture
async def pool():
    p = await db.connect(DSN)
    # Every test lives in its own crossing/session namespace so a shared DB
    # never bleeds across cases.
    async with p.acquire() as conn:
        await conn.execute("TRUNCATE observations, sessions")
    try:
        yield p
    finally:
        await p.close()


async def test_schema_bootstrap_is_idempotent() -> None:
    """connect() runs CREATE TABLE IF NOT EXISTS - it must be safe to call
    against an already-initialized database, which is exactly what happens on
    a pod restart."""
    p1 = await db.connect(DSN)
    p2 = await db.connect(DSN)
    await p1.close()
    await p2.close()


async def test_a_newer_detector_ingest_overrides_the_older_one_per_instant(pool) -> None:
    """The retroactive-honesty guarantee: v1 said BLOCKED at 06:05, v2 said
    CLEAR for the same instant. The later ingest must be what the timeline
    returns, with the detector_version tag recording why."""
    await db.upsert_batch(pool, [_obs(5, state="BLOCKED", detector_version="motion/1")], [])
    # A perceptible gap so ingested_at monotonicity is unambiguous, not a
    # microsecond race between two writes on the same wall clock.
    await asyncio.sleep(0.05)
    await db.upsert_batch(pool, [_obs(5, state="CLEAR", detector_version="motion/2")], [])

    rows = await db.timeline(pool, "SE_12TH_CLINTON", T0, T0 + timedelta(hours=1))
    assert len(rows) == 1, "one instant, one resolved row"
    assert rows[0]["state"] == "CLEAR"
    assert rows[0]["detector_version"] == "motion/2"


async def test_observations_are_idempotent_under_replay(pool) -> None:
    """The materializer's crash-safety story: a replayed batch must not
    duplicate. Same (camera, captured_at, detector_version) collapses under
    ON CONFLICT DO NOTHING."""
    batch = [_obs(0, state="BLOCKED", detector_version="motion/1")]
    await db.upsert_batch(pool, batch, [])
    await db.upsert_batch(pool, batch, [])

    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM observations")
    assert count == 1


async def test_timeline_filters_by_crossing_and_time_range(pool) -> None:
    await db.upsert_batch(
        pool,
        [
            _obs(0, state="CLEAR", detector_version="motion/1"),
            _obs(30, state="BLOCKED", detector_version="motion/1"),
            _obs(120, state="CLEAR", detector_version="motion/1"),  # outside range
            {
                **_obs(15, state="CLEAR", detector_version="motion/1"),
                "crossing_id": "SE_8TH_DIVISION",
            },  # other crossing
        ],
        [],
    )

    rows = await db.timeline(pool, "SE_12TH_CLINTON", T0, T0 + timedelta(minutes=60))
    minutes = [(r["captured_at"], r["state"]) for r in rows]
    assert len(rows) == 2
    assert rows[0]["captured_at"] < rows[1]["captured_at"], "ascending by captured_at"
    assert minutes[0][1] == "CLEAR" and minutes[1][1] == "BLOCKED"


async def test_session_latest_ingest_wins_and_can_reopen_history(pool) -> None:
    """The session projection: v1 declared the 06:00 session still open, v2
    later closed it (correcting a missed CLEAR). session_list must return
    v2's word - closed, with the duration and version recorded."""
    sid = f"session-{uuid.uuid4()}"
    await db.upsert_batch(pool, [], [_sess(sid, detector_version="motion/1", is_open=True)])
    await asyncio.sleep(0.05)
    await db.upsert_batch(pool, [], [_sess(sid, detector_version="motion/2", is_open=False)])

    rows = await db.session_list(pool, "SE_12TH_CLINTON", limit=10)
    ours = [r for r in rows if r["session_id"] == sid]
    assert len(ours) == 1, "latest ingest wins - one row per session_id"
    assert ours[0]["is_open"] is False
    assert ours[0]["detector_version"] == "motion/2"
    assert ours[0]["duration_seconds"] == 1200


async def test_session_list_filters_by_crossing_and_orders_newest_first(pool) -> None:
    older = f"session-old-{uuid.uuid4()}"
    newer = f"session-new-{uuid.uuid4()}"
    other = f"session-other-{uuid.uuid4()}"
    await db.upsert_batch(
        pool,
        [],
        [
            _sess(older, detector_version="motion/1", is_open=False, started_min=0),
            _sess(newer, detector_version="motion/1", is_open=True, started_min=90),
            _sess(
                other,
                detector_version="motion/1",
                is_open=False,
                started_min=45,
                crossing_id="SE_8TH_DIVISION",
            ),
        ],
    )

    rows = await db.session_list(pool, "SE_12TH_CLINTON", limit=10)
    ids = [r["session_id"] for r in rows]
    assert other not in ids, "the other-crossing session is filtered out"
    assert ids.index(newer) < ids.index(older), "newer session first"

    unfiltered = await db.session_list(pool, None, limit=10)
    assert {r["session_id"] for r in unfiltered} >= {older, newer, other}


def _window(start_min: float, end_min: float, crossing_id: str = "SE_12TH_CLINTON") -> dict:
    return {
        "crossing_id": crossing_id,
        "window_start": T0 + timedelta(minutes=start_min),
        "window_end": T0 + timedelta(minutes=end_min),
    }


async def test_backfill_replaces_a_changed_boundary_session(pool) -> None:
    """The reason the load deletes: a better detector moved the session's
    start, which changes its deterministic id, so an upsert alone would leave
    the old wrong row standing next to the new one."""
    await db.upsert_batch(
        pool, [], [_sess("old-boundary", detector_version="motion/1", is_open=False)]
    )
    await asyncio.sleep(0.05)
    await db.load_backfill(
        pool,
        [],
        [_sess("new-boundary", detector_version="motion/2", is_open=False, started_min=2)],
        [_window(-60, 120)],
    )

    rows = await db.session_list(pool, "SE_12TH_CLINTON", limit=10)
    ids = {r["session_id"] for r in rows}
    assert ids == {"new-boundary"}, "the superseded boundary is gone, not co-listed"


async def test_backfill_removes_a_phantom_outright(pool) -> None:
    """The 678 dawn-inversion case: the old detector invented a session, the
    new scan of the same window finds nothing, and the phantom must vanish
    rather than survive as the window's only record."""
    await db.upsert_batch(pool, [], [_sess("phantom", detector_version="motion/1", is_open=False)])
    await db.load_backfill(pool, [], [], [_window(-60, 120)])

    rows = await db.session_list(pool, "SE_12TH_CLINTON", limit=10)
    assert rows == []


async def test_backfill_touches_only_its_window_and_crossing(pool) -> None:
    await db.upsert_batch(
        pool,
        [],
        [
            _sess("before-window", detector_version="motion/1", is_open=False, started_min=-120),
            _sess(
                "other-crossing",
                detector_version="motion/1",
                is_open=False,
                crossing_id="SE_8TH_DIVISION",
            ),
        ],
    )
    await db.load_backfill(pool, [], [], [_window(-60, 120)])

    survivors = {r["session_id"] for r in await db.session_list(pool, None, limit=10)}
    assert survivors == {"before-window", "other-crossing"}


async def test_backfill_observations_join_as_a_new_layer(pool) -> None:
    """Observations are never deleted - the load adds the new version's word
    and the timeline resolves latest-ingest-wins, same as the streaming path."""
    await db.upsert_batch(pool, [_obs(5, state="BLOCKED", detector_version="motion/1")], [])
    await asyncio.sleep(0.05)
    await db.load_backfill(
        pool, [_obs(5, state="CLEAR", detector_version="motion/2")], [], [_window(0, 10)]
    )

    rows = await db.timeline(pool, "SE_12TH_CLINTON", T0, T0 + timedelta(hours=1))
    assert [(r["state"], r["detector_version"]) for r in rows] == [("CLEAR", "motion/2")]
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM observations")
    assert count == 2, "both layers kept; resolution happens at read time"


async def test_backfill_is_idempotent(pool) -> None:
    """Re-running the same load must land in the same place: the delete
    claims the window again and the same rows go back in."""
    sessions = [_sess("stable", detector_version="motion/2", is_open=False)]
    await db.load_backfill(pool, [], sessions, [_window(-60, 120)])
    await db.load_backfill(pool, [], sessions, [_window(-60, 120)])

    rows = await db.session_list(pool, "SE_12TH_CLINTON", limit=10)
    assert [r["session_id"] for r in rows] == ["stable"]


async def test_analytics_buckets_resolved_observations_in_local_time(pool) -> None:
    """T0 is 06:00 UTC = 23:00 the previous evening in Portland; the bucket
    must land there, and a newer detector layer flipping an instant to CLEAR
    must move the count the moment it is ingested."""
    await db.upsert_batch(
        pool,
        [
            _obs(0, state="BLOCKED", detector_version="motion/1"),
            _obs(5, state="BLOCKED", detector_version="motion/1"),
            _obs(10, state="UNKNOWN", detector_version="motion/1"),
        ],
        [],
    )
    out = await db.analytics(pool)
    entry = out["SE_12TH_CLINTON"]
    # 2026-08-11 06:00 UTC is Monday 23:00 in America/Los_Angeles; dow=1.
    slot = entry["hour_of_week"][1 * 24 + 23]
    assert slot == {"blocked": 2, "scoreable": 2}, "UNKNOWN is never scoreable"
    assert entry["blocked_share"] == 1.0

    await asyncio.sleep(0.05)
    await db.upsert_batch(pool, [_obs(5, state="CLEAR", detector_version="motion/2")], [])
    out = await db.analytics(pool)
    slot = out["SE_12TH_CLINTON"]["hour_of_week"][1 * 24 + 23]
    assert slot == {"blocked": 1, "scoreable": 2}, "the newer layer's word counts"


async def test_analytics_summarizes_closed_sessions_per_local_day(pool) -> None:
    await db.upsert_batch(
        pool,
        [_obs(0, state="BLOCKED", detector_version="motion/1")],
        [
            _sess("a", detector_version="motion/1", is_open=False),  # 20 min
            _sess("b", detector_version="motion/1", is_open=False, started_min=60),
            _sess("open", detector_version="motion/1", is_open=True, started_min=120),
        ],
    )
    out = await db.analytics(pool)
    entry = out["SE_12TH_CLINTON"]
    assert entry["durations_seconds"] == [1200, 1200], "open sessions have no duration yet"
    assert entry["sessions_closed"] == 2
    # 06:00 and 07:00 UTC straddle local midnight: Monday 23:00 and Tuesday
    # 00:00 in Portland - exactly the split UTC bucketing would get wrong.
    assert list(entry["daily_blocked_minutes"].values()) == [20, 20]
    assert entry["minutes_per_day"] == 40.0, "coverage clamps to one day minimum"


async def test_session_list_limit_caps_the_result(pool) -> None:
    for i in range(5):
        await db.upsert_batch(
            pool,
            [],
            [
                _sess(
                    f"s-{uuid.uuid4()}",
                    detector_version="motion/1",
                    is_open=False,
                    started_min=float(i),
                )
            ],
        )
    rows = await db.session_list(pool, "SE_12TH_CLINTON", limit=3)
    assert len(rows) == 3
