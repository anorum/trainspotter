"""The `/api/v1/timeline` range semantics.

`from`/`to` (ISO timestamps) bound the range exactly and take precedence over
`hours`; a malformed timestamp fails as caller error. The range logic lives in
`resolve_range`, tested directly; the endpoint itself is Postgres-backed and
its storage behavior is pinned in test_history_db.py. The HTTP test here pins
the two non-storage contracts: 422 on a bad stamp, 503 without a history
store - the deployment always configures one, and a half-true history from
memory is worse than an honest error.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from api.app import build_app, resolve_range
from blockade.config import Settings
from fastapi import HTTPException

T0 = datetime(2026, 8, 11, 6, 0, tzinfo=UTC)

ROSTER_YAML = """\
cameras:
  - camera_id: odot-678
    name: 12th at Clinton
    crossing_id: SE_12TH_CLINTON
    image_url: https://example/1.jpg
    source: manual
    poll_interval_seconds: 30.0
    enabled: true
"""


def test_from_and_to_bound_the_range_exactly() -> None:
    start, end = resolve_range(
        (T0 + timedelta(minutes=5)).isoformat(), (T0 + timedelta(minutes=30)).isoformat(), 24
    )
    assert start == T0 + timedelta(minutes=5)
    assert end == T0 + timedelta(minutes=30)


def test_from_and_to_take_precedence_over_hours() -> None:
    """`hours` is the trailing-window shorthand; explicit bounds override it."""
    start, end = resolve_range(T0.isoformat(), (T0 + timedelta(hours=1)).isoformat(), 9999)
    assert (end - start) == timedelta(hours=1)


def test_hours_alone_is_a_trailing_window() -> None:
    start, end = resolve_range(None, None, 24)
    assert (end - start) == timedelta(hours=24)
    assert end.tzinfo is not None


def test_naive_stamps_are_treated_as_utc() -> None:
    start, _ = resolve_range("2026-08-11T06:00:00", None, 24)
    assert start == T0


def test_bad_iso_timestamp_is_caller_error() -> None:
    with pytest.raises(HTTPException) as exc:
        resolve_range("not-a-time", None, 24)
    assert exc.value.status_code == 422


@pytest.fixture
def bare_app(tmp_path: Path):
    """The app without a database: history endpoints must refuse honestly."""
    roster_path = tmp_path / "cameras.yaml"
    roster_path.write_text(ROSTER_YAML)
    settings = Settings(
        s3_bucket="blockade-test",
        local_cache_dir=tmp_path / "frames",
        manifest_dir=tmp_path / "manifests",
        camera_config_path=roster_path,
        # required by StateFeed; never dialed - lifespan is not entered
        kafka_bootstrap="localhost:9092",
    )
    return build_app(settings=settings)


async def _get(app, path, params):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, params=params)


async def test_bad_timestamp_returns_422_over_http(bare_app) -> None:
    resp = await _get(
        bare_app,
        "/api/v1/timeline",
        {"crossing_id": "SE_12TH_CLINTON", "from": "yesterday-ish"},
    )
    assert resp.status_code == 422


async def test_history_without_a_database_is_503_not_a_guess(bare_app) -> None:
    for path, params in (
        ("/api/v1/timeline", {"crossing_id": "SE_12TH_CLINTON"}),
        ("/api/v1/sessions", {}),
    ):
        resp = await _get(bare_app, path, params)
        assert resp.status_code == 503, path
