"""Resolve the six target cameras from the ODOT TripCheck CCTV Inventory.

The inventory refreshes every 24 hours (per ODOT's Getting Started Guide v5), so
this runs roughly once a day -- the subscription key is spent here and nowhere
else. Image polling goes directly to the per-camera ``cctv-url`` and does not
consume API quota.

Endpoint and response shape are taken from the TripCheck API Getting Started
Guide v5 (2020-10-21), sections 1.3.4 and 2.3.2:

    GET https://api.odot.state.or.us/tripcheck/Cctv/Inventory
        ?DeviceId=&DeviceName=&RouteId=&Bounds=
    Ocp-Apim-Subscription-Key: <primary or secondary key>

    {
      "organization-information": {...},
      "CCTVInventoryRequest": [
        {"device-id": 277, "device-name": "AstoriaUS101MeglerBrNB",
         "latitude": 46.18705, "longitude": -123.85347,
         "cctv-url": "http://www.TripCheck.com/roadcams/cams/...jpg",
         "cctv-other": "US101 at Astoria - ODOT District Office", ...}
      ]
    }

Note the two distinct names: ``device-name`` is a compact slug and ``cctv-other``
is the human-readable description. Which one carries the names in DESIGN.md is
not knowable without a live response, so both are matched.
"""

from __future__ import annotations

import json
import logging
import math
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

INVENTORY_ENVELOPE_KEY = "CCTVInventoryRequest"

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

# Documented field names first; the rest are tolerated in case the schema drifts.
ID_FIELDS = ("device-id", "deviceId", "device_id", "id")
NAME_FIELDS = ("device-name", "deviceName", "device_name", "name")
DESCRIPTION_FIELDS = ("cctv-other", "cctvOther", "description", "title")
URL_FIELDS = ("cctv-url", "cctvUrl", "cctv_url", "url", "imageUrl")
LAT_FIELDS = ("latitude", "lat")
LON_FIELDS = ("longitude", "lon", "lng")


def _first_present(item: dict[str, Any], candidates: tuple[str, ...]) -> Any | None:
    for key in candidates:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None


