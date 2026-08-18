"""Camera resolution against a recorded ODOT response.

The fixture is a real CCTV Inventory response (trimmed to the relevant cameras),
not a hand-written approximation. An earlier version of this module guessed at
camelCase field names and would have matched nothing -- the actual schema is
kebab-case, and the human-readable name lives in ``cctv-other`` rather than
``device-name`` for some cameras. Recording the real response is what caught it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from blockade.config import CameraRoster
from poller.inventory import TARGET_CAMERAS, _find_camera_list, resolve

FIXTURE = Path(__file__).parent / "fixtures" / "odot_cctv_inventory.json"


@pytest.fixture
def inventory() -> dict:
    return json.loads(FIXTURE.read_text())


def test_finds_the_documented_envelope(inventory):
    items = _find_camera_list(inventory)
    assert items
    assert {"device-id", "device-name", "cctv-url", "cctv-other"} <= set(items[0])


def test_resolves_all_six_target_cameras(inventory):
    cameras, missing = resolve(inventory)

    assert missing == []
    assert len(cameras) == len(TARGET_CAMERAS) == 6
    assert {c["camera_id"] for c in cameras} == {
        "odot-676",
        "odot-677",
        "odot-678",
        "odot-679",
        "odot-681",
        "odot-682",
    }


def test_matches_on_description_when_device_name_differs(inventory):
    """The 8th Ave cameras have device-name '8th at Division' but cctv-other
    'Portland - 8th at Division'. Matching only one field would drop them."""
    cameras, _ = resolve(inventory)
    eighth = [c for c in cameras if c["crossing_id"] == "SE_8TH_DIVISION"]
    assert len(eighth) == 2


def test_image_urls_are_upgraded_to_https(inventory):
    """The inventory returns http:// URLs. These are polled every 30s for years."""
    cameras, _ = resolve(inventory)
    assert all(c["image_url"].startswith("https://") for c in cameras)


def test_resolved_roster_validates_against_the_config_schema(inventory):
    """What resolve() writes must be loadable by the poller without editing."""
    cameras, _ = resolve(inventory)
    roster = CameraRoster.model_validate({"cameras": cameras})

    assert len(roster.enabled()) == 6
    # Two cameras per crossing, which is what makes cross-camera consensus possible.
    assert {cid: len(c) for cid, c in roster.by_crossing().items()} == {
        "SE_11TH_MILWAUKIE": 2,
        "SE_12TH_CLINTON": 2,
        "SE_8TH_DIVISION": 2,
    }


def test_missing_camera_is_reported_not_skipped(inventory):
    """Cameras get renamed and decommissioned. Silently capturing five of six
    would not be noticed for weeks."""
    cameras, missing = resolve(
        inventory, {"Portland - 12th at Clinton": "SE_12TH_CLINTON", "No Such Camera": "SE_X"}
    )

    assert len(cameras) == 1
    assert missing == ["No Such Camera"]


def test_name_matching_tolerates_formatting_drift(inventory):
    cameras, missing = resolve(inventory, {"portland-12thatclinton": "SE_12TH_CLINTON"})
    assert missing == []
    assert cameras[0]["camera_id"] == "odot-678"


def test_resolve_marks_the_track_blind_cameras_non_scoring(inventory):
    """677 and 679 cannot see their crossings (verified by eye 2026-08-12); a
    roster regen must carry that policy rather than silently re-enfranchise
    them. Matching goes through the same normalisation as name resolution, so
    formatting drift cannot drop the flag."""
    from poller.inventory import NON_SCORING_CAMERAS, TARGET_CAMERAS, resolve

    assert set(TARGET_CAMERAS) >= NON_SCORING_CAMERAS, "a typo here would be a silent no-op"

    resolved, missing = resolve(inventory)
    assert not missing
    by_id = {c["camera_id"]: c for c in resolved}
    assert by_id["odot-677"].get("scores") is False
    assert by_id["odot-679"].get("scores") is False
    for cam_id in ("odot-676", "odot-678", "odot-681", "odot-682"):
        assert "scores" not in by_id[cam_id], "the default stays invisible in the roster"

    # The invariant the docstring above claims: a target written with drifted
    # formatting resolves to the same camera, so it must carry the same policy.
    drifted, _ = resolve(inventory, {"portland-12thatdivision": "SE_12TH_CLINTON"})
    assert drifted[0]["camera_id"] == "odot-679"
    assert drifted[0].get("scores") is False, "the flag must survive formatting drift"
