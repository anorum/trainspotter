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


def _find_repo_root() -> Path:
    """Walk up looking for the workspace root.

    A fixed `parents[N]` breaks the moment a file moves between directories,
    which is exactly what happened when this package moved into a workspace. In
    a container none of these markers exist, and the env vars set in the image
    supply the paths instead, so falling back to the working directory is
    correct rather than a guess.
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / ".git").exists() or (candidate / "libs").is_dir():
            return candidate
    return Path.cwd()


REPO_ROOT = _find_repo_root()
DEFAULT_CAMERA_CONFIG = REPO_ROOT / "config" / "cameras.yaml"


class CameraSource(StrEnum):
    """Provenance of a camera's image URL. Recorded so the survey doc can say
    which entries are authoritative and which still need confirming."""

    ODOT_INVENTORY = "odot_inventory"
    """Resolved from the TripCheck Camera Inventory endpoint. Authoritative."""

    MANUAL = "manual"
    """Hand-entered before the API key arrived. Re-verify against the inventory."""


class Camera(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    camera_id: str = Field(description="Stable internal ID, e.g. 'odot-1234'.")
    name: str = Field(description="Camera name as it appears in the TripCheck inventory.")
    crossing_id: str = Field(
        description="Which crossing this camera watches, e.g. SE_11TH_MILWAUKIE."
    )
    image_url: HttpUrl
    source: CameraSource = CameraSource.MANUAL
    lat: float | None = None
    lon: float | None = None
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

    model_config = SettingsConfigDict(env_prefix="BLOCKADE_", env_file=".env", extra="ignore")

    # --- ODOT ---------------------------------------------------------------
    odot_api_key: str | None = Field(
        default=None,
        description=(
            "TripCheck Data API subscription key, sent as Ocp-Apim-Subscription-Key. "
            "Needed only for the camera inventory, which refreshes every 24h -- so "
            "this is roughly one API call per day, not one per frame."
        ),
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
    s3_bucket: str = "pdx-trainspotter"
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

    # --- Kafka ----------------------------------------------------------------
    kafka_bootstrap: str | None = Field(
        default=None,
        description=(
            "Kafka bootstrap servers. None disables publishing entirely: capture "
            "runs exactly as before and the manifest accumulates, ready to be "
            "drained when a broker appears. Capture must never depend on Kafka."
        ),
    )
    kafka_frames_topic: str = "crossing.frames.v1"
    kafka_observations_topic: str = "crossing.observations.v1"
    kafka_sessions_topic: str = "crossing.sessions.v1"
    kafka_alerts_topic: str = "crossing.alerts.v1"
    kafka_group_id: str = Field(
        default="blockade-detector",
        description=(
            "Consumer group for the streaming detector. Also the replay lever: "
            "a new group id has no committed offsets and starts from the "
            "earliest retained frame, rescoring the topic's history without "
            "touching the group that serves live traffic."
        ),
    )
    database_url: str | None = Field(
        default=None,
        description=(
            "Postgres DSN for the history store. None disables it entirely: the "
            "API then serves only the in-memory window, which is Phase A "
            "behavior. The live board never depends on the database."
        ),
    )
    outbox_dir: Path = Field(
        default=Path("var/outbox"),
        description=(
            "Where the outbox publisher keeps its per-camera position files. "
            "Must live on the same durable volume as the manifest: losing a "
            "position file is harmless (the backlog republishes, at-least-once) "
            "but it should not happen on every pod restart."
        ),
    )

    # --- Polling behaviour --------------------------------------------------
    user_agent: str = Field(
        default="blockade/0.1 (+https://github.com/alexnorum/blockade)",
        description="Identify the project and a contact address. Being anonymous at "
        "30s intervals is how a key gets revoked.",
    )
    request_timeout_seconds: float = 10.0
    metrics_port: int = 9102

    camera_config_path: Path = DEFAULT_CAMERA_CONFIG

    # --- Detection ----------------------------------------------------------
    detector: str = Field(
        default="reference",
        description=(
            "Which detector to run: reference | vlm | classifier | auto. Interchangeable by "
            "design -- which one is best is an open question only real data "
            "answers, and every row records the detector_version that produced it "
            "so results from different detectors are never silently mixed."
        ),
    )
    references_dir: Path = Path("var/references")

    detector_model: str = Field(
        default="claude-haiku-4-5",
        description=(
            "Vision model used to judge each frame. Haiku is chosen deliberately: at "
            "~$0.0003/call it makes a 2-minute cadence across three crossings cost "
            "roughly $19/month, and the task is a three-way classification rather than "
            "a reasoning problem."
        ),
    )

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
