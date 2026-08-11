"""The live-state reducer: Kafka records in, the current board out.

Pure and synchronous on purpose - the tailer feeds it records, tests feed it
records, and neither can tell the difference. All the judgment calls about
"what is true right now" live here where they are unit-tested:

- Staleness: a state older than ``stale_after`` is a memory, not a
  measurement, and is downgraded to UNKNOWN with ``stale=True``.
- Out-of-order tolerance: an observation older than the newest already seen
  for its crossing updates history (the recent buffer) but never regresses
  the current state.
- Compaction duplicates: sessions are keyed by their deterministic
  session_id, so replaying a not-yet-compacted topic converges instead of
  duplicating.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta

from blockade.api.models import CameraFrameInfo, CrossingStatus, StatusResponse
from blockade.schemas import BlockageSession, CrossingState, ObservationRecord

DEFAULT_STALE_AFTER = timedelta(minutes=6)
"""Three times the ~2 minute worst observed camera cadence."""

RECENT_BUFFER = timedelta(days=7)
"""How much observation history the in-memory buffer keeps - matches the
observations topic's retention, because that is all a replay can restore."""


class LiveState:
    """Everything the serving API knows about the present."""

    def __init__(
        self,
        cameras_by_crossing: dict[str, list[tuple[str, str]]],
        stale_after: timedelta = DEFAULT_STALE_AFTER,
    ) -> None:
        """``cameras_by_crossing`` maps crossing_id to [(camera_id, name)] from
        the roster, so every crossing renders its cameras even before their
        first frame arrives."""
        self._cameras_by_crossing = cameras_by_crossing
        self._stale_after = stale_after
        self._sessions: dict[str, BlockageSession] = {}
        self._latest: dict[str, ObservationRecord] = {}
        self._state_since: dict[str, datetime] = {}
        self._latest_frame: dict[str, tuple[str, datetime]] = {}
        self._recent: dict[str, deque[ObservationRecord]] = {}
        self.changed = False
        """Set by the apply methods when the visible board changed; the caller
        (the SSE fan-out) reads and clears it. A heartbeat is not a change."""

    # ------------------------------------------------------------- reducers

    def apply_observation(self, obs: ObservationRecord) -> None:
        buffer = self._recent.setdefault(obs.crossing_id, deque())
        buffer.append(obs)
        self._trim(buffer, obs.captured_at)

        newest = self._latest.get(obs.crossing_id)
        if newest is not None and obs.captured_at < newest.captured_at:
            # Late arrival: history, not news. The buffer keeps it for joins;
            # the current state must not move backwards in time.
            return

        if obs.object_key:
            frame = self._latest_frame.get(obs.camera_id)
            if frame is None or obs.captured_at >= frame[1]:
                self._latest_frame[obs.camera_id] = (obs.object_key, obs.captured_at)
                self.changed = True

        if newest is None or obs.state is not newest.state:
            self._state_since[obs.crossing_id] = obs.captured_at
            self.changed = True
        self._latest[obs.crossing_id] = obs

    def apply_session(self, session: BlockageSession) -> None:
        previous = self._sessions.get(session.session_id)
        self._sessions[session.session_id] = session
        if previous != session:
            self.changed = True

    # --------------------------------------------------------------- views

    def snapshot(self, now: datetime | None = None) -> StatusResponse:
        now = now or datetime.now(UTC)
        crossings = [
            self._crossing_status(crossing_id, now)
            for crossing_id in sorted(self._cameras_by_crossing)
        ]
        return StatusResponse(generated_at=now, crossings=crossings)

    def recent_observations(
        self, crossing_id: str, start: datetime, end: datetime
    ) -> list[ObservationRecord]:
        """The session-imagery join, over the in-memory window."""
        return sorted(
            (o for o in self._recent.get(crossing_id, ()) if start <= o.captured_at <= end),
            key=lambda o: o.captured_at,
        )

    def sessions(self) -> list[BlockageSession]:
        return sorted(self._sessions.values(), key=lambda s: s.started_at, reverse=True)

    # ------------------------------------------------------------ internals

    def _crossing_status(self, crossing_id: str, now: datetime) -> CrossingStatus:
        latest = self._latest.get(crossing_id)
        stale = latest is None or now - latest.captured_at > self._stale_after
        state = CrossingState.UNKNOWN if stale else latest.state
        open_session = next(
            (s for s in self._sessions.values() if s.crossing_id == crossing_id and s.is_open),
            None,
        )
        cameras = [
            CameraFrameInfo(
                camera_id=camera_id,
                name=name,
                object_key=(frame := self._latest_frame.get(camera_id)) and frame[0],
                captured_at=frame and frame[1],
            )
            for camera_id, name in self._cameras_by_crossing.get(crossing_id, [])
        ]
        return CrossingStatus(
            crossing_id=crossing_id,
            state=state,
            stale=stale,
            since=self._state_since.get(crossing_id),
            latest_observation=latest,
            open_session=open_session,
            cameras=cameras,
        )

    @staticmethod
    def _trim(buffer: deque[ObservationRecord], newest: datetime) -> None:
        cutoff = newest - RECENT_BUFFER
        while buffer and buffer[0].captured_at < cutoff:
            buffer.popleft()
