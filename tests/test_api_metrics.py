"""The api's Prometheus surface: its own port, route-template labels, SSE gauge.

The metrics live on a separate HTTP server (`settings.metrics_port`) because
the public HTTPRoute forwards every path on the app port - `/metrics` there
would be internet-facing. These tests run the app under a real uvicorn on a
real socket (only the Kafka tails are stubbed; no broker in CI) so SSE
disconnects behave as they do in production, and they scrape the real metrics
port the way Prometheus does.

The dashboard test joins the shipped Grafana ConfigMap against the exporter:
every `blockade_api_*` series a panel queries must be one the process actually
registers, or the panel goes blank with nothing else turning red. The ConfigMap
is a machine-consumed artifact; it is parsed into the JSON Grafana loads and
asserted on semantically, never matched as text.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
import yaml
from api.app import build_app
from api.tailer import StateFeed
from blockade.config import REPO_ROOT, Settings
from prometheus_client import REGISTRY
from prometheus_client.parser import text_string_to_metric_families

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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _scrape(port: int) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """One real HTTP scrape, parsed to {(name, sorted labels): value}."""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
        text = resp.read().decode()
    return {
        (sample.name, tuple(sorted(sample.labels.items()))): sample.value
        for family in text_string_to_metric_families(text)
        for sample in family.samples
    }


def _settings(tmp_path: Path) -> Settings:
    roster_path = tmp_path / "cameras.yaml"
    roster_path.write_text(ROSTER_YAML)
    return Settings(
        s3_bucket="blockade-test",
        local_cache_dir=tmp_path / "frames",
        manifest_dir=tmp_path / "manifests",
        camera_config_path=roster_path,
        kafka_bootstrap="localhost:9092",  # required by StateFeed; start is stubbed
        metrics_port=_free_port(),
    )


@asynccontextmanager
async def _running(app):
    """The app served by uvicorn on a real socket - lifespan entered, metrics
    server live - and a TCP client, so requests and SSE disconnects behave as
    an end user's do."""
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    serve = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.02)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            yield client
    finally:
        server.should_exit = True
        await serve


@pytest.fixture
def stub_kafka(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_kafka(self: StateFeed) -> None:
        pass

    monkeypatch.setattr(StateFeed, "start", _no_kafka)
    monkeypatch.setattr(StateFeed, "stop", _no_kafka)


def _requests_delta(before: dict, after: dict, **labels: str) -> float:
    key = ("blockade_api_requests_total", tuple(sorted(labels.items())))
    return after.get(key, 0.0) - before.get(key, 0.0)


async def test_traffic_is_counted_by_route_template_on_the_metrics_port(
    tmp_path: Path, stub_kafka: None
) -> None:
    settings = _settings(tmp_path)
    app = build_app(settings=settings)

    @app.get("/api/v1/boom")
    async def boom() -> None:
        raise RuntimeError("unhandled, deliberately")

    async with _running(app) as client:
        before = _scrape(settings.metrics_port)

        assert (await client.get("/healthz")).status_code == 200
        # Two distinct object keys must land on one template label, or every
        # frame ever served becomes its own series.
        assert (await client.get("/api/v1/frames/not-a-frame-1")).status_code == 404
        assert (await client.get("/api/v1/frames/not-a-frame-2")).status_code == 404
        assert (await client.get("/definitely/not/a/route")).status_code == 404
        assert (await client.get("/api/v1/boom")).status_code == 500
        # The public app itself must not serve the scrape surface.
        assert (await client.get("/metrics")).status_code == 404

        after = _scrape(settings.metrics_port)

    assert _requests_delta(before, after, route="/healthz", method="GET", status="200") == 1
    assert (
        _requests_delta(
            before, after, route="/api/v1/frames/{object_key:path}", method="GET", status="404"
        )
        == 2
    )
    # Both the unknown path and the /metrics probe above miss every route.
    assert _requests_delta(before, after, route="unmatched", method="GET", status="404") == 2
    # The crash only becomes a 500 above the middleware; it must still count.
    assert _requests_delta(before, after, route="/api/v1/boom", method="GET", status="500") == 1

    latency_count = ("blockade_api_request_seconds_count", (("route", "/healthz"),))
    assert after.get(latency_count, 0.0) - before.get(latency_count, 0.0) == 1


async def test_sse_gauge_tracks_open_streams(tmp_path: Path, stub_kafka: None) -> None:
    settings = _settings(tmp_path)
    gauge = ("blockade_api_sse_clients", ())

    async with _running(build_app(settings=settings)) as client:
        baseline = _scrape(settings.metrics_port).get(gauge, 0.0)
        async with client.stream("GET", "/api/v1/events") as resp:
            # The stream is live once the initial status event arrives.
            line = await anext(resp.aiter_lines())
            assert line.startswith("event:")
            assert _scrape(settings.metrics_port).get(gauge) == baseline + 1

        # Disconnect tears the generator down asynchronously; give it a moment.
        async with asyncio.timeout(5):
            while _scrape(settings.metrics_port).get(gauge) != baseline:
                await asyncio.sleep(0.05)


async def test_metrics_port_stops_with_the_app(tmp_path: Path, stub_kafka: None) -> None:
    """The lifespan owns the scrape server: after shutdown the port is closed,
    so a redeploy in the same pod network namespace can bind it again."""
    settings = _settings(tmp_path)
    async with _running(build_app(settings=settings)):
        assert _scrape(settings.metrics_port)
    with pytest.raises(OSError):
        _scrape(settings.metrics_port)


DASHBOARD = REPO_ROOT / "deploy" / "monitoring" / "dashboard.yaml"

METRIC_NAME = re.compile(r"\bblockade_api_[a-z0-9_]+")


def _exported_api_names() -> set[str]:
    """Every series name the api process exposes, including the `_total`,
    `_bucket`, `_count`, and `_sum` forms PromQL actually queries."""
    names = set()
    for family in REGISTRY.collect():
        if not family.name.startswith("blockade_api_"):
            continue
        names.add(family.name)
        names.update(sample.name for sample in family.samples)
        if family.type == "counter":
            names.add(f"{family.name}_total")
        elif family.type == "histogram":
            names.update(f"{family.name}{s}" for s in ("_bucket", "_count", "_sum"))
    return names


def test_dashboard_queries_only_metrics_the_api_exports() -> None:
    documents = [d for d in yaml.safe_load_all(DASHBOARD.read_text()) if d]
    assert len(documents) == 1
    configmap = documents[0]
    assert configmap["kind"] == "ConfigMap"
    # The kube-prometheus-stack sidecar only picks up labeled ConfigMaps.
    assert configmap["metadata"]["labels"]["grafana_dashboard"] == "1"

    (dashboard,) = (json.loads(body) for body in configmap["data"].values())
    panels = dashboard["panels"]
    assert panels, "a dashboard with no panels monitors nothing"

    exprs = [t["expr"] for p in panels for t in p.get("targets", []) if "expr" in t]
    queried = {name for expr in exprs for name in METRIC_NAME.findall(expr)}
    assert queried, "the app-health dashboard never looks at the api"
    exported = _exported_api_names()
    assert queried <= exported, f"panels query series the api never exports: {queried - exported}"
