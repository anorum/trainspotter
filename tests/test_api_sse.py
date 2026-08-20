"""The event stream's duty during poller silence.

capture_stale is a wall-clock verdict: a dead poller produces no record to
announce itself, so no queue signal ever arrives. The stream's heartbeat tick
is the only place that can notice, and it must push the verdict rather than
keep the last board on screen forever. Runs under a real uvicorn on a real
socket, same rig as test_api_metrics, so the wire behaves as a browser sees it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from api import app as app_module
from api.app import build_app
from api.tailer import StateFeed
from blockade.schemas import CapturedAtSource, FetchStatus, FrameRecord

from tests.test_api_metrics import _running, _settings


@pytest.fixture
def stub_kafka(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_kafka(self: StateFeed) -> None:
        pass

    monkeypatch.setattr(StateFeed, "start", _no_kafka)
    monkeypatch.setattr(StateFeed, "stop", _no_kafka)


def _last_poll(minutes_ago: float) -> FrameRecord:
    fetched = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return FrameRecord(
        camera_id="odot-678",
        crossing_id="SE_12TH_CLINTON",
        fetched_at=fetched,
        captured_at=fetched,
        captured_at_source=CapturedAtSource.FETCHED_AT,
        fetch_status=FetchStatus.OK,
        object_key="frames/odot-678/last.jpg",
        poller_version="test/1",
    )


async def test_the_heartbeat_pushes_a_wall_clock_verdict_change(
    tmp_path: Path, stub_kafka: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "HEARTBEAT_SECONDS", 0.05)
    app = build_app(settings=_settings(tmp_path))

    async with _running(app) as client:
        async with client.stream("GET", "/api/v1/events") as resp:
            lines = resp.aiter_lines()
            assert await anext(lines) == "event: status"
            first = json.loads((await anext(lines)).removeprefix("data: "))
            assert first["feed"]["status"] == "ok"

            # A quiet tick with an unmoved verdict stays a bare comment.
            line = await anext(lines)
            while line == "":
                line = await anext(lines)
            assert line == ": heartbeat"

            # The poller's last word was ten minutes ago. Event time saw
            # nothing change, so no queue signal will ever come; only the
            # heartbeat's wall-clock re-judgement can deliver the news.
            app.state.live.apply_frame(_last_poll(10.0))

            async with asyncio.timeout(5):
                while True:
                    line = await anext(lines)
                    if line == "event: status":
                        break
                    assert line in ("", ": heartbeat")
            pushed = json.loads((await anext(lines)).removeprefix("data: "))
            assert pushed["feed"]["status"] == "capture_stale"
