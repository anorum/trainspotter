/** The scrubbed board's decision procedure, against the scenarios the live
 * reducer's own tests encode (tests/test_api_state.py).
 *
 * The rule is duplicated across two languages by necessity - the reducer only
 * holds the present, and the slider asks about the past - so the scenarios
 * that matter are pinned on both sides. Every case here is one the corridor
 * actually produces: a blockage ending, a glare-blind camera disagreeing with
 * a camera that sees the train, a detector going quiet, and the two cameras
 * that cannot see their crossing at all.
 */

import { describe, expect, it } from "vitest";
import { STALE_AFTER_MS, stateAt, type TimelineObs } from "./scrub";

const T0 = Date.UTC(2026, 7, 12, 5, 45, 0);

function at(seconds: number): number {
  return T0 + seconds * 1000;
}

function row(
  seconds: number,
  camera: string,
  state: TimelineObs["state"],
  objectKey: string | null = `frames/${camera}/${seconds}.jpg`,
): TimelineObs {
  return {
    captured_at: new Date(at(seconds)).toISOString(),
    state,
    object_key: objectKey,
    camera_id: camera,
  };
}

/** The endpoint returns rows ascending by captured_at; so does this. */
function timeline(...rows: TimelineObs[]): TimelineObs[] {
  return [...rows].sort((a, b) => a.captured_at.localeCompare(b.captured_at));
}

describe("stateAt", () => {
  it("lets a camera's own later CLEAR supersede its earlier BLOCKED", () => {
    // The train clears at 05:46:00 and 678 says so every 30s afterwards. Six
    // minutes of trailing red is exactly what the live board never shows: its
    // consensus keeps one record per camera, the latest.
    const rows = timeline(
      row(0, "odot-678", "BLOCKED"),
      row(30, "odot-678", "BLOCKED"),
      row(60, "odot-678", "CLEAR"),
      row(90, "odot-678", "CLEAR"),
      row(120, "odot-678", "CLEAR"),
    );

    const result = stateAt(rows, at(120));

    expect(result.state).toBe("CLEAR");
    expect(result.stale).toBe(false);
    expect(result.since).toBe(new Date(at(120)).toISOString());
  });

  it("holds BLOCKED when another camera's fresh CLEAR disagrees", () => {
    // The incident the blocked bias exists for: 681 confirmed a train, and
    // 682 - which cannot resolve the tracks at night - said CLEAR two seconds
    // later. Latest-wins would show CLEAR while a train crossed.
    const rows = timeline(
      row(37, "odot-681", "BLOCKED"),
      row(39, "odot-682", "CLEAR"),
    );

    const result = stateAt(rows, at(40));

    expect(result.state).toBe("BLOCKED");
    expect(result.since).toBe(new Date(at(37)).toISOString());
  });

  it("reads UNKNOWN and stale once every judgement is older than the bound", () => {
    // 676's detector went quiet after 05:45:37. A dead detector must never
    // leave BLOCKED frozen on the board, scrubbed or live.
    const rows = timeline(row(37, "odot-676", "BLOCKED"));

    const fresh = stateAt(rows, at(37) + STALE_AFTER_MS - 1000);
    const stale = stateAt(rows, at(37) + STALE_AFTER_MS + 1000);

    expect(fresh.state).toBe("BLOCKED");
    expect(stale.state).toBe("UNKNOWN");
    expect(stale.stale).toBe(true);
    expect(stale.since).toBeNull();
  });

  it("keeps a non-scoring camera's frames while its UNKNOWNs never vote", () => {
    // 679 watches the Division intersection with the crossing out of frame,
    // so it publishes a zero-inference UNKNOWN every tick. The board keeps
    // showing its picture; the crossing's state stays 678's word.
    const rows = timeline(
      row(0, "odot-678", "BLOCKED"),
      row(10, "odot-679", "UNKNOWN"),
      row(40, "odot-679", "UNKNOWN"),
    );

    const result = stateAt(rows, at(60));

    expect(result.state).toBe("BLOCKED");
    expect(result.stale).toBe(false);
    expect(result.frames.get("odot-679")?.captured_at).toBe(new Date(at(40)).toISOString());
    expect(result.frames.get("odot-678")?.captured_at).toBe(new Date(at(0)).toISOString());
  });

  it("treats a camera's newer UNKNOWN as withdrawing its older judgement", () => {
    // Glare ruins 678's view at 05:45:30. A refusal to judge is the camera's
    // current word, so the crossing has no witness rather than a two-minute-old
    // BLOCKED still standing.
    const rows = timeline(
      row(0, "odot-678", "BLOCKED"),
      row(30, "odot-678", "UNKNOWN"),
    );

    const result = stateAt(rows, at(60));

    expect(result.state).toBe("UNKNOWN");
    expect(result.stale).toBe(true);
  });

  it("ignores rows after the scrubbed instant", () => {
    const rows = timeline(
      row(0, "odot-678", "CLEAR"),
      row(60, "odot-678", "BLOCKED"),
    );

    const result = stateAt(rows, at(30));

    expect(result.state).toBe("CLEAR");
    expect(result.frames.has("odot-678")).toBe(true);
    expect(result.frames.get("odot-678")?.captured_at).toBe(new Date(at(0)).toISOString());
  });

  it("carries a camera's last frame forward even when its newer rows have none", () => {
    // A frameless row is a tick the poller recorded without an image; the
    // panel should keep showing the last picture that exists rather than
    // blanking to "No frame yet".
    const rows = timeline(
      row(0, "odot-678", "CLEAR"),
      row(30, "odot-678", "CLEAR", null),
    );

    const result = stateAt(rows, at(60));

    expect(result.frames.get("odot-678")?.captured_at).toBe(new Date(at(0)).toISOString());
  });

  it("reports UNKNOWN and stale for a crossing with no rows at all", () => {
    const result = stateAt([], at(0));

    expect(result).toEqual({ state: "UNKNOWN", stale: true, since: null, frames: new Map() });
  });
});
