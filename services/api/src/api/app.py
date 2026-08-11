"""The FastAPI app: thin plumbing around blockade.api.state.

Routes are deliberately small enough to live in one file for Phase A; they
split into a package when sessions and analytics arrive.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path

from blockade.api.state import LiveState
from blockade.config import Settings, get_settings, load_roster
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from api.images import FrameImages
from api.tailer import StateFeed

LATLON = re.compile(r"lat=(-?[\d.]+)\s+lon=(-?[\d.]+)")
HEARTBEAT_SECONDS = 20


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    roster = load_roster(settings.camera_config_path).enabled()
    cameras_by_crossing: dict[str, list[tuple[str, str]]] = {}
    coords: dict[str, tuple[float, float]] = {}
    for camera in roster:
        cameras_by_crossing.setdefault(camera.crossing_id, []).append(
            (camera.camera_id, camera.name)
        )
        if camera.notes and (m := LATLON.search(camera.notes)):
            coords.setdefault(camera.crossing_id, (float(m.group(1)), float(m.group(2))))

    state = LiveState(cameras_by_crossing)
    feed = StateFeed(settings, state)
    images = FrameImages(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await feed.start()
        yield
        await feed.stop()

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
        return Response(
            state.snapshot().model_dump_json(), media_type="application/json"
        )

    @app.get("/api/v1/crossings")
    async def crossings() -> dict:
        return {
            "crossings": [
                {
                    "crossing_id": crossing_id,
                    "cameras": [
                        {"camera_id": cid, "name": name} for cid, name in cams
                    ],
                    "lat": coords.get(crossing_id, (None, None))[0],
                    "lon": coords.get(crossing_id, (None, None))[1],
                }
                for crossing_id, cams in sorted(cameras_by_crossing.items())
            ]
        }

    @app.get("/api/v1/frames/latest/{camera_id}")
    async def latest_frame(camera_id: str) -> Response:
        snapshot = state.snapshot()
        for crossing in snapshot.crossings:
            for cam in crossing.cameras:
                if cam.camera_id == camera_id and cam.object_key:
                    data = await images.get(cam.object_key)
                    if data is None:
                        raise HTTPException(status_code=404)
                    return Response(
                        data,
                        media_type="image/jpeg",
                        headers={"ETag": f'"{cam.object_key}"', "Cache-Control": "no-cache"},
                    )
        raise HTTPException(status_code=404)

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
    async def timeline(crossing_id: str, hours: int = 24) -> dict:
        """Phase A scope: the in-memory window only. Postgres extends this to
        all history in Phase B."""
        from datetime import UTC, datetime, timedelta

        end = datetime.now(UTC)
        start = end - timedelta(hours=min(hours, 7 * 24))
        rows = state.recent_observations(crossing_id, start, end)
        return {
            "crossing_id": crossing_id,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "observations": [
                {
                    "captured_at": o.captured_at.isoformat(),
                    "state": o.state.value,
                    "object_key": o.object_key,
                    "camera_id": o.camera_id,
                }
                for o in rows
            ],
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
