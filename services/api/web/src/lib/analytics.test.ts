import { describe, expect, it } from "vitest";
import { waitOutlook } from "./analytics";

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
