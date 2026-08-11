"""Response bodies for the serving API.

Every shape here is a plain JSON document that could exist as a static file -
that is a deliberate contract. The browser must not care whether /api/v1/status
came from a live FastAPI process or a blob a scheduled job published, which
keeps a future GitHub Pages deployment (static site + published JSON) open
without rework.

ObservationRecord and BlockageSession are embedded verbatim from schemas.py:
the topics, the tables, and the API speak one language.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from blockade.schemas import BlockageSession, CrossingState, ObservationRecord


class CameraFrameInfo(BaseModel):
    """The latest stored frame for one camera, by reference."""

    model_config = ConfigDict(frozen=True)

    camera_id: str
    name: str
    captured_at: datetime | None = None
    object_key: str | None = None
    """None until the camera's first scored frame arrives (a fresh camera, or
    a fresh server that has not yet replayed far enough)."""


class CrossingStatus(BaseModel):
    """One crossing, now."""

    model_config = ConfigDict(frozen=True)

    crossing_id: str
    state: CrossingState
    stale: bool = False
    """True when the newest observation is old enough that the state shown is
    a memory, not a measurement. The displayed state is downgraded to UNKNOWN
    whenever this is set - a dead detector must never leave BLOCKED frozen on
    a screen."""
    since: datetime | None = None
    """When the current state began (the first observation of the present run
    of identical states)."""
    latest_observation: ObservationRecord | None = None
    open_session: BlockageSession | None = None
    cameras: list[CameraFrameInfo] = []


class StatusResponse(BaseModel):
    """The whole board: every crossing, one generation timestamp."""

    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    crossings: list[CrossingStatus]


class CrossingInfo(BaseModel):
    """Static roster facts for one crossing (from cameras.yaml)."""

    model_config = ConfigDict(frozen=True)

    crossing_id: str
    cameras: list[CameraFrameInfo]
    lat: float | None = None
    lon: float | None = None
