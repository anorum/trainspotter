"""Load a re-scored history window into Postgres.

The other half of "streaming owns now, batch owns history": after a detector
improves, `blockade-detect scan` re-scores the kept frames, and this module
takes that JSONL, derives sessions with the same parameters the streaming
sessionizer uses, and loads both into the history store. Nothing here touches
Kafka - replaying history through the live sessionizer would corrupt its
keyed state, which is why the batch path exists at all.

Observations join the store as a new versioned layer and the timeline
resolves latest-ingest-wins per instant. Sessions are a projection of them
and are simply rebuilt: the load replaces every session starting inside the
re-scored window with what the new derivation found, so a phantom the better
detector no longer believes in disappears instead of standing next to its
replacement under a different session_id.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from blockade.config import CameraRoster
from blockade.schemas import BlockageSession, ObservationRecord
from blockade.sessions import SessionParams, derive_sessions

LIVE_EDGE = SessionParams().gap
"""How close to now a backfill window may reach: one session gap.

Inside that horizon the streaming sessionizer still owns the data - a batch
derivation there would declare a genuinely open session closed, and the next
streaming emission would fight the load for it. History only.
"""


class BackfillError(Exception):
    """The observations file cannot be loaded as a backfill."""


@dataclass(frozen=True)
class Window:
    """The span one crossing's sessions get rebuilt over.

    Exactly the span of observations actually loaded - min to max
    ``captured_at``, every state counted. Anything wider would delete
    sessions in a range the scan never looked at.
    """

    crossing_id: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class BackfillPlan:
    observations: list[ObservationRecord]
    sessions: list[BlockageSession]
    windows: list[Window]

    def summary(self) -> str:
        states = Counter(o.state.value for o in self.observations)
        lines = [
            f"{len(self.observations)} observations "
            f"({', '.join(f'{k}={v}' for k, v in sorted(states.items()))}), "
            f"{len(self.sessions)} sessions"
        ]
        for w in self.windows:
            kept = [s for s in self.sessions if s.crossing_id == w.crossing_id]
            versions = sorted(
                {o.detector_version for o in self.observations if o.crossing_id == w.crossing_id}
            )
            lines.append(
                f"  {w.crossing_id}: {w.start:%Y-%m-%d %H:%M} .. {w.end:%Y-%m-%d %H:%M} UTC, "
                f"{len(kept)} sessions [{', '.join(versions)}]"
            )
        return "\n".join(lines)


def _witnesses(roster: CameraRoster | None) -> dict[str, set[str]]:
    """Which cameras a crossing's sessions are derived from, per crossing.

    The non-scoring ones are not witnesses: they publish UNKNOWN by policy and
    a re-score that omits them loses nothing.
    """
    out: dict[str, set[str]] = {}
    for camera in roster.enabled() if roster else []:
        if camera.scores:
            out.setdefault(camera.crossing_id, set()).add(camera.camera_id)
    return out


def plan(
    observations: list[ObservationRecord],
    now: datetime | None = None,
    *,
    roster: CameraRoster | None = None,
    allow_empty_window: bool = False,
) -> BackfillPlan:
    """Derive sessions and per-crossing rebuild windows from a scan's output.

    Refuses a scan that does not cover every witness of a crossing it would
    rewrite. Sessions are a per-crossing projection of all its cameras at once,
    but the load's unit is the window, so a scan of one camera of two rebuilds
    that window from half the evidence and deletes what the other camera saw -
    silently, because the plan looks complete. ``allow_empty_window`` waives it
    for the legitimate edges: a window predating a camera, or one whose
    sessions really were phantoms.
    """
    if not observations:
        raise BackfillError("no observations to load")
    now = now or datetime.now(UTC)
    witnesses = _witnesses(roster)

    windows: list[Window] = []
    by_crossing: dict[str, list[ObservationRecord]] = {}
    for obs in observations:
        by_crossing.setdefault(obs.crossing_id, []).append(obs)
    for crossing_id, group in sorted(by_crossing.items()):
        times = [o.captured_at for o in group]
        window = Window(crossing_id=crossing_id, start=min(times), end=max(times))
        if window.end > now - LIVE_EDGE:
            raise BackfillError(
                f"{crossing_id}: window ends {window.end:%Y-%m-%d %H:%M:%S} UTC, inside "
                f"the live edge (now minus {int(LIVE_EDGE.total_seconds() // 60)}min). "
                "Streaming owns now; backfill only history. Scan with --until, or "
                "re-run once the window is old enough."
            )
        seen = {o.camera_id for o in group}
        missing = sorted(witnesses.get(crossing_id, set()) - seen)
        if missing and not allow_empty_window:
            raise BackfillError(
                f"{crossing_id}: the scan covers {', '.join(sorted(seen))}, but the "
                f"crossing is also watched by {', '.join(missing)}. Its sessions are "
                "derived from every scoring camera at once, so loading this would "
                f"rebuild {window.start:%Y-%m-%d %H:%M} .. {window.end:%Y-%m-%d %H:%M} "
                "UTC from part of the evidence and delete what the missing camera saw. "
                "Re-score them together - drop --camera, or scan each and concatenate "
                "the JSONL. If this window predates the missing camera, re-run with "
                "--allow-empty-window."
            )
        windows.append(window)

    # Same parameters as the streaming job; a divergence here would make batch
    # and live disagree about what a session even is.
    sessions = derive_sessions(observations, SessionParams())
    return BackfillPlan(observations=observations, sessions=sessions, windows=windows)


def plan_rows(p: BackfillPlan) -> tuple[list[dict], list[dict], list[dict]]:
    """The plan as the dict shapes db.py speaks - identical to the Kafka
    payloads the materializer parses, so both paths load through one code path."""
    return (
        [o.model_dump(mode="json") for o in p.observations],
        [s.model_dump(mode="json") for s in p.sessions],
        [
            {"crossing_id": w.crossing_id, "window_start": w.start, "window_end": w.end}
            for w in p.windows
        ],
    )
