"""Resolve the six target cameras from the ODOT TripCheck Camera Inventory.

The inventory refreshes every 24 hours, so this runs roughly once a day -- the
subscription key is spent here and nowhere else. Image polling goes directly to
the per-camera image URLs this endpoint returns and does not consume API quota.

The exact response field names are not documented publicly and have not been
verified against a live response (no subscription key yet). Rather than guess
silently, this module tries a set of candidate field names and, on failure,
prints the keys it actually saw so the mapping can be corrected in one edit. The
raw response is always saved first, so a shape change is diagnosable after the
fact instead of only at the moment it breaks.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import typer
import yaml

from blockade.config import REPO_ROOT, CameraSource, Settings, get_settings

log = logging.getLogger(__name__)

INVENTORY_RAW_PATH = REPO_ROOT / "config" / "odot_camera_inventory.json"
CAMERA_CONFIG_PATH = REPO_ROOT / "config" / "cameras.yaml"

# From DESIGN.md section 2.1. Two cameras per crossing is deliberate: simultaneous
# stopped queues on multiple approaches is a far stronger signal than any single
# camera, and is the main reason to run all six.
#
# Crossing IDs are provisional. Phase 3 loads the FRA National Highway-Rail
# Crossing Inventory and these become aliases for official FRA crossing IDs.
TARGET_CAMERAS: dict[str, str] = {
    "Portland - 11th at Milwaukie N": "SE_11TH_MILWAUKIE",
    "Portland - 11th at Milwaukie S": "SE_11TH_MILWAUKIE",
    "Portland - 12th at Clinton": "SE_12TH_CLINTON",
    "Portland - 12th at Division": "SE_12TH_CLINTON",
    "Portland - 8th at Division": "SE_8TH_DIVISION",
    "Portland - 8th at Division Place": "SE_8TH_DIVISION",
}

# Candidate field names, most likely first. ODOT's API is Azure APIM fronting an
# internal service, so casing conventions vary between resources.
ID_FIELDS = ("cameraId", "CameraId", "camera_id", "id", "Id", "deviceId")
NAME_FIELDS = ("cameraName", "CameraName", "camera_name", "name", "Name", "title", "description")
URL_FIELDS = ("cameraUrl", "CameraUrl", "camera_url", "url", "Url", "imageUrl", "ImageUrl", "href")


def _first_present(item: dict[str, Any], candidates: tuple[str, ...]) -> Any | None:
    for key in candidates:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None


def _find_camera_list(payload: Any) -> list[dict[str, Any]]:
    """Locate the list of camera objects in an unknown envelope.

    The payload may be a bare list or a list nested under some wrapper key. Rather
    than hardcode a guess, walk the structure and take the longest list of dicts.
    """
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload

    best: list[dict[str, Any]] = []
    stack: list[Any] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            if node and all(isinstance(x, dict) for x in node) and len(node) > len(best):
                best = node
            else:
                stack.extend(x for x in node if isinstance(x, (dict, list)))
    return best


def fetch_inventory(settings: Settings) -> Any:
    """Fetch the raw camera inventory. Returns parsed JSON."""
    if not settings.has_odot_key:
        raise RuntimeError(
            "No ODOT subscription key. Set BLOCKADE_ODOT_API_KEY, or hand-write "
            "config/cameras.yaml from config/cameras.example.yaml to start capture "
            "before the key arrives."
        )
    response = httpx.get(
        settings.odot_inventory_url,
        headers={
            "Ocp-Apim-Subscription-Key": settings.odot_api_key or "",
            "Accept": "application/json",
            "User-Agent": settings.user_agent,
        },
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()


def resolve(payload: Any, targets: dict[str, str] | None = None) -> tuple[list[dict], list[str]]:
    """Map the inventory onto the target camera list.

    Returns the resolved camera entries and the names that could not be found.
    A missing name is reported rather than skipped: cameras get renamed and
    decommissioned, and silently capturing five of six would not be obvious for
    weeks.
    """
    targets = targets or TARGET_CAMERAS
    items = _find_camera_list(payload)
    if not items:
        raise ValueError(
            f"No list of camera objects found in the inventory response. "
            f"Top-level type was {type(payload).__name__}. "
            f"Raw response saved to {INVENTORY_RAW_PATH}."
        )

    sample_keys = sorted(items[0].keys())
    indexed: dict[str, dict[str, Any]] = {}
    for item in items:
        name = _first_present(item, NAME_FIELDS)
        if name is not None:
            indexed[str(name).strip().casefold()] = item

    if not indexed:
        raise ValueError(
            f"Found {len(items)} camera objects but none had a recognisable name field. "
            f"Tried {NAME_FIELDS}; observed keys: {sample_keys}. "
            f"Add the correct field name to NAME_FIELDS in this module."
        )

    resolved: list[dict] = []
    missing: list[str] = []
    for target_name, crossing_id in targets.items():
        item = indexed.get(target_name.casefold())
        if item is None:
            missing.append(target_name)
            continue
        raw_id = _first_present(item, ID_FIELDS)
        url = _first_present(item, URL_FIELDS)
        if raw_id is None or url is None:
            raise ValueError(
                f"Camera {target_name!r} matched but is missing an id or image URL. "
                f"Tried id={ID_FIELDS}, url={URL_FIELDS}; observed keys: {sorted(item.keys())}."
            )
        resolved.append(
            {
                "camera_id": f"odot-{raw_id}",
                "name": target_name,
                "crossing_id": crossing_id,
                "image_url": str(url),
                "source": CameraSource.ODOT_INVENTORY.value,
                "usability": "unknown",
                "poll_interval_seconds": 30.0,
                "enabled": True,
                "notes": "",
            }
        )
    return resolved, missing


app = typer.Typer(help="ODOT camera inventory.", no_args_is_help=True)


@app.command()
def fetch(
    output: Path = typer.Option(INVENTORY_RAW_PATH, help="Where to save the raw response."),
) -> None:
    """Fetch and save the raw inventory. Committed as the provenance record."""
    settings = get_settings()
    payload = fetch_inventory(settings)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    items = _find_camera_list(payload)
    typer.echo(f"Saved {len(items)} camera records to {output}")
    if items:
        typer.echo(f"Observed fields: {sorted(items[0].keys())}")


@app.command("resolve")
def resolve_cmd(
    source: Path = typer.Option(
        INVENTORY_RAW_PATH, help="Raw inventory JSON. Fetched first if absent."
    ),
    output: Path = typer.Option(CAMERA_CONFIG_PATH, help="Camera roster to write."),
) -> None:
    """Resolve the six target cameras and write config/cameras.yaml."""
    settings = get_settings()
    if not source.exists():
        payload = fetch_inventory(settings)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps(payload, indent=2, sort_keys=True))
    else:
        payload = json.loads(source.read_text())

    cameras, missing = resolve(payload)
    for name in missing:
        typer.secho(f"NOT FOUND in inventory: {name}", fg=typer.colors.RED, err=True)

    output.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Generated by `blockade-inventory resolve`.\n"
        f"# Source: {settings.odot_inventory_url}\n"
        f"# Resolved: {datetime.now(UTC).isoformat()}\n"
        "# Set `usability` per camera after the Phase 0 survey (docs/camera-survey.md).\n"
    )
    output.write_text(header + yaml.safe_dump({"cameras": cameras}, sort_keys=False))
    typer.echo(f"Wrote {len(cameras)} cameras to {output}")
    if missing:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
