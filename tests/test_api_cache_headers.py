"""Every read endpoint declares how long its answer may be cached.

The numbers are policy, chosen against the camera cadence: `max-age` stops one
phone re-asking during a single glance, `s-maxage` stops ten thousand phones
becoming ten thousand requests. These tests pin the policy on the wire - a real
uvicorn socket, same rig as test_api_metrics - and pin the one deliberate
absence: an answer that says "the history store is down" must not outlive the
outage, so it carries no Cache-Control at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from api import app as app_module
from api.app import build_app

from tests.test_api_metrics import _running, _settings
from tests.test_api_sse import stub_kafka  # noqa: F401  (fixture)


class _StubPool:
    """Just enough pool for the lifespan's shutdown close()."""

    async def close(self) -> None:
        pass


class _StubMaterializer:
    """Constructed in place of the real one so no Kafka consumer is built."""

    def __init__(self, settings: object, pool: object) -> None:
        pass

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


@pytest.fixture
def stub_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """A history store that answers instantly and holds nothing."""

    async def _connect(dsn: str) -> _StubPool:
        return _StubPool()

    async def _timeline(pool: object, crossing_id: str, start: object, end: object) -> list:
        return []

    async def _sessions(pool: object, crossing_id: object, limit: int) -> list:
        return []

    async def _analytics(pool: object) -> dict:
        return {}

    monkeypatch.setattr(app_module.db, "connect", _connect)
    monkeypatch.setattr(app_module.db, "timeline", _timeline)
    monkeypatch.setattr(app_module.db, "session_list", _sessions)
    monkeypatch.setattr(app_module.db, "analytics", _analytics)
    monkeypatch.setattr(app_module, "Materializer", _StubMaterializer)


async def test_every_read_endpoint_states_its_cache_lifetime(
    tmp_path: Path,
    stub_kafka: None,  # noqa: F811
    stub_history: None,
) -> None:
    settings = _settings(tmp_path)
    settings.database_url = "postgresql://stubbed/history"
    app = build_app(settings=settings)

    lifetimes = {
        "/api/v1/status": "public, max-age=15, s-maxage=20",
        "/api/v1/crossings": "public, max-age=300, s-maxage=3600",
        "/api/v1/timeline?crossing_id=SE_12TH_CLINTON": "public, max-age=30, s-maxage=60",
        "/api/v1/sessions": "public, max-age=60, s-maxage=120",
        "/api/v1/analytics": "public, max-age=300, s-maxage=600",
    }
    async with _running(app) as client:
        for url, expected in lifetimes.items():
            resp = await client.get(url)
            assert resp.status_code == 200, url
            assert resp.headers.get("cache-control") == expected, url


async def test_a_missing_history_store_is_never_cached(
    tmp_path: Path,
    stub_kafka: None,  # noqa: F811
) -> None:
    app = build_app(settings=_settings(tmp_path))

    async with _running(app) as client:
        resp = await client.get("/api/v1/analytics")
        assert resp.status_code == 200
        assert resp.json() == {"available": False, "crossings": {}}
        # A cached outage outlives the outage; this answer must expire now.
        assert "cache-control" not in resp.headers
