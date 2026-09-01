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
import time
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
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from api import db
from api.images import FrameImages
from api.materializer import Materializer
from api.tailer import StateFeed

HTTP_REQUESTS = Counter(
    "blockade_api_requests_total",
    "HTTP requests served, by route template",
    ["route", "method", "status"],
)
HTTP_SECONDS = Histogram(
    "blockade_api_request_seconds", "Request latency, by route template", ["route"]
)
SSE_CLIENTS = Gauge("blockade_api_sse_clients", "Open event-stream connections")

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
        # Metrics on their own port: the public HTTPRoute forwards every path
        # on 8000, so /metrics there would be internet-facing.
        metrics_server, _ = start_http_server(settings.metrics_port)
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
        metrics_server.shutdown()
        metrics_server.server_close()

    def history_pool() -> asyncpg.Pool:
        if pool is None:
            raise HTTPException(status_code=503, detail="history store not configured")
        return pool

    app = FastAPI(title="blockade-api", lifespan=lifespan)
    app.state.live = state

    @app.middleware("http")
    async def measure(request, call_next):  # type: ignore[no-untyped-def]
        began = time.perf_counter()
        # An unhandled handler exception only becomes a 500 above this
        # middleware, in ServerErrorMiddleware - count it on the way out or
        # the dashboard's 5xx panel misses exactly the crashes it exists for.
        status = "500"
        try:
            response = await call_next(request)
            status = str(response.status_code)
            return response
        finally:
            # The matched route's template, never the raw path: object keys
            # and query strings would explode the label cardinality.
            route = getattr(request.scope.get("route"), "path", "unmatched")
            HTTP_REQUESTS.labels(route, request.method, status).inc()
            HTTP_SECONDS.labels(route).observe(time.perf_counter() - began)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/readyz")
    async def readyz() -> Response:
        if not feed.ready:
            return JSONResponse({"ok": False, "reason": "replaying the log"}, status_code=503)
        return JSONResponse({"ok": True})

    def cache_for(browser: int, edge: int) -> str:
        """How long each cache may hold a read answer.

        The record moves slowly - a camera speaks every three to ten minutes -
        so a few seconds at the edge costs no freshness a viewer could
        perceive while taking the read path off the origin almost entirely.
        The two numbers protect different things: `max-age` stops one phone
        re-asking during a single glance, `s-maxage` stops ten thousand
        phones becoming ten thousand requests.

        Cloudflare will not cache these extensionless paths on the header
        alone - the cache rule deploy/cloudflare/apply.sh applies over
        /api/v1/* makes them eligible - and every URLSession and browser
        honours the header directly.
        """
        return f"public, max-age={browser}, s-maxage={edge}"

    @app.get("/api/v1/status")
    async def status() -> Response:
        return Response(
            state.snapshot().model_dump_json(),
            media_type="application/json",
            headers={"Cache-Control": cache_for(browser=15, edge=20)},
        )

    @app.get("/api/v1/crossings")
    async def crossings(response: Response) -> dict:
        # The roster only changes when a camera is added, which is a deploy.
        response.headers["Cache-Control"] = cache_for(browser=300, edge=3600)
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
        response: Response,
        crossing_id: str,
        hours: int = 24,
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = Query(default=None, alias="to"),
    ) -> dict:
        """Latest detector version wins per instant, over all history."""
        response.headers["Cache-Control"] = cache_for(browser=30, edge=60)
        start, end = resolve_range(from_, to, hours)
        observations = await db.timeline(history_pool(), crossing_id, start, end)
        return {
            "crossing_id": crossing_id,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "observations": observations,
        }

    @app.get("/api/v1/sessions")
    async def sessions(response: Response, crossing_id: str | None = None, limit: int = 50) -> dict:
        """Session history, latest ingest wins per session_id."""
        response.headers["Cache-Control"] = cache_for(browser=60, edge=120)
        return {"sessions": await db.session_list(history_pool(), crossing_id, limit)}

    @app.get("/api/v1/analytics")
    async def analytics(response: Response) -> dict:
        """Temporal patterns per crossing. The `available` flag lets the UI
        hide the stats surface entirely when there is no history store."""
        if pool is None:
            # Deliberately uncached: the store being down is a condition that
            # can end at any moment, and a cached outage outlives the outage.
            return {"available": False, "crossings": {}}
        # Weeks of aggregate - one new hour cannot move it perceptibly.
        response.headers["Cache-Control"] = cache_for(browser=300, edge=600)
        return {
            "available": True,
            "local_tz": db.LOCAL_TZ,
            "crossings": await db.analytics(pool),
        }

    @app.get("/api/v1/events")
    async def events() -> StreamingResponse:
        async def stream():
            queue = feed.subscribe()
            SSE_CLIENTS.inc()
            try:
                snapshot = state.snapshot()
                sent_feed_status = snapshot.feed.status
                yield _sse("status", snapshot.model_dump_json())
                while True:
                    try:
                        await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                        snapshot = state.snapshot()
                        sent_feed_status = snapshot.feed.status
                        yield _sse("status", snapshot.model_dump_json())
                    except TimeoutError:
                        # The feed verdict can move on wall clock alone - a
                        # silent poller produces no record to announce its own
                        # death - so the quiet tick re-judges before settling
                        # for a bare keep-alive comment.
                        snapshot = state.snapshot()
                        if snapshot.feed.status != sent_feed_status:
                            sent_feed_status = snapshot.feed.status
                            yield _sse("status", snapshot.model_dump_json())
                        else:
                            yield ": heartbeat\n\n"
            finally:
                SSE_CLIENTS.dec()
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
