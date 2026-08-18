/** What the board shows at a past instant, derived from raw timeline rows.
 *
 * The live board answers "what is true right now" server-side, in
 * `LiveState._consensus` (libs/blockade-core/src/blockade/api/state.py). The
 * time slider asks the same question of an instant hours ago, and has to
 * answer it on the client from `/api/v1/timeline` rows, because the reducer
 * only ever holds the present. That makes this a second copy of a rule the
 * corridor's honesty depends on, so it lives here as a pure function with the
 * consensus scenarios pinned in scrub.test.ts rather than inline in a memo.
 */

import type { State } from "./crossings";

export interface TimelineObs {
  captured_at: string;
  state: State;
  object_key: string | null;
  camera_id: string;
  detector_version: string;
  /** Optional pre-parsed captured_at. A slider drag calls stateAt dozens of
   *  times a second over a window that can hold tens of thousands of rows;
   *  parsing each ISO string once at fetch time (see withTimes) instead of
   *  on every call is the difference the drag can feel. */
  tMs?: number;
}

/** Rows with captured_at parsed once, for callers that hold a window. */
export function withTimes(rows: readonly TimelineObs[]): TimelineObs[] {
  return rows.map((o) => ({ ...o, tMs: Date.parse(o.captured_at) }));
}

/** Marks the zero-inference UNKNOWNs a camera that cannot see its crossing
 *  publishes every tick. They are policy, not observation: the camera never
 *  looked, so unlike a glare-ruined frame it is not a witness that refused to
 *  judge. Rows a blind camera produced before the policy landed still carry a
 *  real detector's version and still count, until the re-score and backfill
 *  layer `unscored/1` over them - which is why that backfill follows the
 *  merge.
 *
 *  The other end of this contract is `UNSCORED_VERSION` in
 *  services/detector/src/detector/runner.py, which mints the rows. Both sides
 *  are pinned: tests/test_detector_stream.py asserts the minted stamp stays in
 *  this namespace, and scrub.test.ts feeds the real literal through here. */
const UNSCORED_PREFIX = "unscored/";

function isPolicyUnknown(o: TimelineObs): boolean {
  return o.state === "UNKNOWN" && o.detector_version.startsWith(UNSCORED_PREFIX);
}

/** How long a judgement stands as a measurement rather than a memory.
 *  Mirrors DEFAULT_STALE_AFTER in state.py; the two have to move together, or
 *  the scrubbed board and the live board disagree about whether the same
 *  instant had a witness. */
export const STALE_AFTER_MS = 15 * 60_000;

export interface ScrubbedState {
  state: State;
  stale: boolean;
  /** Each camera's latest frame at or before the instant, at any age - the
   *  non-scoring cameras' pictures time-travel even though their rows never
   *  vote. */
  frames: Map<string, TimelineObs>;
}

/** The crossing's state at `atMs`, by the rules the live consensus applies.
 *
 * One vote per camera: a camera's latest row at that instant is its word, so
 * its own newer CLEAR supersedes its older BLOCKED rather than standing beside
 * it. Fresh votes only: an older judgement is a memory, and a dead detector
 * must never leave BLOCKED frozen on screen. Blocked-biased among the
 * judgements, because a camera that sees a train outranks one that sees
 * nothing: the glare-blind camera's CLEAR two seconds later must not clear a
 * crossing with a train across it. And a fresh UNKNOWN from a camera that
 * actually looked is a witness that refused to judge, not an absence of one -
 * it cannot outvote a judgement, but it does mean the crossing was watched, so
 * the board says UNKNOWN rather than "no recent signal". Only the policy
 * UNKNOWNs of a camera that cannot see the crossing are absent from the vote
 * entirely; they still carry their picture into `frames`.
 *
 * `rows` must be ascending by captured_at, which is what /api/v1/timeline
 * returns; the scan stops at the instant instead of walking the tail of a
 * 30-day window on every frame of a slider drag.
 */
export function stateAt(rows: readonly TimelineObs[], atMs: number): ScrubbedState {
  const freshFrom = atMs - STALE_AFTER_MS;
  const frames = new Map<string, TimelineObs>();
  const votes = new Map<string, { t: number; state: State }>();
  for (const o of rows) {
    const t = o.tMs ?? Date.parse(o.captured_at);
    if (t > atMs) break;
    if (o.object_key) frames.set(o.camera_id, o);
    if (!isPolicyUnknown(o)) votes.set(o.camera_id, { t, state: o.state });
  }

  // Which fresh states exist is the whole question: blocked-bias means any
  // fresh BLOCKED answers BLOCKED, else any fresh CLEAR answers CLEAR, else a
  // fresh witness answers UNKNOWN. Recency among the fresh votes never
  // matters here - the scrubbed board nulls latest_observation - so nothing
  // tracks it.
  let sawBlocked = false;
  let sawClear = false;
  let sawFresh = false;
  for (const vote of votes.values()) {
    if (vote.t < freshFrom) continue;
    sawFresh = true;
    if (vote.state === "BLOCKED") sawBlocked = true;
    else if (vote.state === "CLEAR") sawClear = true;
  }

  return {
    state: sawBlocked ? "BLOCKED" : sawClear ? "CLEAR" : "UNKNOWN",
    stale: !sawFresh,
    frames,
  };
}

/** The scrubber's rule, swept over a whole window: the intervals during which
 * the board would have shown BLOCKED.
 *
 * Blocked-bias makes this a union: the consensus is BLOCKED exactly while
 * some camera's latest judgement is a fresh BLOCKED, so each BLOCKED row
 * opens a span that runs until that camera's next judgement supersedes it or
 * freshness expires - whichever comes first. Overlapping spans merge. The
 * lanes under the slider draw these, which is what makes them agree with
 * what scrubbing to any instant would show.
 *
 * `rows` ascending by captured_at, as /api/v1/timeline returns.
 */
export function blockedSpans(rows: readonly TimelineObs[]): [number, number][] {
  // Each camera's judgement instants, in order, for the supersession lookup.
  const nextJudgement = new Map<string, number[]>();
  for (const o of rows) {
    if (isPolicyUnknown(o)) continue;
    const t = o.tMs ?? Date.parse(o.captured_at);
    let list = nextJudgement.get(o.camera_id);
    if (!list) nextJudgement.set(o.camera_id, (list = []));
    list.push(t);
  }
  const spans: [number, number][] = [];
  const cursor = new Map<string, number>();
  for (const o of rows) {
    if (o.state !== "BLOCKED" || isPolicyUnknown(o)) continue;
    const t = o.tMs ?? Date.parse(o.captured_at);
    const times = nextJudgement.get(o.camera_id)!;
    let i = cursor.get(o.camera_id) ?? 0;
    while (times[i] <= t) i++;
    cursor.set(o.camera_id, i);
    const supersededAt = i < times.length ? times[i] : Infinity;
    spans.push([t, Math.min(supersededAt, t + STALE_AFTER_MS)]);
  }
  spans.sort((a, b) => a[0] - b[0]);
  const merged: [number, number][] = [];
  for (const [a, b] of spans) {
    const last = merged[merged.length - 1];
    if (last && a <= last[1]) last[1] = Math.max(last[1], b);
    else merged.push([a, b]);
  }
  return merged;
}
