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


COVERAGE_SLACK = SessionParams().gap
"""How far inside a window's edge a camera's own rows may start or end.

One session gap: a hole shorter than that cannot hide or split a session, so
it costs the rebuild nothing. Anything wider means the scan stopped seeing
that camera partway through the span it is about to rewrite.
"""


def _coverage_failure(
    group: list[ObservationRecord], window: Window, expected: set[str]
) -> str | None:
    """Why this window cannot be rebuilt from these observations, if it cannot.

    A window is deleted and re-derived whole, so every scoring camera has to
    cover it whole. Presence is not coverage: ``scan`` drops frames whose bytes
    are no longer in the local cache and reports only what survived, so a
    single surviving row is enough to make a three-day scan of a camera look
    complete while the blockages only that camera witnessed are deleted with
    nothing to replace them.
    """
    spans: dict[str, tuple[datetime, datetime]] = {}
    for o in group:
        first, last = spans.get(o.camera_id, (o.captured_at, o.captured_at))
        spans[o.camera_id] = (min(first, o.captured_at), max(last, o.captured_at))

    absent = sorted(expected - spans.keys())
    if absent:
        return (
            f"the scan covers {', '.join(sorted(spans))}, but the crossing is also "
            f"watched by {', '.join(absent)}. Its sessions are derived from every "
            "scoring camera at once, so loading this would rebuild "
            f"{window.start:%Y-%m-%d %H:%M} .. {window.end:%Y-%m-%d %H:%M} UTC from "
            "part of the evidence and delete what the missing camera saw. Re-score "
            "them together - drop --camera, or scan each and concatenate the JSONL. "
            "If this window predates the missing camera, re-run with "
            "--allow-empty-window."
        )

    short = [
        (camera_id, spans[camera_id])
        for camera_id in sorted(expected)
        if spans[camera_id][0] - window.start > COVERAGE_SLACK
        or window.end - spans[camera_id][1] > COVERAGE_SLACK
    ]
    if short:
        covered = "; ".join(
            f"{camera_id} only {start:%Y-%m-%d %H:%M} .. {end:%Y-%m-%d %H:%M}"
            for camera_id, (start, end) in short
        )
        return (
            f"the window to rebuild is {window.start:%Y-%m-%d %H:%M} .. "
            f"{window.end:%Y-%m-%d %H:%M} UTC, but {covered}. The rebuild covers the "
            "whole window from every scoring camera at once, so the span that camera "
            "is missing from would be re-derived without it and the blockages only it "
            "witnessed deleted. `scan` drops frames whose bytes have left the local "
            "cache without failing, which is what a hole like this usually means: "
            "re-sync that camera's frames for the window and scan again. If it really "
            "was down for that span, re-run with --allow-empty-window."
        )
    return None


def plan(
    observations: list[ObservationRecord],
    now: datetime | None = None,
    *,
    roster: CameraRoster,
    allow_empty_window: bool = False,
) -> BackfillPlan:
    """Derive sessions and per-crossing rebuild windows from a scan's output.

    Refuses a scan whose every witness does not cover every window it would
    rewrite. Sessions are a per-crossing projection of all its cameras at once,
    but the load's unit is the window, so a scan of one camera of two rebuilds
    that window from half the evidence and deletes what the other camera saw -
    silently, because the plan looks complete. ``allow_empty_window`` waives it
    for the legitimate edges: a window predating a camera, one a camera was
    genuinely down for, or one whose sessions really were phantoms.
    """
    if not observations:
        raise BackfillError("no observations to load")
    now = now or datetime.now(UTC)
    # Non-scoring cameras are not witnesses: they publish UNKNOWN by policy
    # and a re-score that omits them loses nothing.
    witnesses = roster.witnesses_by_crossing()

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
        if crossing_id not in witnesses:
            # An unknown crossing must not pass the guard trivially with zero
            # required witnesses - the guard's premise is that the roster
            # describes this history.
            raise BackfillError(
                f"{crossing_id}: not a crossing in the roster (or it has no "
                "scoring cameras) - is the scan or the roster pointed at the "
                "wrong file?"
            )
        if not allow_empty_window:
            failure = _coverage_failure(group, window, witnesses[crossing_id])
            if failure:
                raise BackfillError(f"{crossing_id}: {failure}")
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
