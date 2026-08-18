"""The history store: versioned observations and sessions in Postgres.

The design settled with Alex after the backfill discussion: streaming owns
now, batch owns history, and every row carries the detector_version that
produced it. Observations are append-only - a better detector's backfill adds
a newer layer and reads resolve "latest ingest wins" per (camera, instant) -
which is what makes the time slider retroactively honest when the detector
improves. Sessions are a projection of observations: streaming upserts keep
the recent ones current, and a backfill simply rebuilds the projection over
the window it re-scored.

Schema bootstrap is CREATE TABLE IF NOT EXISTS on startup; at this scale a
migration framework is ceremony. Everything here is derived and replayable
from Kafka + S3, so losing the database is an inconvenience, not data loss.

Plain functions over an asyncpg pool.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import asyncpg

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    crossing_id      text NOT NULL,
    camera_id        text NOT NULL,
    captured_at      timestamptz NOT NULL,
    detector_version text NOT NULL,
    state            text NOT NULL,
    confidence       real NOT NULL,
    reason           text NOT NULL,
    object_key       text NOT NULL,
    ingested_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (camera_id, captured_at, detector_version)
);
CREATE INDEX IF NOT EXISTS obs_crossing_time ON observations (crossing_id, captured_at);
-- Serves the DISTINCT ON (camera_id, captured_at) latest-ingest-wins
-- resolution in timeline and analytics as an index scan instead of a
-- full sort of the table.
CREATE INDEX IF NOT EXISTS obs_resolution
    ON observations (camera_id, captured_at, ingested_at DESC);

CREATE TABLE IF NOT EXISTS sessions (
    session_id       text NOT NULL,
    detector_version text NOT NULL,
    crossing_id      text NOT NULL,
    started_at       timestamptz NOT NULL,
    ended_at         timestamptz,
    duration_seconds int,
    peak_queue_occupancy real,
    is_open          bool NOT NULL,
    ingested_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, detector_version)
);
CREATE INDEX IF NOT EXISTS sessions_crossing_start ON sessions (crossing_id, started_at DESC);
"""


async def connect(dsn: str) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)
    return pool


async def upsert_batch(pool: asyncpg.Pool, observations: list[dict], sessions: list[dict]) -> None:
    """One transaction per batch; the caller commits Kafka offsets only after
    this returns - the same commit-after-durable pattern as the whole bus."""
    async with pool.acquire() as conn, conn.transaction():
        await _upsert(conn, observations, sessions)


class EmptyWindowError(Exception):
    """A window would delete a crossing's sessions and put nothing back."""


async def load_backfill(
    pool: asyncpg.Pool,
    observations: list[dict],
    sessions: list[dict],
    windows: list[dict],
    *,
    allow_empty_window: bool = False,
) -> None:
    """Load a re-derived window: observations join the versioned layers as
    usual, but sessions inside the window are replaced outright.

    Observations are the dataset and stay append-only - that is where
    retroactive honesty lives. Sessions are a projection of them, and a
    session whose boundaries changed under a better detector gets a new
    session_id, so upserts alone would leave the old wrong row standing next
    to the new one. Deleting what the window covered and inserting what the
    new derivation found is the projection being rebuilt, not history being
    edited. One transaction, so a crash mid-load leaves the old projection
    intact rather than a window with no sessions at all.

    A crossing's sessions are derived from all of its cameras at once, so a
    scan that covered only some of them derives too few - and a scan of a
    camera that can no longer score derives none at all. Wiping a window and
    replacing it with nothing is therefore refused unless the caller says
    that is what it meant: an empty derivation is the expected result of a
    phantom being retracted, and the disastrous result of re-scoring one
    witness out of two. ``allow_empty_window`` is the former.
    """
    async with pool.acquire() as conn, conn.transaction():
        for w in windows:
            if not allow_empty_window and not any(
                s["crossing_id"] == w["crossing_id"] for s in sessions
            ):
                doomed = await conn.fetchval(
                    """SELECT count(*) FROM sessions
                       WHERE crossing_id = $1 AND started_at BETWEEN $2 AND $3""",
                    w["crossing_id"],
                    w["window_start"],
                    w["window_end"],
                )
                if doomed:
                    raise EmptyWindowError(
                        f"{w['crossing_id']}: the new derivation has no sessions, but "
                        f"{doomed} existing session(s) start inside "
                        f"{w['window_start']:%Y-%m-%d %H:%M} .. "
                        f"{w['window_end']:%Y-%m-%d %H:%M} UTC and would be deleted with "
                        "nothing to replace them. Sessions are derived from every camera "
                        "on the crossing at once, so re-score all of them together - drop "
                        "--camera, or scan each and concatenate the JSONL - and load that. "
                        "If this window genuinely holds no blockage, re-run with "
                        "--allow-empty-window."
                    )
            await conn.execute(
                """DELETE FROM sessions
                   WHERE crossing_id = $1 AND started_at BETWEEN $2 AND $3""",
                w["crossing_id"],
                w["window_start"],
                w["window_end"],
            )
        await _upsert(conn, observations, sessions)


