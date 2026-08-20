/** The amber verdict note: whose fault stale pictures are, in words.
 *
 * The contract pinned here is blame. Each reducer verdict must name its
 * culprit - the upstream verdicts say "ODOT" and vouch for the pipeline,
 * capture silence says "this one is on us" - and a healthy feed must show
 * nothing at all. Times are asserted as convention rather than exact
 * strings, so the suite does not depend on the runner's timezone.
 */

import { describe, expect, it } from "vitest";
import { feedNote } from "./feed";

const SINCE = "2026-08-19T03:14:00Z"; // 8:14 PM in Portland
const CLOCK = /\d{1,2}:\d{2} (AM|PM)/;

describe("feedNote", () => {
  it("shows nothing while the feed is healthy", () => {
    expect(feedNote({ status: "ok", since: null })).toBeNull();
    expect(feedNote(undefined)).toBeNull();
  });

  it("blames ODOT's frozen cameras and vouches for the pipeline", () => {
    // Last night's 304-forever case: healthy polls, nothing new past the
    // staleness bound. `since` is the last genuinely new image.
    const note = feedNote({ status: "upstream_stale", since: SINCE })!;
    expect(note).toMatch(
      new RegExp(
        `^ODOT has served no new pictures since ${CLOCK.source} - the pipeline is healthy and waiting\\.$`,
      ),
    );
  });

  it("blames ODOT's server when every camera is on an error streak", () => {
    // `since` is the last successful poll of any camera.
    const note = feedNote({ status: "upstream_down", since: SINCE })!;
    expect(note).toMatch(/^ODOT's camera server is not answering; no successful poll since /);
    expect(note).toMatch(/- the pipeline is healthy and waiting\.$/);
  });

  it("owns the fault when the poll heartbeat itself has gone quiet", () => {
    const note = feedNote({ status: "capture_stale", since: SINCE })!;
    expect(note).toMatch(new RegExp(`^No camera has been polled since ${CLOCK.source} `));
    expect(note).toMatch(/- this one is on us\.$/);
  });

  it("still delivers each verdict when the books hold no since-time", () => {
    // upstream_down with no successful poll ever recorded ships since=null;
    // the note must not render a dangling "since".
    expect(feedNote({ status: "upstream_down", since: null })).toBe(
      "ODOT's camera server is not answering - the pipeline is healthy and waiting.",
    );
    expect(feedNote({ status: "capture_stale", since: null })).toBe(
      "No camera has been polled - this one is on us.",
    );
    expect(feedNote({ status: "upstream_stale", since: null })).toBe(
      "ODOT has served no new pictures - the pipeline is healthy and waiting.",
    );
  });
});
