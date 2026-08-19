"""Record schemas for every stage of the pipeline.

This module is the single source of truth. The JSONL manifest, the Kafka
topics, the Postgres history store, and the serving API all use these
definitions, so the captured corpus replays through the streaming pipeline
with no translation layer.

Schema numbering and rationale trace to docs/history/design.md section 5.

Event-time rule: ``captured_at`` is the event time, everywhere, always. Never
``fetched_at``, never processing time.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


def parse_utc(stamp: str) -> datetime:
    """ISO timestamp to aware-UTC datetime; naive input means UTC.

    The policy for operator-supplied window bounds, shared by the two ends of
    one workflow: ``blockade-detect scan --since/--until`` writes a window and
    ``/api/v1/timeline?from=&to=`` reads it back, so what a naive stamp means
    must not depend on which end parses it. Raises ``ValueError`` on garbage;
    HTTP callers map that to 422.
    """
    parsed = datetime.fromisoformat(stamp)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class FetchStatus(StrEnum):
    """Outcome of a single camera poll."""

    OK = "ok"
    """New image bytes were retrieved and stored."""

    NOT_MODIFIED = "not_modified"
    """Server returned 304 to a conditional GET. No bytes transferred."""

    DUPLICATE = "duplicate"
    """Bytes retrieved but identical to the previous frame. Not stored again."""

    ERROR = "error"
    """Fetch failed. The camera may be down; this is not evidence of a clear crossing."""


class CapturedAtSource(StrEnum):
    """Where ``captured_at`` came from, which sets how much to trust the event time."""

    LAST_MODIFIED = "last_modified"
    """From the image server's Last-Modified header. Authoritative."""

    FETCHED_AT = "fetched_at"
    """Header absent; falls back to fetch time. May lag the true capture by the
    camera's refresh interval, which is why watermarks allow 90s of lateness."""


class BaseRecord(BaseModel):
    """Strict by default: an unexpected field is a schema drift bug, not a warning."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class FrameRecord(BaseRecord):
    """``crossing.frames.v1`` -- key: ``camera_id``.

    Also the line format of the Phase 0 JSONL manifest, which is the backfill
    source for the entire pipeline. Never carries image bytes: object storage
    plus a reference, always (docs/history/design.md section 10.3).
    """

    camera_id: str
    crossing_id: str
    captured_at: datetime = Field(description="EVENT TIME")
    captured_at_source: CapturedAtSource
    fetched_at: datetime
    fetch_status: FetchStatus = FetchStatus.OK

    object_key: str | None = Field(
        default=None,
        description=(
            "S3 key of the JPEG. For DUPLICATE records this points at the first "
            "occurrence of these bytes, so the timeline stays complete without "
            "storing the image twice. None only for ERROR records."
        ),
    )
    content_hash: str | None = Field(
        default=None, description="sha256:... of the image bytes. None for ERROR records."
    )
    image_bytes: int | None = None

    poller_version: str
    error: str | None = Field(default=None, description="Set only when fetch_status is ERROR.")

    @property
    def is_duplicate(self) -> bool:
        return self.fetch_status in (FetchStatus.DUPLICATE, FetchStatus.NOT_MODIFIED)


class CrossingState(StrEnum):
    CLEAR = "CLEAR"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"
    """First-class state. A camera going dark is not a clear crossing."""


class ObservationRecord(BaseRecord):
    """One judgement about one crossing at one instant -- the analytical record.

    This is the dataset. ``sessions`` are derived from a sequence of these and can
    be rebuilt at any time, so observations are the layer that must be right.
    """

    crossing_id: str
    camera_id: str
    captured_at: datetime = Field(description="EVENT TIME -- when the camera took the frame.")
    observed_at: datetime = Field(description="When the judgement was made.")

    state: CrossingState
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(
        description="One line explaining the judgement. Kept for auditing the dataset: a "
        "statistic nobody can trace back to a frame and a reason is hard to trust."
    )

    object_key: str = Field(description="The exact frame this judgement came from.")
    detector_version: str = Field(
        description="Model plus prompt hash. Rows produced by different prompts are not "
        "directly comparable, and re-runs must be distinguishable from originals."
    )

    @property
    def is_confident(self) -> bool:
        """A judgement rather than an abstention.

        UNKNOWN is an honest answer, not a measurement -- a night of UNKNOWN is
        'no data', never 'no blockages'. Consumers that cannot call Python
        restate this rule where they consume the record: the analytics SQL in
        services/api db.py and the web scrubber's scrub.ts.
        """
        return self.state is not CrossingState.UNKNOWN


class BlockageSession(BaseRecord):
    """``blockage_sessions`` -- the analytical core. Phase 2 SessionJob output."""

    session_id: str
    crossing_id: str
    started_at: datetime
    ended_at: datetime | None = None
    duration_seconds: int | None = None
    peak_queue_occupancy: float | None = None
    is_open: bool = True
    detector_version: str
    observation_count: int | None = None
    """How many BLOCKED frames built this run - the evidence a consumer
    thresholds on. None on rows recorded before the field existed; those
    all passed the era's derivation minimums, so readers treat None as
    certified."""

    @staticmethod
    def make_session_id(crossing_id: str, started_at: datetime) -> str:
        """Derive a session ID that is stable for the life of the session.

        Assigned when the session opens and never regenerated when it updates or
        closes. Every downstream consumer -- API, MQTT, future notifier -- uses it
        for idempotency, so an unstable ID means duplicate alerts and broken
        upserts on replay. The original design flagged this as expensive to retrofit.

        Normalised to UTC before hashing: a naive or local-time input would make the
        ID depend on the machine that computed it, so a replay on a differently
        configured host would mint new IDs for sessions that already exist.
        """
        if started_at.tzinfo is None:
            raise ValueError(
                "started_at must be timezone-aware; a naive datetime yields an "
                "unstable session_id that depends on the host's local timezone"
            )
        seed = f"{crossing_id}|{started_at.astimezone(UTC).isoformat()}"
        return hashlib.sha256(seed.encode()).hexdigest()[:32]
