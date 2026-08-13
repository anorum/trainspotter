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
}

/** How long a judgement stands as a measurement rather than a memory.
 *  Mirrors DEFAULT_STALE_AFTER in state.py; the two have to move together, or
 *  the scrubbed board and the live board disagree about whether the same
 *  instant had a witness. */
export const STALE_AFTER_MS = 6 * 60_000;

export interface ScrubbedState {
  state: State;
  stale: boolean;
  since: string | null;
  /** Each camera's latest frame at or before the instant, at any age - the
   *  non-scoring cameras' pictures time-travel even though their rows never
   *  vote. */
  frames: Map<string, TimelineObs>;
}

/** The crossing's state at `atMs`, by the four rules the live consensus applies.
 *
 * One vote per camera: a camera's latest row at that instant is its word, so
 * its own newer CLEAR supersedes its older BLOCKED rather than standing beside
 * it. Fresh votes only: an older judgement is a memory, and a dead detector
 * must never leave BLOCKED frozen on screen. Judgements only: UNKNOWN is a
 * refusal to judge - a glare-ruined frame, or a camera that does not view the
 * crossing and publishes a zero-inference UNKNOWN every tick - never an
 * assertion about the crossing. And blocked-biased among what remains, because
 * a camera that sees a train outranks one that sees nothing: the glare-blind
 * camera's CLEAR two seconds later must not clear a crossing with a train
 * across it. No fresh judgement at all is UNKNOWN and stale, which is the
 * truth - at that instant the crossing had no witness.
 *
 * `rows` must be ascending by captured_at, which is what /api/v1/timeline
 * returns; the scan stops at the instant instead of walking the tail of a
 * 30-day window on every frame of a slider drag.
 */
export function stateAt(rows: readonly TimelineObs[], atMs: number): ScrubbedState {
  const freshFrom = atMs - STALE_AFTER_MS;
  const frames = new Map<string, TimelineObs>();
  const votes = new Map<string, { t: number; obs: TimelineObs }>();
  for (const o of rows) {
    const t = new Date(o.captured_at).getTime();
    if (t > atMs) break;
    if (o.object_key) frames.set(o.camera_id, o);
    votes.set(o.camera_id, { t, obs: o });
  }

  let blocked: { t: number; obs: TimelineObs } | undefined;
  let clear: { t: number; obs: TimelineObs } | undefined;
  for (const vote of votes.values()) {
    if (vote.t < freshFrom) continue;
    if (vote.obs.state === "BLOCKED") {
      if (!blocked || vote.t > blocked.t) blocked = vote;
    } else if (vote.obs.state === "CLEAR") {
      if (!clear || vote.t > clear.t) clear = vote;
    }
  }

  const winner = blocked ?? clear;
  return {
    state: winner ? winner.obs.state : "UNKNOWN",
    stale: !winner,
    since: winner ? winner.obs.captured_at : null,
    frames,
  };
}
