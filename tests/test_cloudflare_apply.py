"""The Cloudflare apply script, checked by what it sends over the wire.

The script's one real output is the sequence of API requests it makes, so the
tests run the actual script against a curl shim standing in for the Cloudflare
API and assert on the recorded request bodies -- the JSON payloads Cloudflare
receives are the contract. The dangerous edge is the cache-settings PUT: the
phase entrypoint is replaced wholesale, so a merge that mishandles existing
rules silently rewrites rules this repo never owned. The zone here already
carries a rule with a `ref` and a field this script has never heard of; both
must come back in the PUT untouched, with only the server-managed fields
(id, version, last_updated) stripped, because Cloudflare rejects a PUT that
echoes them back.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from blockade.config import REPO_ROOT

SCRIPT = REPO_ROOT / "deploy" / "cloudflare" / "apply.sh"

RULE_DESC = "pdxtrain: cache API reads on origin TTL"

# What the zone's cache-settings phase already holds: a rule owned by someone
# else (with a stable `ref` and a field newer than this script), and a stale
# version of our own rule that the merge must replace rather than duplicate.
FOREIGN_RULE = {
    "id": "f0f0f0",
    "version": "3",
    "last_updated": "2026-01-01T00:00:00Z",
    "ref": "bypass-admin",
    "description": "someone else's rule",
    "expression": 'starts_with(http.request.uri.path, "/admin")',
    "action": "set_cache_settings",
    "action_parameters": {"cache": False},
    "enabled": True,
    "exposed_credential_check": {"future": "field"},
}
STALE_OURS = {
    "id": "a1a1a1",
    "version": "7",
    "last_updated": "2026-01-02T00:00:00Z",
    "description": RULE_DESC,
    "expression": 'starts_with(http.request.uri.path, "/api/v1/")',
    "action": "set_cache_settings",
    "action_parameters": {"cache": True, "edge_ttl": {"mode": "respect_origin"}},
    "enabled": True,
}

CURL_SHIM = """\
#!/usr/bin/env python3
# Stands in for curl: answers as the Cloudflare API (and, for the verify
# block, as the live edge) and appends every request it sees to the log.
import json, os, sys

args = sys.argv[1:]
url = next(a for a in args if a.startswith("http"))
method = args[args.index("-X") + 1] if "-X" in args else "GET"
body = args[args.index("--data") + 1] if "--data" in args else None

with open(os.environ["CURL_LOG"], "a") as log:
    log.write(json.dumps({"method": method, "url": url, "body": body}) + "\\n")

if "www.pdxtrain.com" in url:
    print("cache-control: public, max-age=15, s-maxage=20")
    print("cf-cache-status: HIT")
    sys.exit(0)

existing = json.loads(os.environ["EXISTING_RULES"])
if "/zones?" in url:
    out = {"success": True, "result": [{"id": "zone123"}]}
elif url.endswith("/zones/zone123"):
    out = {"success": True, "result": {"plan": {"name": "Free Website"}}}
elif url.endswith("/entrypoint") and method == "GET":
    out = {"success": True, "result": {"rules": existing}}
elif url.endswith("/settings/always_online"):
    out = {"success": True, "result": {"value": "off"}}
else:
    out = {"success": True, "result": {}}
print(json.dumps(out))
"""


def run_apply(tmp_path: Path, *script_args: str) -> tuple[list[dict], str]:
    """Run apply.sh against the shim; return (recorded requests, stdout)."""
    shim = tmp_path / "bin" / "curl"
    shim.parent.mkdir()
    shim.write_text(CURL_SHIM)
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "requests.jsonl"
    log.touch()

    env = os.environ | {
        "PATH": f"{shim.parent}:{os.environ['PATH']}",
        "CLOUDFLARE_API_TOKEN": "test-token",
        "CURL_LOG": str(log),
        "EXISTING_RULES": json.dumps([FOREIGN_RULE, STALE_OURS]),
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT), *script_args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    requests = [json.loads(line) for line in log.read_text().splitlines()]
    return requests, proc.stdout


def cache_rules_put(requests: list[dict]) -> list[dict]:
    puts = [r for r in requests if r["method"] == "PUT" and r["url"].endswith("/entrypoint")]
    assert len(puts) == 1
    return json.loads(puts[0]["body"])["rules"]


def test_apply_preserves_foreign_rules_verbatim(tmp_path: Path) -> None:
    requests, _ = run_apply(tmp_path)
    rules = cache_rules_put(requests)

    foreign = [r for r in rules if r.get("ref") == "bypass-admin"]
    assert len(foreign) == 1, "the pre-existing rule was dropped from the PUT"
    expected = {k: v for k, v in FOREIGN_RULE.items() if k not in ("id", "version", "last_updated")}
    assert foreign[0] == expected


def test_apply_replaces_our_rule_by_description(tmp_path: Path) -> None:
    requests, _ = run_apply(tmp_path)
    rules = cache_rules_put(requests)

    ours = [r for r in rules if r.get("description") == RULE_DESC]
    assert len(ours) == 1, "stale rule duplicated instead of replaced"
    assert ours[0]["expression"] == (
        'starts_with(http.request.uri.path, "/api/v1/")'
        ' and not starts_with(http.request.uri.path, "/api/v1/events")'
    )
    assert ours[0]["action"] == "set_cache_settings"
    assert ours[0]["action_parameters"] == {
        "cache": True,
        "edge_ttl": {"mode": "respect_origin"},
        "browser_ttl": {"mode": "respect_origin"},
    }
    assert ours[0]["enabled"] is True


def test_apply_writes_offline_page_and_never_touches_always_online(
    tmp_path: Path,
) -> None:
    requests, _ = run_apply(tmp_path)

    pages = [r for r in requests if "/custom_pages/500_errors" in r["url"]]
    assert [r["method"] for r in pages] == ["PUT"]
    assert json.loads(pages[0]["body"]) == {
        "url": "https://pdxtrain.com/offline.html",
        "state": "customized",
    }

    always_online = [r for r in requests if "always_online" in r["url"]]
    assert always_online, "always_online was never checked"
    assert {r["method"] for r in always_online} == {"GET"}


def test_dry_run_sends_no_writes(tmp_path: Path) -> None:
    requests, stdout = run_apply(tmp_path, "--dry-run")

    api = [r for r in requests if "api.cloudflare.com" in r["url"]]
    assert api, "dry run should still read the zone"
    assert {r["method"] for r in api} == {"GET"}
    assert "would PUT" in stdout


@pytest.fixture(autouse=True)
def _script_exists() -> None:
    assert SCRIPT.exists()
