"""The scrape chain, checked as a graph rather than as text.

A metric reaches Prometheus only if a ServiceMonitor's selector matches a
Service's labels, the endpoint names a port that Service publishes, and that
port resolves to one the container actually opens. Every link is a label or a
name written in one file and matched in another, so a rename breaks the chain
without breaking anything that applies: the manifests stay valid, ArgoCD stays
green, and the scrape simply stops. That is how the poller's ServiceMonitor
went on selecting `app: blockade-capture` after the rename and left four alert
rules -- including the critical one -- permanently unable to fire.

The manifests are parsed into the objects Kubernetes resolves, and the joins
are asserted between them. Nothing here matches on file contents.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from blockade.config import REPO_ROOT

DEPLOY = REPO_ROOT / "deploy"

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet"}


@dataclass
class ServicePort:
    name: str | None
    port: int
    target: str | int
    """A named container port, or a number. Absent in the manifest means the
    service port number, which is what Kubernetes assumes."""


@dataclass
class Service:
    at: str
    namespace: str
    name: str
    labels: dict[str, str]
    selector: dict[str, str]
    ports: list[ServicePort]


@dataclass
class Workload:
    at: str
    namespace: str
    name: str
    pod_labels: dict[str, str]
    container_ports: list[tuple[str | None, int]]


@dataclass
class Monitor:
    at: str
    namespace: str
    name: str
    selector: dict[str, str]
    endpoint_ports: list[str]


def _documents() -> list[tuple[Path, dict]]:
    out = []
    for path in sorted(DEPLOY.rglob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if isinstance(doc, dict) and "kind" in doc:
                out.append((path, doc))
    return out


def _parse() -> tuple[list[Service], list[Workload], list[Monitor]]:
    services: list[Service] = []
    workloads: list[Workload] = []
    monitors: list[Monitor] = []
    for path, doc in _documents():
        kind = doc["kind"]
        meta = doc.get("metadata") or {}
        spec = doc.get("spec") or {}
        name = meta.get("name", "<unnamed>")
        namespace = meta.get("namespace", "default")
        at = f"{path.relative_to(REPO_ROOT).as_posix()} {kind}/{name}"
        if kind == "Service":
            services.append(
                Service(
                    at,
                    namespace,
                    name,
                    meta.get("labels") or {},
                    spec.get("selector") or {},
                    [
                        ServicePort(p.get("name"), p["port"], p.get("targetPort", p["port"]))
                        for p in spec.get("ports") or []
                    ],
                )
            )
        elif kind in WORKLOAD_KINDS:
            template = spec.get("template") or {}
            containers = (template.get("spec") or {}).get("containers") or []
            workloads.append(
                Workload(
                    at,
                    namespace,
                    name,
                    (template.get("metadata") or {}).get("labels") or {},
                    [
                        (p.get("name"), p["containerPort"])
                        for c in containers
                        for p in c.get("ports") or []
                    ],
                )
            )
        elif kind == "ServiceMonitor":
            monitors.append(
                Monitor(
                    at,
                    namespace,
                    name,
                    (spec.get("selector") or {}).get("matchLabels") or {},
                    [e["port"] for e in spec.get("endpoints") or [] if "port" in e],
                )
            )
    return services, workloads, monitors


SERVICES, WORKLOADS, MONITORS = _parse()


def _matches(labels: dict[str, str], selector: dict[str, str]) -> bool:
    """Kubernetes equality-based selection. An empty selector is not a match
    here: it would select every object, which is never what a manifest means."""
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def _selected_services(monitor: Monitor) -> list[Service]:
    return [
        s
        for s in SERVICES
        if s.namespace == monitor.namespace and _matches(s.labels, monitor.selector)
    ]


def _scraped_services() -> list[Service]:
    """Only Services a ServiceMonitor selects. A Service fronting pods this repo
    does not declare -- the Strimzi bootstrap, for instance -- has no in-repo
    workload to resolve its target port against, and holding it to that would be
    a failure of the guard rather than of the chain."""
    by_identity = {(s.namespace, s.name): s for m in MONITORS for s in _selected_services(m)}
    return list(by_identity.values())


SCRAPED_SERVICES = _scraped_services()


def test_the_deploy_tree_yields_the_objects_under_test():
    """Guards the parser itself: every assertion below is vacuous if parsing
    silently returns nothing, and a per-object parametrize would report that as
    a green run."""
    assert {m.name for m in MONITORS} >= {"blockade-capture", "detector"}
    assert {s.name for s in SCRAPED_SERVICES} >= {"poller", "detector"}
    assert WORKLOADS


@pytest.mark.parametrize("monitor", MONITORS, ids=lambda m: m.name)
def test_every_service_monitor_selects_a_service(monitor: Monitor):
    """The link the rename broke."""
    assert _selected_services(monitor), (
        f"{monitor.at}: selector {monitor.selector} matches no Service in "
        f"namespace {monitor.namespace}, so nothing is scraped"
    )


@pytest.mark.parametrize("monitor", MONITORS, ids=lambda m: m.name)
def test_every_scraped_endpoint_names_a_published_port(monitor: Monitor):
    published = {p.name for s in _selected_services(monitor) for p in s.ports}
    for port in monitor.endpoint_ports:
        assert port in published, (
            f"{monitor.at}: endpoint port {port!r} is not published by any "
            f"Service it selects (has {sorted(n for n in published if n)})"
        )


@pytest.mark.parametrize("service", SCRAPED_SERVICES, ids=lambda s: f"{s.namespace}-{s.name}")
def test_every_scraped_service_port_lands_on_a_container_port(service: Service):
    """The far end of the chain: a Service can name a target port no container
    opens, and the failure looks identical to a mismatched selector."""
    pods = [
        w
        for w in WORKLOADS
        if w.namespace == service.namespace and _matches(w.pod_labels, service.selector)
    ]
    assert pods, f"{service.at}: selector {service.selector} matches no pod template"

    names = {name for w in pods for name, _ in w.container_ports if name}
    numbers = {number for w in pods for _, number in w.container_ports}
    for port in service.ports:
        assert port.target in names or port.target in numbers, (
            f"{service.at}: port {port.name or port.port} targets {port.target!r}, "
            f"which no selected container opens (names {sorted(names)}, numbers {sorted(numbers)})"
        )
