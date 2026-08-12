"""Group observations into blockage sessions.

This is the batch oracle: the same rule the streaming sessionizer applies one
observation at a time, written as a whole-history group-by so the two can be
diffed. Two independent implementations that must agree, because a
disagreement is a concrete counterexample rather than a vague worry - the
pairing has already caught a real off-by-one on the gap boundary.

Sessions are a derived view. They are rebuilt from observations whenever the
parameters change, which is why the parameters live in data and travel with the
rows they produced.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from blockade.schemas import BlockageSession, CrossingState, ObservationRecord

DEFAULT_GAP = timedelta(minutes=10)
DEFAULT_MIN_DURATION = timedelta(minutes=5)


@dataclass(frozen=True)
class SessionParams:
    """How observations become sessions."""

    gap: timedelta = DEFAULT_GAP
    """How long without a BLOCKED reading before a session is considered over.

    **This must exceed the worst-case interval between observations, not just
    the semantic 'how long might a train pause'.** Getting that backwards split a
    real 55-minute blockage into three sessions: the gap was five minutes while
    the camera's own refresh reached 319 seconds, so a pause in *sampling* was
    indistinguishable from a pause in *blocking*.

    Ten minutes clears the observed worst case with room to spare. Use
    `suggest_gap` to check it against real cadence rather than trusting it.
    """

    min_duration: timedelta = DEFAULT_MIN_DURATION
    """Sessions shorter than this are dropped.

    DESIGN.md scopes the project to blockages of five minutes or more, and short
    detections are overwhelmingly noise: of eighteen sessions found on one camera
    in nineteen hours, eight were under five minutes and none of those were real.
    """

    min_observations: int = 2
    """A single BLOCKED reading is never a session. One frame can be wrong; two
    consecutive ones are much less likely to be."""


def suggest_gap(observations: Sequence[ObservationRecord], safety: float = 2.0) -> timedelta:
    """A gap that comfortably exceeds the observed sampling interval.

    Cameras refresh irregularly and the tail matters more than the median, so
    this keys off the largest interval actually seen rather than the typical one.
    """
    times = sorted(o.captured_at for o in observations)
    if len(times) < 3:
        return DEFAULT_GAP
    intervals = [(b - a).total_seconds() for a, b in zip(times, times[1:], strict=False)]
    worst = max(intervals)
    typical = statistics.median(intervals)
    return timedelta(seconds=max(worst * safety, typical * 4, DEFAULT_GAP.total_seconds()))


def derive_sessions(
    observations: Iterable[ObservationRecord],
    params: SessionParams | None = None,
    detector_version: str = "",
) -> list[BlockageSession]:
    """Group a crossing's observations into sessions.

    UNKNOWN neither extends a session nor ends one. An unreadable frame is an
    absence of evidence, and treating it as CLEAR would close sessions during
    weather while treating it as BLOCKED would invent them.
    """
    params = params or SessionParams()
    by_crossing: dict[str, list[ObservationRecord]] = {}
    for obs in observations:
        if obs.state is CrossingState.BLOCKED:
            by_crossing.setdefault(obs.crossing_id, []).append(obs)

    sessions: list[BlockageSession] = []
    for crossing_id, blocked in by_crossing.items():
        blocked.sort(key=lambda o: o.captured_at)
        run: list[ObservationRecord] = []
        for obs in blocked:
            if run and obs.captured_at - run[-1].captured_at <= params.gap:
                run.append(obs)
                continue
            sessions.extend(_close(crossing_id, run, params, detector_version))
            run = [obs]
        sessions.extend(_close(crossing_id, run, params, detector_version))

    sessions.sort(key=lambda s: (s.started_at, s.crossing_id))
    return sessions


def _close(
    crossing_id: str,
    run: list[ObservationRecord],
    params: SessionParams,
    detector_version: str,
) -> list[BlockageSession]:
    if len(run) < params.min_observations:
        return []
    started, ended = run[0].captured_at, run[-1].captured_at
    if ended - started < params.min_duration:
        return []
    return [
        BlockageSession(
            session_id=BlockageSession.make_session_id(crossing_id, started),
            crossing_id=crossing_id,
            started_at=started,
            ended_at=ended,
            duration_seconds=int((ended - started).total_seconds()),
            peak_queue_occupancy=max(o.confidence for o in run),
            is_open=False,
            detector_version=detector_version or run[0].detector_version,
        )
    ]