async def _upsert(conn: asyncpg.Connection, observations: list[dict], sessions: list[dict]) -> None:
    if observations:
        await conn.executemany(
            """INSERT INTO observations
               (crossing_id, camera_id, captured_at, detector_version,
                state, confidence, reason, object_key)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
               ON CONFLICT (camera_id, captured_at, detector_version) DO NOTHING""",
            [
                (
                    o["crossing_id"],
                    o["camera_id"],
                    datetime.fromisoformat(o["captured_at"]),
                    o["detector_version"],
                    o["state"],
                    o["confidence"],
                    o["reason"],
                    o["object_key"],
                )
                for o in observations
            ],
        )
    if sessions:
        await conn.executemany(
            """INSERT INTO sessions
               (session_id, detector_version, crossing_id, started_at,
                ended_at, duration_seconds, peak_queue_occupancy, is_open)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
               ON CONFLICT (session_id, detector_version) DO UPDATE SET
                 ended_at = EXCLUDED.ended_at,
                 duration_seconds = EXCLUDED.duration_seconds,
                 peak_queue_occupancy = EXCLUDED.peak_queue_occupancy,
                 is_open = EXCLUDED.is_open,
                 ingested_at = now()""",
            [
                (
                    s["session_id"],
                    s["detector_version"],
                    s["crossing_id"],
                    datetime.fromisoformat(s["started_at"]),
                    datetime.fromisoformat(s["ended_at"]) if s.get("ended_at") else None,
                    s.get("duration_seconds"),
                    s.get("peak_queue_occupancy"),
                    s["is_open"],
                )
                for s in sessions
            ],
        )


async def timeline(
    pool: asyncpg.Pool, crossing_id: str, start: datetime, end: datetime
) -> list[dict]:
    """Latest detector's word for each (camera, instant) in the range."""
    rows = await pool.fetch(
        """SELECT * FROM (
             SELECT DISTINCT ON (camera_id, captured_at)
                    camera_id, captured_at, state, object_key, detector_version
             FROM observations
             WHERE crossing_id = $1 AND captured_at BETWEEN $2 AND $3
             ORDER BY camera_id, captured_at, ingested_at DESC
           ) resolved
           ORDER BY captured_at""",
        crossing_id,
        start,
        end,
    )
    return [
        {
            "camera_id": r["camera_id"],
            "captured_at": r["captured_at"].isoformat(),
            "state": r["state"],
            "object_key": r["object_key"],
            "detector_version": r["detector_version"],
        }
        for r in rows
    ]


