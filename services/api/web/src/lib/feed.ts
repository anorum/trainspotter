/** Whose fault stale pictures are, written out for the board.
 *
 * The reducer's verdict arrives in /status's `feed` block; this is the one
 * place its three blames become sentences. The wording carries the verdict:
 * the two upstream notes name ODOT and vouch for our pipeline, capture
 * silence owns the fault outright.
 */

import { formatClockTime } from "./time";

export interface FeedHealth {
  status: "ok" | "upstream_down" | "upstream_stale" | "capture_stale";
  since: string | null;
}

/** The amber note's text, or null when the feed needs no excuse. `since`
 *  means what the reducer measured for each verdict: the last new image
 *  (upstream_stale), the last successful poll (upstream_down), or the last
 *  poll heartbeat of any kind (capture_stale). */
export function feedNote(feed: FeedHealth | undefined): string | null {
  if (!feed) return null;
  const at = feed.since ? ` since ${formatClockTime(feed.since)}` : "";
  switch (feed.status) {
    case "upstream_stale":
      return `ODOT has served no new pictures${at} - the pipeline is healthy and waiting.`;
    case "upstream_down":
      return `ODOT's camera server is not answering${at ? `; no successful poll${at}` : ""} - the pipeline is healthy and waiting.`;
    case "capture_stale":
      return `No camera has been polled${at} - this one is on us.`;
    default:
      return null;
  }
}
