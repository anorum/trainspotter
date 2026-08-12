"""The `/api/v1/timeline` HTTP surface, in-memory mode.

Phase B added `from`/`to` (ISO timestamps) so the sessions page can pull the
frames that belong to one session by exact bounds. These tests pin the
behavior at the HTTP layer: `from`/`to` bound the range exactly and take
precedence over `hours`, and a malformed timestamp fails as caller error.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from api.app import build_app
from blockade.api.state import LiveState
from blockade.config import Settings
from blockade.schemas import CrossingState, ObservationRecord

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


def _obs(minute: float, state: CrossingState) -> ObservationRecord:
    at = T0 + timedelta(minutes=minute)
    return ObservationRecord(
        crossing_id="SE_12TH_CLINTON",
        camera_id="odot-678",
        captured_at=at,
        observed_at=at,
        state=state,
        confidence=0.9,
        reason="test",
        object_key=f"frames/odot-678/2026/08/11/06/{int(minute * 60_000)}-abcd1234.jpg",
        detector_version="motion/1",
    )


@pytest.fixture
def seeded_app(tmp_path: Path):
    """Build the API app and prime the in-memory LiveState it reads from.

    The LiveState is captured inside the /api/v1/timeline route's closure;
    we reach it through the closure cells so the test exercises the real
    endpoint without also having to spin up the bus tailer.
    """
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
    app = build_app(settings=settings)

    route = next(
        r for r in app.router.routes if getattr(r, "path", None) == "/api/v1/timeline"
    )
    for cell in route.endpoint.__closure__ or ():
        if isinstance(cell.cell_contents, LiveState):
            state: LiveState = cell.cell_contents
            for m in (0, 10, 20, 40, 90):
                state.apply_observation(
                    _obs(m, CrossingState.BLOCKED if m in (10, 20) else CrossingState.CLEAR)
                )
            break
    else:  # pragma: no cover - closure layout changed
        pytest.fail("could not locate LiveState in the /api/v1/timeline closure")

    return app


async def _get(app, path, params):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, params=params)


async def test_from_and_to_bound_the_returned_observations(seeded_app) -> None:
    """Only observations inside [from, to] come back."""
    resp = await _get(
        seeded_app,
        "/api/v1/timeline",
        {
            "crossing_id": "SE_12TH_CLINTON",
            "from": (T0 + timedelta(minutes=5)).isoformat(),
            "to": (T0 + timedelta(minutes=30)).isoformat(),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    minutes = sorted(
        int((datetime.fromisoformat(o["captured_at"]) - T0).total_seconds() // 60)
        for o in body["observations"]
    )
    assert minutes == [10, 20], f"only the two BLOCKED frames in-window should return: {minutes}"
    assert datetime.fromisoformat(body["from"]) == T0 + timedelta(minutes=5)
    assert datetime.fromisoformat(body["to"]) == T0 + timedelta(minutes=30)


async def test_from_and_to_take_precedence_over_hours(seeded_app) -> None:
    """`hours` is the trailing-window shorthand the scrubber uses. When
    `from`/`to` are supplied they must win outright, or the sessions page's
    'pull the frames inside this session' request silently narrows."""
    resp = await _get(
        seeded_app,
        "/api/v1/timeline",
        {
            "crossing_id": "SE_12TH_CLINTON",
            "hours": 1,
            "from": T0.isoformat(),
            "to": (T0 + timedelta(hours=2)).isoformat(),
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    # All five seeded frames sit inside T0..T0+2h. `hours=1` refers to the
    # trailing window from now(), which excludes T0 entirely - if it had
    # narrowed the response, we'd see zero observations.
    assert len(body["observations"]) == 5
    assert datetime.fromisoformat(body["to"]) == T0 + timedelta(hours=2)


async def test_bad_iso_timestamp_returns_422(seeded_app) -> None:
    """A malformed `from` is caller error, not a server crash."""
    resp = await _get(
        seeded_app,
        "/api/v1/timeline",
        {"crossing_id": "SE_12TH_CLINTON", "from": "not-a-timestamp"},
    )
    assert resp.status_code == 422
    assert "not-a-timestamp" in resp.text
