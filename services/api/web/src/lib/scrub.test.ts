/** The scrubbed board's decision procedure, against the scenarios the live
 * reducer's own tests encode (tests/test_api_state.py).
 *
 * The rule is duplicated across two languages by necessity - the reducer only
 * holds the present, and the slider asks about the past - so the scenarios
 * that matter are pinned on both sides. Every case here is one the corridor
 * actually produces: a blockage ending, a glare-blind camera disagreeing with
 * a camera that sees the train, a detector going quiet, glare ruining a view,
 * and the two cameras that cannot see their crossing at all.
 */

import { describe, expect, it } from "vitest";
import { STALE_AFTER_MS, stateAt, withTimes, type TimelineObs } from "./scrub";

const T0 = Date.UTC(2026, 7, 12, 5, 45, 0);
const SCORED = "classifier/abc123";
/** The literal the detector stamps on a non-scoring camera's rows
 *  (UNSCORED_VERSION in services/detector/src/detector/runner.py), written out
 *  here rather than derived, so this end of the contract breaks if that one
 *  moves out of the namespace scrub.ts matches on. */
const UNSCORED = "unscored/1";

function at(seconds: number): number {
  return T0 + seconds * 1000;
}

function row(
  seconds: number,
  camera: string,
  state: TimelineObs["state"],
  {
    version = SCORED,
    objectKey = `frames/${camera}/${seconds}.jpg`,
  }: { version?: string; objectKey?: string | null } = {},
): TimelineObs {
  return {
    captured_at: new Date(at(seconds)).toISOString(),
    state,
    object_key: objectKey,
    camera_id: camera,
    detector_version: version,
  };
}

/** The endpoint returns rows ascending by captured_at; so does this. */
function timeline(...rows: TimelineObs[]): TimelineObs[] {
  return [...rows].sort((a, b) => a.captured_at.localeCompare(b.captured_at));
}

it("carries the shipped staleness bound", () => {
  // The one assertion that cares about the number. Its twin is
  // DEFAULT_STALE_AFTER in libs/blockade-core/src/blockade/api/state.py, where
  // the two ceilings the bound answers to are written down; the scrubbed board
  // and the live board answer "did this instant have a witness" separately, so
  // moving one without the other makes them disagree.
  expect(STALE_AFTER_MS).toBe(15 * 60_000);
});

