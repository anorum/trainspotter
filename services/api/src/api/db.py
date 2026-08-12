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

import json
from datetime import datetime

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


async def load_backfill(
    pool: asyncpg.Pool,
    observations: list[dict],
    sessions: list[dict],
    windows: list[dict],
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
    """
    async with pool.acquire() as conn, conn.transaction():
        for w in windows:
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
        """SELECT DISTINCT ON (camera_id, captured_at)
                  camera_id, captured_at, state, object_key, detector_version
           FROM observations
           WHERE crossing_id = $1 AND captured_at BETWEEN $2 AND $3
           ORDER BY camera_id, captured_at, ingested_at DESC""",
        crossing_id,
        start,
        end,
    )
    return sorted(
        (
            {
                "camera_id": r["camera_id"],
                "captured_at": r["captured_at"].isoformat(),
                "state": r["state"],
                "object_key": r["object_key"],
                "detector_version": r["detector_version"],
            }
            for r in rows
        ),
        key=lambda r: r["captured_at"],
    )


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


def parse_record(value: bytes) -> dict:
    return json.loads(value)
