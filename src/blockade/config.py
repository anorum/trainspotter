"""Configuration: process settings from the environment, camera roster from YAML.

The camera roster is deliberately separable from the ODOT API. ``inventory.py``
generates it once a subscription key exists, but a hand-written roster with the
same shape works identically -- so capture can start before the key arrives and
the corpus is continuous either way.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CAMERA_CONFIG = REPO_ROOT / "config" / "cameras.yaml"


class CameraSource(StrEnum):
    """Provenance of a camera's image URL. Recorded so the survey doc can say
    which entries are authoritative and which still need confirming."""

    ODOT_INVENTORY = "odot_inventory"
    """Resolved from the TripCheck Camera Inventory endpoint. Authoritative."""

    MANUAL = "manual"
    """Hand-entered before the API key arrived. Re-verify against the inventory."""


class CameraUsability(StrEnum):
    """Phase 0 survey verdict. Drives which cameras Phase 1 trusts."""

    UNKNOWN = "unknown"
    USABLE = "usable"
    MARGINAL = "marginal"
    UNUSABLE = "unusable"


class Camera(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    camera_id: str = Field(description="Stable internal ID, e.g. 'odot-1234'.")
    name: str = Field(description="Camera name as it appears in the TripCheck inventory.")
    crossing_id: str = Field(
        description="Which crossing this camera watches, e.g. SE_11TH_MILWAUKIE."
    )
    image_url: HttpUrl
    source: CameraSource = CameraSource.MANUAL
    usability: CameraUsability = CameraUsability.UNKNOWN
    poll_interval_seconds: float = Field(
        default=30.0,
        ge=15.0,
        description=(
            "Floor of 15s. ODOT does not archive images and refreshes on its own "
            "schedule; polling faster than the camera refreshes only wastes requests "
            "and risks the key. Phase 0 measures the real interval per camera."
        ),
    )
    enabled: bool = True
    notes: str = ""


class CameraRoster(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cameras: list[Camera]

    def enabled(self) -> list[Camera]:
        return [c for c in self.cameras if c.enabled]

    def by_crossing(self) -> dict[str, list[Camera]]:
        """Grouped for cross-camera consensus, which is the main reason to run all six."""
        out: dict[str, list[Camera]] = {}
        for camera in self.enabled():
            out.setdefault(camera.crossing_id, []).append(camera)
        return out


class Settings(BaseSettings):
    """Environment-driven process settings. Secrets never live in YAML."""

    model_config = SettingsConfigDict(
        env_prefix="BLOCKADE_", env_file=".env", extra="ignore"
    )

    # --- ODOT ---------------------------------------------------------------
    odot_api_key: str | None = Field(
        default=None,
        description=(
            "TripCheck Data API subscription key, sent as Ocp-Apim-Subscription-Key. "
            "Needed only for the camera inventory, which refreshes every 24h -- so "
            "this is roughly one API call per day, not one per frame."
        ),
    )
    odot_api_key_secondary: str | None = Field(
        default=None,
        description="Second key issued with the first; alternated to stay under rate limits.",
    )
    odot_inventory_url: str = "https://api.odot.state.or.us/tripcheck/Cctv/Inventory"

    odot_bounds: str = Field(
        default="-122.670,45.495,-122.645,45.515",
        description=(
            "minLon,minLat,maxLon,maxLat passed to the CCTV Inventory endpoint to "
            "return only inner SE Portland instead of every camera in Oregon. "
            "Covers SE 8th through 12th, Clinton through Powell."
        ),
    )

    # --- Storage ------------------------------------------------------------
    s3_bucket: str = "blockade"
    s3_region: str = "us-west-2"
    s3_endpoint_url: str | None = Field(
        default=None,
        description=(
            "None means real AWS S3. Set to a MinIO/localstack endpoint for the "
            "demo stack and integration tests -- the code path is otherwise identical."
        ),
    )
    local_cache_dir: Path = Path("var/frames")
    local_cache_ttl_days: int = Field(
        default=7,
        description=(
            "The detector reads frames from this cache rather than S3. Compute at "
            "home plus storage in AWS means every re-read would otherwise be billed "
            "egress; a warm local cache makes recurring egress ~zero while S3 keeps "
            "the replay corpus."
        ),
    )
    manifest_dir: Path = Path("var/manifests")

    # --- Polling behaviour --------------------------------------------------
    user_agent: str = Field(
        default="blockade/0.1 (+https://github.com/alexnorum/blockade)",
        description="Identify the project and a contact address. Being anonymous at "
        "30s intervals is how a key gets revoked.",
    )
    request_timeout_seconds: float = 10.0
    max_retries: int = 4
    metrics_port: int = 9102

    camera_config_path: Path = DEFAULT_CAMERA_CONFIG

    @property
    def has_odot_key(self) -> bool:
        return bool(self.odot_api_key)


def load_roster(path: Path | None = None) -> CameraRoster:
    """Load the camera roster from YAML.

    Raises rather than returning an empty roster: a poller that silently captures
    nothing looks healthy in metrics while the corpus quietly has a hole in it.
    """
    path = path or DEFAULT_CAMERA_CONFIG
    if not path.exists():
        raise FileNotFoundError(
            f"No camera roster at {path}. Generate one with `blockade-inventory resolve` "
            "once an ODOT key is available, or hand-write it from config/cameras.example.yaml."
        )
    raw = yaml.safe_load(path.read_text()) or {}
    roster = CameraRoster.model_validate(raw)
    if not roster.enabled():
        raise ValueError(f"Camera roster at {path} has no enabled cameras.")
    return roster


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
