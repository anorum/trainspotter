"""The live-state reducer: Kafka records in, the current board out.

Pure and synchronous on purpose - the tailer feeds it records, tests feed it
records, and neither can tell the difference. All the judgment calls about
"what is true right now" live here where they are unit-tested:

- Consensus, blocked-biased: cameras on one crossing disagree, and a camera
  that sees a train outranks one that sees nothing. Any fresh BLOCKED holds
  the crossing BLOCKED; CLEAR requires that no fresh camera says otherwise.
  The rule exists because of a real incident: one camera confirmed a train at
  05:45:37 and the other, glare-blind, said CLEAR two seconds later - and
  latest-wins showed CLEAR while a train crossed.
- Staleness: an observation older than ``stale_after`` is a memory, not a
  measurement. It drops out of consensus, and a crossing with no fresh
  observation at all reports UNKNOWN with ``stale=True`` - a dead detector
  must never leave BLOCKED frozen on screen, and a stale BLOCKED must not
  hold the crossing hostage either.
- Out-of-order tolerance: per camera, an older observation never replaces a
  newer one.
- Compaction duplicates: sessions are keyed by their deterministic
  session_id, so replaying a not-yet-compacted topic converges instead of
  duplicating.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from blockade.api.models import CameraFrameInfo, CrossingStatus, StatusResponse
from blockade.schemas import BlockageSession, CrossingState, ObservationRecord

DEFAULT_STALE_AFTER = timedelta(minutes=15)
"""Three times the four-to-five minute camera cadence ODOT has been measured at.

The bound was 6 minutes, three times the ~2 minute cadence observed when it was first
chosen; at the measured refresh rate that had become closer to one cadence than to
three, so a single skipped refresh could read as a dead detector. See
deploy/poller/README.md on why the interval moves. Widening it is a policy change, not
a comment fix.

``STALE_AFTER_MS`` in web/src/lib/scrub.ts is this same bound on the client, and the
two have to move together or the scrubbed board and the live board disagree about
whether a given instant had a witness.
"""


class LiveState:
    """Everything the serving API knows about the present."""

    def __init__(
        self,
        cameras_by_crossing: dict[str, list[tuple[str, str]]],
        stale_after: timedelta = DEFAULT_STALE_AFTER,
        scoring: set[str] | None = None,
    ) -> None:
        """``cameras_by_crossing`` maps crossing_id to [(camera_id, name)] from
        the roster, so every crossing renders its cameras even before their
        first frame arrives.

        ``scoring`` is the set of camera_ids whose judgements count. A
        non-scoring camera (its view does not include the crossing) still
        renders and still supplies frames, but it must not join consensus:
        its permanent UNKNOWN heartbeat would otherwise read as a fresh
        witness and make ``stale`` unreachable - a dead real camera could
        hide behind it forever. None means every camera scores.
        """
        self._cameras_by_crossing = cameras_by_crossing
        self._stale_after = stale_after
        self._scoring = scoring
        self._sessions: dict[str, BlockageSession] = {}
        self._by_camera: dict[str, ObservationRecord] = {}
        self._state_since: dict[str, tuple[CrossingState, datetime]] = {}
        self._latest_frame: dict[str, tuple[str, datetime]] = {}
        self.changed = False
        """Set by the apply methods when the visible board changed; the caller
        (the SSE fan-out) reads and clears it. A heartbeat is not a change."""

    # ------------------------------------------------------------- reducers

    def apply_observation(self, obs: ObservationRecord) -> None:
        newest = self._by_camera.get(obs.camera_id)
        if newest is not None and obs.captured_at < newest.captured_at:
            # Late arrival: history, not news. Postgres keeps it for joins;
            # this camera's present must not move backwards in time.
            return

        if obs.object_key:
            frame = self._latest_frame.get(obs.camera_id)
            if frame is None or obs.captured_at >= frame[1]:
                self._latest_frame[obs.camera_id] = (obs.object_key, obs.captured_at)
                self.changed = True

        self._by_camera[obs.camera_id] = obs

        # Track consensus transitions at event time, so `since` reflects when
        # the crossing's story changed rather than when we noticed.
        state, _ = self._consensus(obs.crossing_id, obs.captured_at)
        previous = self._state_since.get(obs.crossing_id)
        if previous is None or previous[0] is not state:
            self._state_since[obs.crossing_id] = (state, obs.captured_at)
            self.changed = True

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

    def sessions(self) -> list[BlockageSession]:
        return sorted(self._sessions.values(), key=lambda s: s.started_at, reverse=True)

    # ------------------------------------------------------------ internals

    def _consensus(
        self, crossing_id: str, now: datetime
    ) -> tuple[CrossingState, ObservationRecord | None]:
        """Blocked-biased vote over the crossing's fresh observations.

        Any fresh BLOCKED wins: a camera that sees a train outranks one that
        sees nothing (682 cannot even resolve the tracks at night, and its
        CLEAR must not veto 681's train). CLEAR needs at least one fresh CLEAR
        and no fresh BLOCKED - a fresh UNKNOWN does not veto, because absence
        of evidence is not evidence of clearance either way. No fresh
        observations at all is UNKNOWN, and the caller marks it stale.
        """
        fresh = [
            o
            for camera_id, _ in self._cameras_by_crossing.get(crossing_id, [])
            if (self._scoring is None or camera_id in self._scoring)
            and (o := self._by_camera.get(camera_id)) is not None
            and now - o.captured_at <= self._stale_after
        ]
        blocked = [o for o in fresh if o.state is CrossingState.BLOCKED]
        if blocked:
            return CrossingState.BLOCKED, max(blocked, key=lambda o: o.captured_at)
        clear = [o for o in fresh if o.state is CrossingState.CLEAR]
        if clear:
            return CrossingState.CLEAR, max(clear, key=lambda o: o.captured_at)
        if fresh:
            return CrossingState.UNKNOWN, max(fresh, key=lambda o: o.captured_at)
        return CrossingState.UNKNOWN, None

    def _crossing_status(self, crossing_id: str, now: datetime) -> CrossingStatus:
        state, winner = self._consensus(crossing_id, now)
        stale = winner is None
        open_session = next(
            (s for s in self._sessions.values() if s.crossing_id == crossing_id and s.is_open),
            None,
        )
        since_entry = self._state_since.get(crossing_id)
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
            since=since_entry and since_entry[1],
            latest_observation=winner,
            open_session=open_session,
            cameras=cameras,
        )
