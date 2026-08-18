"""The FastAPI app: thin plumbing around blockade.api.state.

Routes are deliberately small enough to live in one file; they split into a
package when analytics endpoints outgrow it.

Two storage worlds, one rule: the live board (/status, /events, frames) reads
only the in-memory LiveState and never depends on the database; every history
surface (/timeline, /sessions, /analytics) reads only Postgres and refuses
rather than guessing when it is not configured - the deployment always
configures it, and a half-true history from a memory buffer is worse than an
honest error. Refusing is a 503, except on /analytics, where the UI needs to
know to hide the stats surface entirely rather than render an error.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
from blockade.api.state import LiveState
from blockade.config import Settings, get_settings, load_roster
from blockade.schemas import parse_utc
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from api import db
from api.images import FrameImages
from api.materializer import Materializer
from api.tailer import StateFeed

HEARTBEAT_SECONDS = 20


def parse_stamp(stamp: str) -> datetime:
    """ISO timestamp to aware UTC datetime; a malformed one is caller error."""
    try:
        return parse_utc(stamp)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"bad timestamp: {stamp}") from exc


def resolve_range(from_: str | None, to: str | None, hours: int) -> tuple[datetime, datetime]:
    """`from`/`to` bound the range exactly - the sessions page uses them to pull
    the frames inside one session - and take precedence over `hours`, which
    remains the trailing-window shorthand the scrubber uses."""
    end = parse_stamp(to) if to else datetime.now(UTC)
    start = parse_stamp(from_) if from_ else end - timedelta(hours=hours)
    return start, end


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    roster = load_roster(settings.camera_config_path)
    cameras_by_crossing: dict[str, list[tuple[str, str]]] = {}
    coords: dict[str, tuple[float, float]] = {}
    for camera in roster.enabled():
        cameras_by_crossing.setdefault(camera.crossing_id, []).append(
            (camera.camera_id, camera.name)
        )
        if camera.lat is not None and camera.lon is not None:
            coords.setdefault(camera.crossing_id, (camera.lat, camera.lon))

    state = LiveState(
        cameras_by_crossing,
        scoring={camera.camera_id for camera in roster.scoring()},
    )
    feed = StateFeed(settings, state)
    images = FrameImages(settings)

    pool: asyncpg.Pool | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal pool
        materializer = None
        if settings.database_url:
            # The feed's Kafka replay and the pool's connect+DDL are
            # independent; only the materializer needs the pool.
            _, pool = await asyncio.gather(feed.start(), db.connect(settings.database_url))
            materializer = Materializer(settings, pool)
            await materializer.start()
        else:
            await feed.start()
        yield
        if materializer is not None:
            await materializer.stop()
        if pool is not None:
            await pool.close()
        await feed.stop()

    def history_pool() -> asyncpg.Pool:
        if pool is None:
            raise HTTPException(status_code=503, detail="history store not configured")
        return pool

    app = FastAPI(title="blockade-api", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/readyz")
    async def readyz() -> Response:
        if not feed.ready:
            return JSONResponse({"ok": False, "reason": "replaying the log"}, status_code=503)
        return JSONResponse({"ok": True})

    @app.get("/api/v1/status")
    async def status() -> Response:
        return Response(state.snapshot().model_dump_json(), media_type="application/json")

    @app.get("/api/v1/crossings")
    async def crossings() -> dict:
        return {
            "crossings": [
                {
                    "crossing_id": crossing_id,
                    "cameras": [{"camera_id": cid, "name": name} for cid, name in cams],
                    "lat": coords.get(crossing_id, (None, None))[0],
                    "lon": coords.get(crossing_id, (None, None))[1],
                }
                for crossing_id, cams in sorted(cameras_by_crossing.items())
            ]
        }

    @app.get("/api/v1/frames/{object_key:path}")
    async def frame(object_key: str) -> Response:
        data = await images.get(object_key)
        if data is None:
            raise HTTPException(status_code=404)
        return Response(
            data,
            media_type="image/jpeg",
            # Content-addressed key: these bytes can never change.
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/api/v1/timeline")
    async def timeline(
        crossing_id: str,
        hours: int = 24,
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = Query(default=None, alias="to"),
    ) -> dict:
        """Latest detector version wins per instant, over all history."""
        start, end = resolve_range(from_, to, hours)
        observations = await db.timeline(history_pool(), crossing_id, start, end)
        return {
            "crossing_id": crossing_id,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "observations": observations,
        }

    @app.get("/api/v1/sessions")
    async def sessions(crossing_id: str | None = None, limit: int = 50) -> dict:
        """Session history, latest ingest wins per session_id."""
        return {"sessions": await db.session_list(history_pool(), crossing_id, limit)}

    @app.get("/api/v1/sightings")
    async def sightings(crossing_id: str, days: int = 14) -> dict:
        """Blocked runs too brief for the record book - shown, not certified."""
        return {"sightings": await db.sightings(history_pool(), crossing_id, days)}

    @app.get("/api/v1/analytics")
    async def analytics() -> dict:
        """Temporal patterns per crossing. The `available` flag lets the UI
        hide the stats surface entirely when there is no history store."""
        if pool is None:
            return {"available": False, "crossings": {}}
        return {
            "available": True,
            "local_tz": db.LOCAL_TZ,
            "crossings": await db.analytics(pool),
        }

    @app.get("/api/v1/events")
    async def events() -> StreamingResponse:
        async def stream():
            queue = feed.subscribe()
            try:
                yield _sse("status", state.snapshot().model_dump_json())
                while True:
                    try:
                        await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                        yield _sse("status", state.snapshot().model_dump_json())
                    except TimeoutError:
                        yield ": heartbeat\n\n"
            finally:
                feed.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    static_dir = Path("/app/static")
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="site")

    return app


def _sse(event: str, data: str) -> str:
    # Pydantic's JSON contains no raw newlines, so the payload is one data line.
    return f"event: {event}\ndata: {data}\n\n"