def _normalise(text: str) -> str:
    """Fold case, punctuation, and spacing so 'Portland - 11th at Milwaukie N'
    matches 'Portland-11th at Milwaukie  N' and similar formatting drift."""
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def _find_camera_list(payload: Any) -> list[dict[str, Any]]:
    """Locate the camera list, preferring the documented envelope key."""
    if isinstance(payload, dict):
        documented = payload.get(INVENTORY_ENVELOPE_KEY)
        if isinstance(documented, list):
            return [x for x in documented if isinstance(x, dict)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    # Fallback: the envelope key changed. Take the longest list of dicts.
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


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def fetch_inventory(settings: Settings, bounded: bool = True) -> Any:
    """Fetch the raw camera inventory.

    ``Bounds`` narrows the response to inner SE Portland rather than every camera
    in Oregon. The full inventory is still fetchable with ``bounded=False``, which
    is what to use when a name fails to resolve and the camera may have moved.
    """
    if not settings.has_odot_key:
        raise RuntimeError(
            "No ODOT subscription key. Set BLOCKADE_ODOT_API_KEY (get one at "
            "https://apiportal.odot.state.or.us -> Products -> TripCheck Data), or "
            "hand-write config/cameras.yaml from config/cameras.example.yaml to "
            "start capture before the key arrives."
        )
    params = {"Bounds": settings.odot_bounds} if bounded else {}
    response = httpx.get(
        settings.odot_inventory_url,
        params=params,
        headers={
            "Ocp-Apim-Subscription-Key": settings.odot_api_key or "",
            "Accept": "application/json",
            "User-Agent": settings.user_agent,
        },
        timeout=30.0,
    )
    if response.status_code == 401:
        raise RuntimeError("401 Access Denied -- the subscription key is invalid or missing.")
    if response.status_code == 429:
        raise RuntimeError("429 Too Many Requests -- rate limited. The inventory only "
                           "changes every 24h; cache it rather than re-fetching.")
    response.raise_for_status()
    return response.json()


def describe(item: dict[str, Any]) -> str:
    """Human-readable one-line summary of an inventory entry."""
    name = _first_present(item, NAME_FIELDS) or "?"
    other = _first_present(item, DESCRIPTION_FIELDS) or ""
    lat = _first_present(item, LAT_FIELDS)
    lon = _first_present(item, LON_FIELDS)
    has_coords = isinstance(lat, (int, float)) and isinstance(lon, (int, float))
    coords = f"{lat:.5f},{lon:.5f}" if has_coords else "?"
    return f"{_first_present(item, ID_FIELDS)!s:>6}  {name:<38} {coords:>20}  {other}"


def resolve(payload: Any, targets: dict[str, str] | None = None) -> tuple[list[dict], list[str]]:
    """Map the inventory onto the target camera list.

    Matches ``device-name`` and ``cctv-other`` against the DESIGN.md names, after
    normalising away case, spacing, and punctuation. Returns the resolved entries
    and the names that could not be found -- a missing name is reported rather
    than skipped, because silently capturing five of six would not be obvious for
    weeks.
    """
    targets = targets or TARGET_CAMERAS
    items = _find_camera_list(payload)
    if not items:
        raise ValueError(
            f"No camera list in the inventory response (expected key "
            f"{INVENTORY_ENVELOPE_KEY!r}). Top-level type was {type(payload).__name__}. "
            f"Raw response saved to {INVENTORY_RAW_PATH}."
        )

    index: dict[str, dict[str, Any]] = {}
    for item in items:
        for field in (NAME_FIELDS, DESCRIPTION_FIELDS):
            value = _first_present(item, field)
            if value is not None:
                index.setdefault(_normalise(str(value)), item)

    if not index:
        raise ValueError(
            f"Found {len(items)} camera objects, none with a recognisable name. "
            f"Tried {NAME_FIELDS + DESCRIPTION_FIELDS}; observed keys: "
            f"{sorted(items[0].keys())}."
        )

    resolved: list[dict] = []
    missing: list[str] = []
    for target_name, crossing_id in targets.items():
        item = index.get(_normalise(target_name))
        if item is None:
            missing.append(target_name)
            continue
        raw_id = _first_present(item, ID_FIELDS)
        url = _first_present(item, URL_FIELDS)
        if raw_id is None or url is None:
            raise ValueError(
                f"Camera {target_name!r} matched but is missing an id or image URL. "
                f"Observed keys: {sorted(item.keys())}."
            )
        lat = _first_present(item, LAT_FIELDS)
        lon = _first_present(item, LON_FIELDS)
        resolved.append(
            {
                "camera_id": f"odot-{raw_id}",
                "name": target_name,
                "crossing_id": crossing_id,
                # https, not the http the inventory returns: these are polled every
                # 30s for years and there is no reason to do it in cleartext.
                "image_url": str(url).replace("http://", "https://", 1),
                "source": CameraSource.ODOT_INVENTORY.value,
                "poll_interval_seconds": 30.0,
                "enabled": True,
                "notes": f"lat={lat} lon={lon}",
            }
        )
    return resolved, missing


app = typer.Typer(help="ODOT camera inventory.", no_args_is_help=True)


def _load_or_fetch(settings: Settings, source: Path, bounded: bool = True) -> Any:
    if source.exists():
        return json.loads(source.read_text())
    payload = fetch_inventory(settings, bounded=bounded)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return payload


@app.command()
def fetch(
    output: Path = typer.Option(INVENTORY_RAW_PATH, help="Where to save the raw response."),
    everything: bool = typer.Option(False, "--all", help="Fetch statewide, ignoring Bounds."),
) -> None:
    """Fetch and save the raw inventory. Committed as the provenance record."""
    settings = get_settings()
    try:
        payload = fetch_inventory(settings, bounded=not everything)
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    items = _find_camera_list(payload)
    typer.echo(f"Saved {len(items)} camera records to {output}")
    if items:
        typer.echo(f"Observed fields: {sorted(items[0].keys())}")


@app.command("list")
def list_cmd(
    source: Path = typer.Option(INVENTORY_RAW_PATH, help="Raw inventory JSON."),
    everything: bool = typer.Option(False, "--all", help="Fetch statewide, ignoring Bounds."),
    near: str = typer.Option(
        "", help="lat,lon -- sort by distance from this point instead of by id."
    ),
) -> None:
    """List cameras in the inventory. Use this when a name fails to resolve: the
    cameras are all here, and picking them by location is more reliable than
    matching a display name ODOT can change."""
    settings = get_settings()
    try:
        payload = _load_or_fetch(settings, source, bounded=not everything)
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    items = _find_camera_list(payload)
    if near:
        lat0, lon0 = (float(x) for x in near.split(","))

        def distance(item: dict) -> float:
            lat, lon = _first_present(item, LAT_FIELDS), _first_present(item, LON_FIELDS)
            if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
                return float("inf")
            return _haversine_m(lat0, lon0, lat, lon)

        items = sorted(items, key=distance)
        for item in items:
            typer.echo(f"{distance(item):8.0f}m  {describe(item)}")
        return

    for item in items:
        typer.echo(describe(item))


@app.command("resolve")
def resolve_cmd(
    source: Path = typer.Option(
        INVENTORY_RAW_PATH, help="Raw inventory JSON. Fetched first if absent."
    ),
    output: Path = typer.Option(CAMERA_CONFIG_PATH, help="Camera roster to write."),
) -> None:
    """Resolve the six target cameras and write config/cameras.yaml."""
    settings = get_settings()
    try:
        payload = _load_or_fetch(settings, source)
        cameras, missing = resolve(payload)
    except (RuntimeError, ValueError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    for name in missing:
        typer.secho(f"NOT FOUND in inventory: {name}", fg=typer.colors.RED, err=True)
    if missing:
        typer.secho(
            "\nCameras get renamed. Run `blockade-inventory list --near 45.5045,-122.6540` "
            "to see what is actually near the crossings, then set the names in "
            "TARGET_CAMERAS or hand-write the roster.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Generated by `blockade-inventory resolve`.\n"
        f"# Source: {settings.odot_inventory_url}\n"
        f"# Resolved: {datetime.now(UTC).isoformat()}\n"
    )
    output.write_text(header + yaml.safe_dump({"cameras": cameras}, sort_keys=False))
    typer.echo(f"Wrote {len(cameras)} cameras to {output}")
    if missing:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