async def session_list(pool: asyncpg.Pool, crossing_id: str | None, limit: int) -> list[dict]:
    """Latest ingest wins per session_id, newest sessions first."""
    rows = await pool.fetch(
        """SELECT * FROM (
             SELECT DISTINCT ON (session_id) *
             FROM sessions
             WHERE ($1::text IS NULL OR crossing_id = $1)
             ORDER BY session_id, ingested_at DESC
           ) latest
           ORDER BY started_at DESC
           LIMIT $2""",
        crossing_id,
        limit,
    )
    return [
        {
            "session_id": r["session_id"],
            "crossing_id": r["crossing_id"],
            "started_at": r["started_at"].isoformat(),
            "ended_at": r["ended_at"].isoformat() if r["ended_at"] else None,
            "duration_seconds": r["duration_seconds"],
            "peak_queue_occupancy": r["peak_queue_occupancy"],
            "is_open": r["is_open"],
            "detector_version": r["detector_version"],
        }
        for r in rows
    ]


async def sightings(pool: asyncpg.Pool, crossing_id: str, days: int) -> list[dict]:
    """Blocked runs the session minimums filtered out - the one-frame trains.

    Applies the board's own rule (a BLOCKED judgement holds until its camera's
    next judgement supersedes it or freshness expires), then subtracts every
    interval a recorded session already covers. What remains is what a camera
    saw but the record book declined to certify: real at today's frame
    cadence, so the sheet shows them as sightings rather than hiding them.
    """
    from blockade.api.state import DEFAULT_STALE_AFTER

    stale_ms = DEFAULT_STALE_AFTER.total_seconds() * 1000
    rows = await pool.fetch(
        """SELECT camera_id, captured_at, state, confidence
           FROM (
             SELECT DISTINCT ON (camera_id, captured_at)
                    camera_id, captured_at, state, confidence, detector_version
             FROM observations
             WHERE crossing_id = $1 AND captured_at > now() - make_interval(days => $2)
             ORDER BY camera_id, captured_at, ingested_at DESC
           ) resolved
           WHERE detector_version NOT LIKE 'unscored/%'
           ORDER BY captured_at""",
        crossing_id,
        days,
    )
    by_camera: dict[str, list] = {}
    for r in rows:
        by_camera.setdefault(r["camera_id"], []).append(r)
    spans: list[tuple[float, float, int, float]] = []  # start_ms, end_ms, frames, peak
    for cam_rows in by_camera.values():
        for i, r in enumerate(cam_rows):
            if r["state"] != "BLOCKED":
                continue
            t = r["captured_at"].timestamp() * 1000
            nxt = (
                cam_rows[i + 1]["captured_at"].timestamp() * 1000
                if i + 1 < len(cam_rows)
                else float("inf")
            )
            spans.append((t, min(nxt, t + stale_ms), 1, r["confidence"]))
    spans.sort()
    merged: list[list] = []
    for a, b, n, c in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
            merged[-1][2] += n
            merged[-1][3] = max(merged[-1][3], c)
        else:
            merged.append([a, b, n, c])

    sessions = await pool.fetch(
        """SELECT started_at, ended_at FROM (
             SELECT DISTINCT ON (session_id) *
             FROM sessions WHERE crossing_id = $1
             ORDER BY session_id, ingested_at DESC
           ) latest
           WHERE started_at > now() - make_interval(days => $2)""",
        crossing_id,
        days,
    )
    taken = [
        (
            r["started_at"].timestamp() * 1000,
            (r["ended_at"].timestamp() * 1000 if r["ended_at"] else float("inf")),
        )
        for r in sessions
    ]
    out = []
    for a, b, n, c in merged:
        if any(a < tb and b > ta for ta, tb in taken):
            continue  # the record book already tells this story
        out.append(
            {
                "started_at": datetime.fromtimestamp(a / 1000, tz=UTC).isoformat(),
                "ended_at": datetime.fromtimestamp(b / 1000, tz=UTC).isoformat(),
                "frames": n,
                "peak_confidence": c,
            }
        )
    out.reverse()  # newest first, like the sheet reads
    return out


LOCAL_TZ = "America/Los_Angeles"
"""Patterns are bucketed in corridor-local time. Trains do not follow UTC,
and an hour-of-day profile shifted by seven or eight hours would put the
morning freight in the middle of the night."""


