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
    """True when no camera on this crossing has a fresh observation - the
    displayed state is UNKNOWN whenever this is set, because a dead detector
    must never leave BLOCKED frozen on a screen and a stale BLOCKED must not
    hold the crossing hostage either."""
    since: datetime | None = None
    """When the crossing's consensus state last changed, at event time."""
    latest_observation: ObservationRecord | None = None
    """The freshest observation from the camera that carried the consensus
    vote, or None when no camera is fresh (i.e. ``stale`` is True)."""
    open_session: BlockageSession | None = None
    cameras: list[CameraFrameInfo] = []


class StatusResponse(BaseModel):
    """The whole board: every crossing, one generation timestamp."""

    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    crossings: list[CrossingStatus]
