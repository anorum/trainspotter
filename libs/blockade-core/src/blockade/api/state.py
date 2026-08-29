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

from blockade.api.models import CameraFrameInfo, CrossingStatus, FeedHealth, StatusResponse
from blockade.schemas import (
    BlockageSession,
    CrossingState,
    FetchStatus,
    FrameRecord,
    ObservationRecord,
)

CAPTURE_SILENT_AFTER = timedelta(minutes=5)
"""No poll outcome heard at all for this long means our own capture is the
problem - the poller reports every attempt, success or failure, every ~30s,
so silence is ours regardless of what ODOT is doing."""

UPSTREAM_ERROR_STREAK = 3
"""Consecutive failed polls on every camera before the verdict is that
ODOT's server is down rather than one flaky request."""

DEFAULT_STALE_AFTER = timedelta(minutes=15)
"""How long one camera's judgement still counts. Two ceilings decide the number.

**Floor, from the cameras.** The bound has to clear more than one refresh, or a single
skipped one reads as a dead detector. The refresh keeps drifting: ~2 minutes when this
was first chosen, 4-5 minutes by mid-August daytime, and 692 seconds worst over the
Aug 12-18 2026 week-long measurement (docs/architecture.md, poller section). 12 minutes
would leave under half a minute of headroom over that worst case; 15 clears it with
~3.5 minutes of margin.

**Ceiling, from the sessionizer.** Staleness must not outlive a session close, or the
board claims a live blockage the train sheet has already ended and timed. A session
closes ``DEFAULT_GAP`` (15 minutes, blockade.sessions) after its last BLOCKED reading
plus ``OUT_OF_ORDERNESS`` (2 minutes, sessionizer.runner) of drift margin, so 17
minutes is the most this bound may be; 15 keeps the two moving together.

Moving it is a policy change, not a comment fix: re-derive it against both ceilings.
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
        non-scoring camera (``scores: false`` in the roster) still
        renders and still supplies frames, but it must not join consensus:
        its permanent UNKNOWN heartbeat would otherwise read as a fresh
        witness and make ``stale`` unreachable - a dead real camera could
        hide behind it forever. None means every camera scores.
        """
        self._cameras_by_crossing = cameras_by_crossing
        self._stale_after = stale_after
        # The scores policy resolved once, at boot: who votes is a fixed
        # property of the roster, not something to re-derive per consensus
        # call on the SSE hot path.
        self._voters = {
            crossing_id: [cid for cid, _ in cams if scoring is None or cid in scoring]
            for crossing_id, cams in cameras_by_crossing.items()
        }
        self._roster = {cid for cams in cameras_by_crossing.values() for cid, _ in cams}
        # One open session per crossing, plus the last emission per crossing
        # for change detection - both bounded by the roster. Closed sessions
        # are history and belong to Postgres, not this reducer.
        self._open_by_crossing: dict[str, BlockageSession] = {}
        self._last_session: dict[str, BlockageSession] = {}
        self._by_camera: dict[str, ObservationRecord] = {}
        # Poll outcomes, for blaming staleness correctly: last outcome of any
        # kind, last non-error outcome, last genuinely new image, and the
        # error streak - each per camera, all bounded by the roster.
        self._last_poll: dict[str, datetime] = {}
        self._last_ok: dict[str, datetime] = {}
        self._last_new: dict[str, datetime] = {}
        self._error_streak: dict[str, int] = {}
        self._feed_status: str = "ok"
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

    def apply_frame(self, record: FrameRecord) -> None:
        """One poll outcome. The poller reports every attempt, so this stream
        is the heartbeat that lets staleness be blamed correctly.

        Records from cameras outside the roster are dropped: the groupless
        tail replays the whole topic on boot, and a decommissioned camera's
        final healthy polls would otherwise sit in the books forever with a
        zero error streak, vetoing the upstream_down verdict."""
        cam = record.camera_id
        if cam not in self._roster:
            return
        self._last_poll[cam] = record.fetched_at
        if record.fetch_status is FetchStatus.ERROR:
            self._error_streak[cam] = self._error_streak.get(cam, 0) + 1
        else:
            self._error_streak[cam] = 0
            self._last_ok[cam] = record.fetched_at
            if record.fetch_status is FetchStatus.OK:
                self._last_new[cam] = record.fetched_at
        # Event time, like every other reducer rule: the record's own clock
        # decides transitions, so replay converges identically to live.
        status = self._feed(record.fetched_at).status
        if status != self._feed_status:
            self._feed_status = status
            self.changed = True

    def _feed(self, now: datetime) -> FeedHealth:
        """Blame for stale pictures, in order of what the evidence supports.

        Capture silence outranks everything: with no poll outcomes at all we
        cannot testify about ODOT either way, and the fault is ours. A full
        error streak on every camera is their server refusing us. Fresh
        successful polls that yield no new image for longer than the
        staleness bound is their cameras frozen behind a healthy server.
        """
        if not self._last_poll:
            return FeedHealth(status="ok")
        newest_poll = max(self._last_poll.values())
        if now - newest_poll > CAPTURE_SILENT_AFTER:
            return FeedHealth(status="capture_stale", since=newest_poll)
        streaks = [self._error_streak.get(c, 0) for c in self._last_poll]
        if streaks and all(s >= UPSTREAM_ERROR_STREAK for s in streaks):
            last_ok = max(self._last_ok.values(), default=None)
            return FeedHealth(status="upstream_down", since=last_ok)
        newest_new = max(self._last_new.values(), default=None)
        if newest_new is not None and now - newest_new > self._stale_after:
            return FeedHealth(status="upstream_stale", since=newest_new)
        return FeedHealth(status="ok")

    def apply_session(self, session: BlockageSession) -> None:
        previous = self._last_session.get(session.crossing_id)
        self._last_session[session.crossing_id] = session
        if session.is_open:
            self._open_by_crossing[session.crossing_id] = session
        else:
            open_now = self._open_by_crossing.get(session.crossing_id)
            if open_now is not None and open_now.session_id == session.session_id:
                del self._open_by_crossing[session.crossing_id]
        if previous != session:
            self.changed = True

    # --------------------------------------------------------------- views

    def snapshot(self, now: datetime | None = None) -> StatusResponse:
        now = now or datetime.now(UTC)
        crossings = [
            self._crossing_status(crossing_id, now)
            for crossing_id in sorted(self._cameras_by_crossing)
        ]
        return StatusResponse(generated_at=now, crossings=crossings, feed=self._feed(now))

    # ------------------------------------------------------------ internals

    def _consensus(
        self, crossing_id: str, now: datetime
    ) -> tuple[CrossingState, ObservationRecord | None]:
        """Blocked-biased vote over the crossing's fresh observations.

        Any fresh BLOCKED wins: a camera that sees a train outranks one that
        sees nothing (682, back when it still voted, could not resolve the
        tracks at night, and its CLEAR must not have vetoed 681's train).
        CLEAR needs at least one fresh CLEAR
        and no fresh BLOCKED - a fresh UNKNOWN does not veto, because absence
        of evidence is not evidence of clearance either way. No fresh
        observations at all is UNKNOWN, and the caller marks it stale.
        """
        fresh = [
            o
            for camera_id in self._voters.get(crossing_id, ())
            if (o := self._by_camera.get(camera_id)) is not None
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
        open_session = self._open_by_crossing.get(crossing_id)
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
