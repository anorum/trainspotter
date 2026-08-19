"""The streaming counterpart of ``sessions.py``: one observation at a time.

The batch oracle sees the whole history and looks backwards; a stream cannot.
The gap rule - "a session ends when no BLOCKED arrives for one
``SessionParams.gap``" - turns into a timer: every BLOCKED observation re-arms
an alarm at ``captured_at + gap``, and if the alarm fires first, the session
closes. That is the entire
translation; everything else is bookkeeping.

This class is pure Python with all state in one serializable dataclass, so the
host wrapping it (the sessionizer service) stays a thin shell: the host owns
keyed state and decides when timers fire, while the decisions live here where
they are unit-tested and diffed against the batch oracle. A closed session
emitted by this class must match what ``derive_sessions`` produces from the
same observations - that equivalence is pinned by tests, and a disagreement
is a concrete counterexample rather than a vague worry.

Emission protocol, shaped for the compacted ``crossing.sessions.v1`` topic:
a session is emitted with ``is_open=True`` as soon as it qualifies (enough
observations, long enough) and re-emitted as it grows; the final emission on
close has ``is_open=False``. Every emission carries the same deterministic
``session_id``, so compaction keeps exactly one row per session - the latest.
Runs that never qualify close silently, matching the oracle's filters.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from blockade.schemas import BlockageSession, CrossingState, ObservationRecord
from blockade.sessions import SessionParams


def to_ms(moment: datetime) -> int:
    """Epoch-ms, the shared currency between this core and its host: deadlines
    the core mints and the host's sweep clock must be the same arithmetic."""
    return int(moment.timestamp() * 1000)


def from_ms(epoch_ms: int) -> datetime:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)


@dataclass
class SessionizerState:
    """Everything the sessionizer remembers about one crossing.

    Primitives only, deliberately: this state must survive serialization by
    any host, and epoch milliseconds have no timezone to lose.
    """

    crossing_id: str
    started_at_ms: int
    last_blocked_ms: int
    observation_count: int
    peak_confidence: float
    detector_version: str

    def to_json_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_json_dict(cls, data: dict) -> SessionizerState:
        return cls(**data)


class StreamingSessionizer:
    """Feeds on one crossing's observations; emits session records.

    Stateless itself - the caller owns the ``SessionizerState`` and the timer,
    so any host can supply durability. Methods return the new state, the
    records to emit, and the timer to (re)arm. One instance serves every key.
    """

    def __init__(self, params: SessionParams | None = None) -> None:
        self.params = params or SessionParams()

    def observe(
        self, state: SessionizerState | None, obs: ObservationRecord
    ) -> tuple[SessionizerState | None, list[BlockageSession], int | None]:
        """One observation in; (new state, emissions, timer-at-ms) out.

        UNKNOWN and CLEAR are ignored entirely, exactly as in the oracle: an
        unreadable frame is an absence of evidence, and a momentary CLEAR
        between railcars must not split a session - only sustained silence
        (the gap timer) ends one.
        """
        if obs.state is not CrossingState.BLOCKED:
            return state, [], None

        emissions: list[BlockageSession] = []
        at = to_ms(obs.captured_at)
        if state is not None and at - state.last_blocked_ms > self._gap_ms:
            # This observation arrived past the gap, so the previous session is
            # over - and it must be closed *here*, not left to the timer. Any
            # sane host delays timers by an out-of-orderness margin, so this
            # element is processed before the close timer fires; relying on
            # the timer would silently drop the close emission. The stale timer
            # later fires against the fresh state and is ignored.
            emissions.append(self._session(state, is_open=False))
            state = None
        if state is None:
            state = SessionizerState(
                crossing_id=obs.crossing_id,
                started_at_ms=at,
                last_blocked_ms=at,
                observation_count=1,
                peak_confidence=obs.confidence,
                detector_version=obs.detector_version,
            )
        else:
            state.last_blocked_ms = max(state.last_blocked_ms, at)
            state.observation_count += 1
            state.peak_confidence = max(state.peak_confidence, obs.confidence)

        emissions.append(self._session(state, is_open=True))
        # One millisecond past the boundary, because the gap is inclusive: an
        # observation exactly `gap` after the last one continues the session
        # (the oracle's rule is `<=`), so the timer must fire strictly after
        # that instant. The oracle caught this as an off-by-one on real data -
        # two frames landed exactly 600.000s apart and the session split.
        return state, emissions, state.last_blocked_ms + self._gap_ms + 1

    def on_timer(
        self, state: SessionizerState | None, fired_at_ms: int
    ) -> tuple[SessionizerState | None, list[BlockageSession]]:
        """The gap elapsed with no BLOCKED. Close and clear, or ignore a stale
        timer that a later observation has already superseded."""
        if state is None or fired_at_ms <= state.last_blocked_ms + self._gap_ms:
            return state, []
        return None, [self._session(state, is_open=False)]

    @property
    def _gap_ms(self) -> int:
        return int(self.params.gap.total_seconds() * 1000)

    def _session(self, state: SessionizerState, is_open: bool) -> BlockageSession:
        started = from_ms(state.started_at_ms)
        return BlockageSession(
            session_id=BlockageSession.make_session_id(state.crossing_id, started),
            crossing_id=state.crossing_id,
            started_at=started,
            ended_at=from_ms(state.last_blocked_ms),
            duration_seconds=int((state.last_blocked_ms - state.started_at_ms) / 1000),
            peak_queue_occupancy=state.peak_confidence,
            is_open=is_open,
            detector_version=state.detector_version,
            observation_count=state.observation_count,
        )