async def analytics(pool: asyncpg.Pool) -> dict:
    """Temporal structure per crossing, for the patterns page and the board's
    detail panel.

    Everything derives from the same resolved layers the timeline serves:
    latest ingest wins per (camera, instant) before any counting, so a
    backfilled correction changes the statistics the moment it lands. The
    blocked share is the share of scoreable camera checks (UNKNOWN excluded)
    that saw a train - at a 2-5 minute sampling cadence that tracks the share
    of time blocked closely, and it is honest about being sampled.
    """
    # Coverage derives from the same resolved, scoreable rows the grid counts:
    # a non-scoring camera's unscored/1 heartbeat must not extend the coverage
    # window (and so deflate minutes/day) while its crossing's real witness is
    # dark. One scan serves both aggregates, and the independent queries run
    # concurrently - latency is the max, not the sum.
    grid_q = pool.fetch(
        f"""WITH resolved AS (
              SELECT DISTINCT ON (camera_id, captured_at)
                     crossing_id, captured_at, state
              FROM observations
              ORDER BY camera_id, captured_at, ingested_at DESC
            ), scoreable AS (
              SELECT * FROM resolved WHERE state IN ('BLOCKED', 'CLEAR')
            )
            SELECT crossing_id,
                   extract(dow  FROM captured_at AT TIME ZONE '{LOCAL_TZ}')::int AS dow,
                   extract(hour FROM captured_at AT TIME ZONE '{LOCAL_TZ}')::int AS hour,
                   count(*) FILTER (WHERE state = 'BLOCKED') AS blocked,
                   count(*)                                  AS scoreable,
                   min(min(captured_at)) OVER (PARTITION BY crossing_id) AS first,
                   max(max(captured_at)) OVER (PARTITION BY crossing_id) AS last
            FROM scoreable
            GROUP BY 1, 2, 3"""
    )
    closed_q = pool.fetch(
        f"""SELECT crossing_id, duration_seconds,
                   (started_at AT TIME ZONE '{LOCAL_TZ}')::date AS local_day
            FROM (
              SELECT DISTINCT ON (session_id) *
              FROM sessions ORDER BY session_id, ingested_at DESC
            ) latest
            WHERE NOT is_open AND duration_seconds IS NOT NULL
            ORDER BY started_at"""
    )
    grid, closed = await asyncio.gather(grid_q, closed_q)

    out: dict[str, dict] = {}
    for r in grid:
        entry = out.get(r["crossing_id"])
        if entry is None:
            days = max(1.0, (r["last"] - r["first"]).total_seconds() / 86_400)
            entry = out[r["crossing_id"]] = {
                "first_observed": r["first"].isoformat(),
                "last_observed": r["last"].isoformat(),
                "coverage_days": round(days, 2),
                "hour_of_week": [{"blocked": 0, "scoreable": 0} for _ in range(168)],
                "durations_seconds": [],
                "daily_blocked_minutes": {},
            }
        slot = entry["hour_of_week"][r["dow"] * 24 + r["hour"]]
        slot["blocked"] += r["blocked"]
        slot["scoreable"] += r["scoreable"]
    for r in closed:
        entry = out.get(r["crossing_id"])
        if entry is not None:
            entry["durations_seconds"].append(r["duration_seconds"])
            day = r["local_day"].isoformat()
            entry["daily_blocked_minutes"][day] = entry["daily_blocked_minutes"].get(
                day, 0
            ) + round(r["duration_seconds"] / 60)
    for entry in out.values():
        blocked = sum(s["blocked"] for s in entry["hour_of_week"])
        scoreable = sum(s["scoreable"] for s in entry["hour_of_week"])
        total_minutes = sum(entry["daily_blocked_minutes"].values())
        entry["blocked_share"] = round(blocked / scoreable, 4) if scoreable else None
        entry["sessions_closed"] = len(entry["durations_seconds"])
        entry["minutes_per_day"] = round(total_minutes / entry["coverage_days"], 1)
    return out