describe("stateAt", () => {
  it("lets a camera's own later CLEAR supersede its earlier BLOCKED", () => {
    // The train clears at 05:46:00 and 678 says so every 30s afterwards. A
    // bound's worth of trailing red is exactly what the live board never shows:
    // its consensus keeps one record per camera, the latest.
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
  });

  it("holds BLOCKED when another camera's fresh CLEAR disagrees", () => {
    // The incident the blocked bias exists for: 681 confirmed a train, and
    // 682 - which cannot resolve the tracks at night - said CLEAR two seconds
    // later. Latest-wins would show CLEAR while a train crossed.
    const rows = timeline(
      row(37, "odot-681", "BLOCKED"),
      row(39, "odot-682", "CLEAR"),
    );

    expect(stateAt(rows, at(40)).state).toBe("BLOCKED");
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
  });

  it("counts a fresh UNKNOWN from a camera that looked as a witness", () => {
    // Glare ruins 678's view. The camera looked and refused to judge, which is
    // not the same as no camera looking: the live board reports UNKNOWN with
    // stale false, so the tile reads "unknown" rather than "no recent signal".
    const rows = timeline(
      row(0, "odot-678", "BLOCKED"),
      row(30, "odot-678", "UNKNOWN"),
    );

    const result = stateAt(rows, at(60));

    expect(result.state).toBe("UNKNOWN");
    expect(result.stale).toBe(false);
  });

  it("does not count a non-scoring camera's policy UNKNOWNs as a witness", () => {
    // 679 watches the Division intersection with the crossing out of frame, so
    // it publishes a zero-inference UNKNOWN every tick without ever looking.
    // A crossing watched only by that camera has no witness at all.
    const rows = timeline(
      row(0, "odot-679", "UNKNOWN", { version: UNSCORED }),
      row(30, "odot-679", "UNKNOWN", { version: UNSCORED }),
    );

    const result = stateAt(rows, at(60));

    expect(result.state).toBe("UNKNOWN");
    expect(result.stale).toBe(true);
    expect(result.frames.get("odot-679")?.captured_at).toBe(new Date(at(30)).toISOString());
  });

  it("keeps a non-scoring camera's frames while its rows never vote", () => {
    // The board keeps showing 679's picture; the crossing's state stays 678's
    // word, which its own UNKNOWN heartbeat must not be able to override.
    const rows = timeline(
      row(0, "odot-678", "BLOCKED"),
      row(10, "odot-679", "UNKNOWN", { version: UNSCORED }),
      row(40, "odot-679", "UNKNOWN", { version: UNSCORED }),
    );

    const result = stateAt(rows, at(60));

    expect(result.state).toBe("BLOCKED");
    expect(result.stale).toBe(false);
    expect(result.frames.get("odot-679")?.captured_at).toBe(new Date(at(40)).toISOString());
    expect(result.frames.get("odot-678")?.captured_at).toBe(new Date(at(0)).toISOString());
  });

  it("still counts a blind camera's pre-policy judgements", () => {
    // 679 scored for months before the policy landed, and those rows carry a
    // real detector's version. They stay authoritative for past instants until
    // the re-score and backfill layer unscored/1 over them.
    const rows = timeline(row(0, "odot-679", "BLOCKED"));

    expect(stateAt(rows, at(30)).state).toBe("BLOCKED");
  });

  it("ignores rows after the scrubbed instant", () => {
    const rows = timeline(
      row(0, "odot-678", "CLEAR"),
      row(60, "odot-678", "BLOCKED"),
    );

    const result = stateAt(rows, at(30));

    expect(result.state).toBe("CLEAR");
    expect(result.frames.get("odot-678")?.captured_at).toBe(new Date(at(0)).toISOString());
  });

  it("carries a camera's last frame forward even when its newer rows have none", () => {
    // A frameless row is a tick the poller recorded without an image; the
    // panel should keep showing the last picture that exists rather than
    // blanking to "No frame yet".
    const rows = timeline(
      row(0, "odot-678", "CLEAR"),
      row(30, "odot-678", "CLEAR", { objectKey: null }),
    );

    const result = stateAt(rows, at(60));

    expect(result.frames.get("odot-678")?.captured_at).toBe(new Date(at(0)).toISOString());
  });

  it("reports UNKNOWN and stale for a crossing with no rows at all", () => {
    expect(stateAt([], at(0))).toEqual({ state: "UNKNOWN", stale: true, frames: new Map() });
  });
});

describe("withTimes", () => {
  /** The scenarios above, replayed through the pre-parsed rows the board
   *  actually holds: LiveBoard parses captured_at once per fetch so a drag
   *  does not re-parse tens of thousands of ISO strings per frame. The
   *  shortcut must be invisible - same state, same staleness, same frames. */
  const scenarios: [string, TimelineObs[], number][] = [
    ["a fresh blockage", timeline(row(0, "odot-678", "BLOCKED")), at(30)],
    [
      "a blockage a scoring camera has since cleared",
      timeline(row(0, "odot-678", "BLOCKED"), row(60, "odot-678", "CLEAR")),
      at(90),
    ],
    [
      "a heartbeat that must not vote",
      timeline(
        row(0, "odot-678", "BLOCKED"),
        row(40, "odot-679", "UNKNOWN", { version: UNSCORED }),
      ),
      at(60),
    ],
    [
      "a detector that went quiet",
      timeline(row(0, "odot-678", "CLEAR")),
      at(13 * 60),
    ],
  ];

  /** The frames map holds the rows themselves, and the pre-parsed ones carry
   *  the extra field by construction; what the panel reads off them is the
   *  frame each camera is showing. */
  const shownFrames = (rows: TimelineObs[], instant: number) =>
    [...stateAt(rows, instant).frames].map(([camera, o]) => [camera, o.object_key, o.captured_at]);

  for (const [name, rows, instant] of scenarios) {
    it(`answers identically for ${name}`, () => {
      const parsed = stateAt(withTimes(rows), instant);
      const raw = stateAt(rows, instant);

      expect(parsed.state).toBe(raw.state);
      expect(parsed.stale).toBe(raw.stale);
      expect(shownFrames(withTimes(rows), instant)).toEqual(shownFrames(rows, instant));
    });
  }

  it("leaves the row's own fields alone", () => {
    const rows = timeline(row(0, "odot-678", "BLOCKED"));
    const [parsed] = withTimes(rows);

    expect(parsed.tMs).toBe(at(0));
    expect({ ...parsed, tMs: undefined }).toEqual({ ...rows[0], tMs: undefined });
    expect(rows[0].tMs).toBeUndefined();
  });
});
