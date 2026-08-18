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
is the human-readable description. Which one carries the names in
docs/history/design.md is not knowable without a live response, so both are
matched.
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

INVENTORY_ENVELOPE_KEY = "CCTVInventoryRequest"

# Two cameras per crossing was the original design (docs/history/design.md
# section 2.1). In practice two of the six views turned out not to include
# their crossing at all (NON_SCORING_CAMERAS below), so today the second
# camera is a picture on the board, not a second witness.
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

# Verified by eye 2026-08-12: these two views do not include their crossing (677
# watches the Gideon intersection, 679 the Division one), so they are captured and
# shown but never judged. Kept here so `resolve` regenerates the roster with the
# same policy instead of silently re-enfranchising them.
NON_SCORING_CAMERAS: set[str] = {
    "Portland - 11th at Milwaukie S",
    "Portland - 12th at Division",
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
    """Locate the camera list under the documented envelope key.

    The committed inventory in config/ proves the documented shape is real; if
    ODOT ever renames the envelope, resolve() raises with the observed
    top-level type and the raw response is on disk to inspect.
    """
    if isinstance(payload, dict):
        documented = payload.get(INVENTORY_ENVELOPE_KEY)
        if isinstance(documented, list):
            return [x for x in documented if isinstance(x, dict)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


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
        raise RuntimeError(
            "429 Too Many Requests -- rate limited. The inventory only "
            "changes every 24h; cache it rather than re-fetching."
        )
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

    Matches ``device-name`` and ``cctv-other`` against the
    docs/history/design.md names, after normalising away case, spacing, and
    punctuation. Returns the resolved entries and the names that could not be
    found -- a missing name is reported rather than skipped, because silently
    capturing five of six would not be obvious for weeks.
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
                "lat": float(lat) if lat is not None else None,
                "lon": float(lon) if lon is not None else None,
                # Emitted only when False, so regenerating leaves every scoring
                # camera byte-identical; written here rather than appended
                # because the YAML dump keeps insertion order.
                **({"scores": False} if target_name in NON_SCORING_CAMERAS else {}),
                "poll_interval_seconds": 30.0,
                "enabled": True,
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
) -> None:
    """List cameras in the inventory, with names and coordinates. Use this when
    a name fails to resolve - the cameras are all here to grep."""
    settings = get_settings()
    try:
        payload = _load_or_fetch(settings, source, bounded=not everything)
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    for item in _find_camera_list(payload):
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
            "\nCameras get renamed. Run `blockade-inventory list` to see every camera "
            "with its coordinates, find the crossings by location, then set the names "
            "in TARGET_CAMERAS or hand-write the roster.",
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
