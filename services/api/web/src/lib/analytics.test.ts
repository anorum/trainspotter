import { describe, expect, it } from "vitest";
import {
  type CrossingAnalytics,
  bestHours,
  peakShare,
  waitOutlook,
  worstHours,
} from "./analytics";

/** The outlook line under the blocked ticker: conditioned on elapsed time,
 *  honest about sample size, and never more precise than 5 minutes. */
describe("waitOutlook", () => {
  const durations = [10, 12, 15, 20, 25, 40, 90].map((m) => m * 60);

  it("says nothing with no record", () => {
    expect(waitOutlook([], 60)).toBeNull();
  });

  it("estimates from the trains this one has not yet outlasted", () => {
    // 5 minutes in: all seven trains remain comparable; the median of the
    // remaining minutes [5,7,10,15,20,35,85] is 15.
    expect(waitOutlook(durations, 5 * 60)).toBe(
      "trains like this usually clear within ~15 min (7 recorded)",
    );
  });

  it("re-conditions as the blockage outlasts the record's short trains", () => {
    // 30 minutes in, only the 40- and 90-minute trains remain comparable.
    expect(waitOutlook(durations, 30 * 60)).toBe(
      "trains like this usually clear within ~60 min (2 recorded)",
    );
  });

  it("admits when the blockage has outlasted every recorded train", () => {
    expect(waitOutlook(durations, 100 * 60)).toBe(
      "already the longest on record (previous record 90 min)",
    );
  });

  it("never promises finer than five minutes", () => {
    expect(waitOutlook([6 * 60], 5 * 60)).toBe(
      "trains like this usually clear within ~5 min (1 recorded)",
    );
  });
});

/** The day-profile sentence. Both halves are derived rather than eyeballed,
 *  so both have to agree with the grid they came from. */
describe("day profile prose", () => {
  /** A week of identical days: `byHour` gives the blocked count for each local
   *  hour, out of ten checks, repeated across all seven days. */
  function grid(byHour: Record<number, number>): CrossingAnalytics {
    const hour_of_week = Array.from({ length: 168 }, (_, i) => ({
      blocked: byHour[i % 24] ?? 0,
      scoreable: 10,
    }));
    return {
      first_observed: "2026-08-01T00:00:00Z",
      last_observed: "2026-08-23T00:00:00Z",
      coverage_days: 22,
      hour_of_week,
      durations_seconds: [],
      daily_blocked_minutes: {},
      blocked_share: 0.1,
      sessions_closed: 0,
      minutes_per_day: 0,
    };
  }

  // A realistic day: a busy small-hours run, an ordinary daytime baseline,
  // and an evening the crossing is reliably free.
  const baseline = Object.fromEntries(Array.from({ length: 24 }, (_, h) => [h, 2]));
  const heavyNights = grid({ ...baseline, 1: 5, 2: 4, 3: 4, 16: 0, 17: 0, 18: 0, 19: 0 });

  it("names the peak run, not just the peak hour", () => {
    expect(worstHours(heavyNights)).toBe("1 AM-4 AM");
  });

  it("names the longest quiet run", () => {
    expect(bestHours(heavyNights)).toBe("4 PM-8 PM");
  });

  it("reports the peak share the sentence quotes", () => {
    expect(peakShare(heavyNights)).toBeCloseTo(0.5, 5);
  });

  it("finds a quiet run that wraps past midnight", () => {
    // Quiet from 10 PM through 2 AM; the scan must not stop at the day break.
    const wrapped = grid({ ...baseline, 12: 6, 22: 0, 23: 0, 0: 0, 1: 0, 2: 0 });
    expect(bestHours(wrapped)).toBe("10 PM-3 AM");
  });

  it("says nothing when there is no record to speak from", () => {
    const empty = grid({});
    expect(worstHours(empty)).toBeNull();
    expect(bestHours(empty)).toBeNull();
  });

  it("declines to name a quiet window that swallows the day", () => {
    // One busy hour and nothing else: "nearly always clear" is the whole
    // story, and naming twenty-three hours of it would not help anyone.
    expect(bestHours(grid({ 12: 6 }))).toBeNull();
  });
});
