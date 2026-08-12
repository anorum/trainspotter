"""Alert branch: fire once at the front of a train, then stay quiet.

DESIGN2 section 6. This is the fast path - it reads raw observations directly
and makes a provisional call immediately, where the analytics branch takes its
time and revises. Same stream, two consumers, different tolerances.

The whole design is one idea: **alert on the transition, not on the state.**
A crossing that is blocked for seventy minutes is one event, not thirty-five
notifications.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from blockade.schemas import CrossingState, ObservationRecord


@dataclass(frozen=True)
class AlertPolicy:
    """How eagerly to fire, and how reluctantly to reset."""

    confirmations: int = 2
    """Consecutive BLOCKED observations before firing.

    One frame can be wrong; two consecutive ones are much less likely to be. At a
    ~2 minute cadence this costs a couple of minutes of latency against a
    blockage that typically runs for tens of minutes.
    """

    clear_confirmations: int = 3
    """Consecutive CLEAR observations before re-arming.

    Deliberately larger than `confirmations` -- this is the asymmetry. Gaps
    between railcars, a low-profile well car, or a single bad frame all read as
    momentarily clear, and re-arming on the first of them would fire a second
    alert for the same train. Slow to clear, quick to fire.
    """

    reset_after: timedelta = timedelta(minutes=20)
    """Re-arm regardless if nothing is heard for this long.

    Guards the case where a camera goes dark mid-blockage and never reports
    CLEAR: without it the crossing would stay armed forever and the next real
    train would pass unannounced.
    """


@dataclass
class CrossingAlertState:
    """What the alerter remembers about one crossing. Keyed state, owned by
    whatever hosts the alerter."""

    alerted: bool = False
    blocked_streak: int = 0
    clear_streak: int = 0
    last_seen: datetime | None = None
    alerted_at: datetime | None = None

    def to_json_dict(self) -> dict:
        """Primitives only - this must survive serialization by any host."""
        return {
            "alerted": self.alerted,
            "blocked_streak": self.blocked_streak,
            "clear_streak": self.clear_streak,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "alerted_at": self.alerted_at.isoformat() if self.alerted_at else None,
        }

    @classmethod
    def from_json_dict(cls, data: dict) -> CrossingAlertState:
        return cls(
            alerted=data["alerted"],
            blocked_streak=data["blocked_streak"],
            clear_streak=data["clear_streak"],
            last_seen=datetime.fromisoformat(data["last_seen"]) if data["last_seen"] else None,
            alerted_at=datetime.fromisoformat(data["alerted_at"]) if data["alerted_at"] else None,
        )


@dataclass(frozen=True)
class Alert:
    """A notification worth sending."""

    crossing_id: str
    started_at: datetime
    confidence: float
    reason: str


@dataclass
class RisingEdgeAlerter:
    """Emits one alert per blockage, at its leading edge."""

    policy: AlertPolicy = field(default_factory=AlertPolicy)
    states: dict[str, CrossingAlertState] = field(default_factory=dict)

    def observe(self, obs: ObservationRecord) -> Alert | None:
        """Feed one observation. Returns an Alert only on a rising edge."""
        state = self.states.setdefault(obs.crossing_id, CrossingAlertState())

        # A long silence means we cannot know whether the train left. Re-arm, so
        # that a camera outage during a blockage does not mute the next one.
        silent_for = obs.captured_at - state.last_seen if state.last_seen else timedelta(0)
        if silent_for > self.policy.reset_after:
            state.alerted = False
            state.blocked_streak = 0
            state.clear_streak = 0
        state.last_seen = obs.captured_at

        if obs.state is CrossingState.UNKNOWN:
            # Absence of evidence. It neither confirms nor clears, and must not
            # break a streak -- an unreadable frame mid-blockage is not a gap in
            # the blockage.
            return None

        if obs.state is CrossingState.BLOCKED:
            state.blocked_streak += 1
            state.clear_streak = 0
            if state.alerted or state.blocked_streak < self.policy.confirmations:
                return None
            state.alerted = True
            state.alerted_at = obs.captured_at
            return Alert(
                crossing_id=obs.crossing_id,
                started_at=obs.captured_at,
                confidence=obs.confidence,
                reason=obs.reason,
            )

        state.clear_streak += 1
        state.blocked_streak = 0
        if state.alerted and state.clear_streak >= self.policy.clear_confirmations:
            state.alerted = False
            state.alerted_at = None
        return None

    def is_alerted(self, crossing_id: str) -> bool:
        state = self.states.get(crossing_id)
        return bool(state and state.alerted)
