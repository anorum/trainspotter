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


def _obs(
    minute: float,
    *,
    state: str,
    detector_version: str,
    camera: str = "odot-678",
    base: datetime = T0,
    confidence: float = 0.9,
) -> dict:
    return {
        "crossing_id": "SE_12TH_CLINTON",
        "camera_id": camera,
        "captured_at": (base + timedelta(minutes=minute)).isoformat(),
        "detector_version": detector_version,
        "state": state,
        "confidence": confidence,
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
    base: datetime = T0,
    ended_min: float | None = None,
) -> dict:
    started = base + timedelta(minutes=started_min)
    if is_open:
        ended = None
    elif ended_min is not None:
        ended = base + timedelta(minutes=ended_min)
    else:
        ended = started + timedelta(minutes=20)
    return {
        "session_id": session_id,
        "detector_version": detector_version,
        "crossing_id": crossing_id,
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat() if ended else None,
        "duration_seconds": None if ended is None else int((ended - started).total_seconds()),
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
    rather than survive as the window's only record. Emptying a window is
    destructive enough to be opt-in, which is what the operator declares here."""
    await db.upsert_batch(pool, [], [_sess("phantom", detector_version="motion/1", is_open=False)])
    await db.load_backfill(pool, [], [], [_window(-60, 120)], allow_empty_window=True)

    rows = await db.session_list(pool, "SE_12TH_CLINTON", limit=10)
    assert rows == []


async def test_backfill_refuses_to_empty_a_window_it_cannot_replace(pool) -> None:
    """The partial-scan footgun: re-scoring one camera of a two-camera crossing
    derives no sessions from the witness that saw nothing - or, for a
    `scores: false` camera, from a window of pure unscored UNKNOWNs - while the
    window still spans the crossing. Deleting the other camera's real trains and
    inserting nothing would erase them from the lanes, the sessions API, and the
    board with nothing to replace them, so the load refuses and the transaction
    rolls back untouched."""
    real = _sess("real-train", detector_version="motion/1", is_open=False)
    await db.upsert_batch(pool, [], [real])

    with pytest.raises(db.EmptyWindowError, match="SE_12TH_CLINTON"):
        await db.load_backfill(
            pool,
            [_obs(5, state="UNKNOWN", detector_version="unscored/1", camera="odot-679")],
            [],
            [_window(-60, 120)],
        )

    rows = await db.session_list(pool, "SE_12TH_CLINTON", limit=10)
    assert [r["session_id"] for r in rows] == ["real-train"], "the refusal is a full rollback"
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM observations")
    assert count == 0, "the observations in the same transaction rolled back too"


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


async def test_coverage_ignores_unscored_heartbeats(pool) -> None:
    """Coverage is what the crossing was actually judged over, not what any
    camera happened to emit. A non-scoring camera's unscored/1 UNKNOWN three
    days after the last real witness must not stretch the coverage window and
    deflate minutes/day by a factor of three."""
    await db.upsert_batch(
        pool,
        [
            _obs(0, state="BLOCKED", detector_version="motion/1"),
            _obs(
                3 * 24 * 60,
                state="UNKNOWN",
                detector_version="unscored/1",
                camera="odot-679",
            ),
        ],
        [_sess("a", detector_version="motion/1", is_open=False)],  # 20 min
    )

    entry = (await db.analytics(pool))["SE_12TH_CLINTON"]

    assert entry["last_observed"] == T0.isoformat(), "the heartbeat is not a witness"
    assert entry["coverage_days"] == 1.0, "one scoreable instant, clamped to one day"
    assert entry["minutes_per_day"] == 20.0, "not deflated across the dark days"


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


def _recent() -> datetime:
    """Sightings anchor on now() in SQL, so their fixtures anchor there too.
    Whole seconds, because the derivation round-trips timestamps through
    epoch milliseconds and the assertions compare ISO strings exactly."""
    return datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)


async def test_sightings_hold_a_blocked_frame_until_the_next_judgement(pool) -> None:
    """The board's own rule replayed over history: a BLOCKED judgement holds
    until its camera's next judgement supersedes it, and with no session
    covering the run it surfaces on the sheet as a sighting."""
    b = _recent()
    await db.upsert_batch(
        pool,
        [
            _obs(0, state="BLOCKED", detector_version="motion/1", base=b),
            _obs(4, state="CLEAR", detector_version="motion/1", base=b),
        ],
        [],
    )

    out = await db.sightings(pool, "SE_12TH_CLINTON", days=14)
    assert [(s["started_at"], s["ended_at"], s["frames"]) for s in out] == [
        (b.isoformat(), (b + timedelta(minutes=4)).isoformat(), 1)
    ]


async def test_sightings_cap_a_lone_blocked_frame_at_the_freshness_limit(pool) -> None:
    """A camera that went quiet mid-train: the BLOCKED word holds only as long
    as the board would trust it, so the span ends at the staleness cap rather
    than stretching to a judgement an hour later."""
    from blockade.api.state import DEFAULT_STALE_AFTER

    b = _recent()
    await db.upsert_batch(
        pool,
        [
            _obs(0, state="BLOCKED", detector_version="motion/1", base=b),
            _obs(60, state="CLEAR", detector_version="motion/1", base=b),
        ],
        [],
    )

    out = await db.sightings(pool, "SE_12TH_CLINTON", days=14)
    assert [s["ended_at"] for s in out] == [(b + DEFAULT_STALE_AFTER).isoformat()]


async def test_sightings_ignore_unscored_rows_entirely(pool) -> None:
    """unscored/% heartbeats are not judgements: an unscored BLOCKED mints no
    sighting, and an unscored row between two real judgements does not
    supersede the BLOCKED word before it."""
    b = _recent()
    await db.upsert_batch(
        pool,
        [
            _obs(0, state="BLOCKED", detector_version="unscored/1", base=b, camera="odot-679"),
            _obs(0, state="BLOCKED", detector_version="motion/1", base=b),
            _obs(2, state="UNKNOWN", detector_version="unscored/1", base=b),
            _obs(5, state="CLEAR", detector_version="motion/1", base=b),
        ],
        [],
    )

    out = await db.sightings(pool, "SE_12TH_CLINTON", days=14)
    assert [(s["started_at"], s["ended_at"], s["frames"]) for s in out] == [
        (b.isoformat(), (b + timedelta(minutes=5)).isoformat(), 1)
    ]


async def test_sightings_merge_overlapping_camera_spans(pool) -> None:
    """Two cameras watching the same train are one sighting: overlapping spans
    merge, the frame count sums, and the peak confidence is the max."""
    b = _recent()
    await db.upsert_batch(
        pool,
        [
            _obs(0, state="BLOCKED", detector_version="motion/1", base=b, confidence=0.5),
            _obs(6, state="CLEAR", detector_version="motion/1", base=b),
            _obs(
                3,
                state="BLOCKED",
                detector_version="motion/1",
                base=b,
                camera="odot-679",
                confidence=0.94,
            ),
            _obs(8, state="CLEAR", detector_version="motion/1", base=b, camera="odot-679"),
        ],
        [],
    )

    out = await db.sightings(pool, "SE_12TH_CLINTON", days=14)
    assert [(s["started_at"], s["ended_at"], s["frames"]) for s in out] == [
        (b.isoformat(), (b + timedelta(minutes=8)).isoformat(), 2)
    ]
    assert out[0]["peak_confidence"] == pytest.approx(0.94)


async def test_sightings_subtract_certified_sessions_including_straddlers(pool) -> None:
    """What a session certifies is not a sighting - including when the session
    started before the window's leading edge. Filtering sessions by started_at
    would drop that straddler and resurface its BLOCKED frames as a phantom
    duplicating the record book."""
    now = datetime.now(UTC).replace(microsecond=0)
    covered = now - timedelta(hours=3)
    edge = now - timedelta(days=13, hours=12)
    survivor = now - timedelta(hours=1)
    await db.upsert_batch(
        pool,
        [
            _obs(0, state="BLOCKED", detector_version="motion/1", base=covered),
            _obs(5, state="CLEAR", detector_version="motion/1", base=covered),
            _obs(0, state="BLOCKED", detector_version="motion/1", base=edge),
            _obs(5, state="CLEAR", detector_version="motion/1", base=edge),
            _obs(0, state="BLOCKED", detector_version="motion/1", base=survivor),
            _obs(4, state="CLEAR", detector_version="motion/1", base=survivor),
        ],
        [
            _sess(
                "covering",
                detector_version="motion/1",
                is_open=False,
                base=covered,
                started_min=-5,
                ended_min=10,
            ),
            _sess(
                "straddler",
                detector_version="motion/1",
                is_open=False,
                base=now,
                started_min=-20 * 24 * 60,
                ended_min=-13 * 24 * 60,
            ),
        ],
    )

    out = await db.sightings(pool, "SE_12TH_CLINTON", days=14)
    assert [s["started_at"] for s in out] == [survivor.isoformat()]


async def test_sightings_subtract_an_open_session_from_before_the_window(pool) -> None:
    """An open session is a running certification up to now: even one whose
    started_at predates the whole window keeps its BLOCKED frames off the
    sightings tier."""
    b = _recent()
    await db.upsert_batch(
        pool,
        [
            _obs(0, state="BLOCKED", detector_version="motion/1", base=b),
            _obs(5, state="CLEAR", detector_version="motion/1", base=b),
        ],
        [
            _sess(
                "open-straddler",
                detector_version="motion/1",
                is_open=True,
                base=datetime.now(UTC),
                started_min=-20 * 24 * 60,
            )
        ],
    )

    out = await db.sightings(pool, "SE_12TH_CLINTON", days=14)
    assert out == []
