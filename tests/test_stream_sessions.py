"""The streaming sessionizer against the batch oracle.

The central assertion: driven by the same observations, the streaming
implementation's *closed* sessions equal ``derive_sessions`` output exactly -
same session_ids, same boundaries, same filters. Two independent
implementations that must agree; a disagreement is a counterexample.

The driver below plays the role the host plays: it owns the state and the
timer, feeds observations in event-time order, and fires the timer whenever
event time passes the armed deadline - which is precisely what a watermark
does. If the semantics survive this harness, the host has nothing left to get
wrong but plumbing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from blockade.schemas import BlockageSession, CrossingState, ObservationRecord
from blockade.sessions import SessionParams, derive_sessions
from blockade.stream_sessions import SessionizerState, StreamingSessionizer

T0 = datetime(2026, 8, 10, 6, 0, tzinfo=UTC)


def obs(
    minute: float, state: CrossingState, crossing: str = "SE_12TH_CLINTON"
) -> ObservationRecord:
    return ObservationRecord(
        crossing_id=crossing,
        camera_id="odot-678",
        captured_at=T0 + timedelta(minutes=minute),
        observed_at=T0 + timedelta(minutes=minute),
        state=state,
        confidence=0.9 if state is CrossingState.BLOCKED else 0.85,
        reason="test",
        object_key="frames/x.jpg",
        detector_version="test/1",
    )


class Driver:
    """Stands in for the host: owns state and timers, advances the watermark.

    ``watermark_lag_ms`` mirrors the bounded-out-of-orderness allowance: the
    watermark trails the newest event by that much, so timers between an old
    event and a new one fire *after* the new element is processed. Equivalence
    must hold at zero lag and at the real two-minute lag - the sessionizer
    cannot be allowed to care which race it loses.
    """

    def __init__(self, params: SessionParams | None = None, watermark_lag_ms: int = 0) -> None:
        self.sessionizer = StreamingSessionizer(params)
        self.watermark_lag_ms = watermark_lag_ms
        self.states: dict[str, SessionizerState | None] = {}
        self.timers: dict[str, int | None] = {}
        self.emitted: list[BlockageSession] = []

    def run(self, observations: list[ObservationRecord]) -> list[BlockageSession]:
        for o in sorted(observations, key=lambda o: o.captured_at):
            now_ms = int(o.captured_at.timestamp() * 1000)
            self._fire_due(now_ms - self.watermark_lag_ms)
            state, out, timer = self.sessionizer.observe(self.states.get(o.crossing_id), o)
            self.states[o.crossing_id] = state
            if timer is not None:
                self.timers[o.crossing_id] = timer
            self.emitted.extend(out)
        # End of input: the live host would keep waiting, but the oracle
        # sees the whole file, so flush every pending timer for the diff.
        self._fire_due(None)
        return self.emitted

    def _fire_due(self, now_ms: int | None) -> None:
        for crossing, timer in list(self.timers.items()):
            if timer is not None and (now_ms is None or timer <= now_ms):
                state, out = self.sessionizer.on_timer(self.states.get(crossing), timer)
                self.states[crossing] = state
                self.timers[crossing] = None
                self.emitted.extend(out)

    def closed(self) -> list[BlockageSession]:
        return sorted(
            (s for s in self.emitted if not s.is_open),
            key=lambda s: (s.started_at, s.crossing_id),
        )


def assert_matches_oracle(observations: list[ObservationRecord]) -> None:
    oracle = [s.model_dump() for s in derive_sessions(observations)]
    for lag_ms in (0, 120_000):
        driver = Driver(watermark_lag_ms=lag_ms)
        driver.run(observations)
        streamed = [s.model_dump() for s in driver.closed()]
        assert streamed == oracle, f"disagrees with oracle at watermark lag {lag_ms}ms"


def test_one_long_blockage_matches_oracle() -> None:
    """The 69-minute night train, roughly: blocked readings every ~3 minutes."""
    readings = [obs(m, CrossingState.BLOCKED) for m in range(0, 69, 3)]
    padding = [obs(-30, CrossingState.CLEAR), obs(100, CrossingState.CLEAR)]
    assert_matches_oracle(padding + readings)


def test_a_gap_splits_two_sessions_identically() -> None:
    first = [obs(m, CrossingState.BLOCKED) for m in (0, 3, 6)]
    second = [obs(m, CrossingState.BLOCKED) for m in (30, 33, 36)]
    assert_matches_oracle(first + second)


def test_short_and_sparse_runs_are_recorded_like_the_oracle() -> None:
    lone = [obs(0, CrossingState.BLOCKED)]
    brief = [obs(m, CrossingState.BLOCKED) for m in (20, 22)]
    assert_matches_oracle(lone + brief)


def test_unknown_and_clear_do_not_split_a_session() -> None:
    readings = [
        obs(0, CrossingState.BLOCKED),
        obs(2, CrossingState.UNKNOWN),
        obs(4, CrossingState.CLEAR),
        obs(6, CrossingState.BLOCKED),
        obs(9, CrossingState.BLOCKED),
    ]
    assert_matches_oracle(readings)
    driver = Driver()
    driver.run(readings)
    assert len(driver.closed()) == 1


def test_crossings_are_independent() -> None:
    a = [obs(m, CrossingState.BLOCKED, "SE_12TH_CLINTON") for m in (0, 3, 6)]
    b = [obs(m, CrossingState.BLOCKED, "SE_8TH_DIVISION") for m in (1, 4, 7)]
    assert_matches_oracle(a + b)


def test_open_emissions_grow_and_share_the_final_id() -> None:
    """The compacted-topic contract: every emission for one session carries the
    same session_id, opens are marked open, and the close is the last word."""
    driver = Driver()
    driver.run([obs(m, CrossingState.BLOCKED) for m in (0, 3, 6, 9)])

    assert driver.emitted, "a qualifying session must emit while still open"
    ids = {s.session_id for s in driver.emitted}
    assert len(ids) == 1
    assert [s.is_open for s in driver.emitted][-1] is False
    assert all(s.is_open for s in driver.emitted[:-1])
    durations = [s.duration_seconds for s in driver.emitted]
    assert durations == sorted(durations), "open updates only ever extend"


def test_a_gap_of_exactly_the_limit_continues_the_session() -> None:
    """The counterexample the oracle found on real data: two frames landed
    exactly 600.000 seconds apart (04:45:19 -> 04:55:19 on SE_8TH_DIVISION),
    and the timer fired a hair before the observation that should have
    extended the session - splitting it and losing the sub-minimum remainder.
    The gap is inclusive; the timer must fire strictly after it."""
    readings = [
        obs(0, CrossingState.BLOCKED),
        obs(5, CrossingState.BLOCKED),
        obs(15, CrossingState.BLOCKED),  # exactly gap (10min) after the last
        obs(18, CrossingState.BLOCKED),
    ]
    assert_matches_oracle(readings)
    driver = Driver()
    driver.run(readings)
    assert len(driver.closed()) == 1
    assert driver.closed()[0].duration_seconds == 18 * 60


def test_state_round_trips_through_json() -> None:
    """What the host persists must survive serialization exactly."""
    state = SessionizerState(
        crossing_id="SE_12TH_CLINTON",
        started_at_ms=1_786_300_000_000,
        last_blocked_ms=1_786_300_600_000,
        observation_count=4,
        peak_confidence=0.95,
        detector_version="reference/d40-r0.45-b10-c0.1",
    )
    assert SessionizerState.from_json_dict(state.to_json_dict()) == state
