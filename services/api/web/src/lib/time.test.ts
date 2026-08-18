/** How the site writes a moment, and where "now" falls on the corridor's clock.
 *
 * Two contracts are pinned here. The corridor clock is the load-bearing one:
 * the hour strip on the board and the timetable on the patterns sheet both
 * ring the current cell, and the buckets they index into are Postgres
 * `extract(dow|hour ... AT TIME ZONE local_tz)` - Sunday-first, corridor
 * local. A "now" derived from the reader's own clock rings the wrong cell for
 * anyone outside Portland, and rings the wrong cell for everyone in the hours
 * where the corridor's date and UTC's date disagree.
 *
 * The second is the pinned locale: every surface writes en-US, because the
 * scrub row reserves exactly the widest en-US datetime and a board that
 * followed the browser while the sheet pinned would show two conventions on
 * one screen. Asserted as convention rather than an exact string, so the
 * suite does not depend on the runner's own timezone.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  corridorDayHour,
  corridorHour,
  formatDayHeading,
  formatMinute,
  formatShortDate,
  formatShortTime,
} from "./time";

const PORTLAND = "America/Los_Angeles";

afterEach(() => {
  vi.useRealTimers();
});

function at(iso: string): void {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(iso));
}

describe("corridorHour", () => {
  it("reports the corridor's hour, not the reader's or UTC's", () => {
    at("2026-08-18T02:41:00Z"); // 7:41 PM Monday in Portland, already Tuesday in UTC
    expect(corridorHour(PORTLAND)).toBe(19);
  });

  it("reports midnight as 0", () => {
    // hour12:false engines write midnight as "24"; the strip has no 24th cell.
    at("2026-08-18T07:05:00Z"); // 12:05 AM PDT
    expect(corridorHour(PORTLAND)).toBe(0);
  });

  it("follows the corridor across daylight saving", () => {
    at("2026-01-15T20:30:00Z"); // PST, UTC-8
    expect(corridorHour(PORTLAND)).toBe(12);
    vi.setSystemTime(new Date("2026-07-15T20:30:00Z")); // PDT, UTC-7
    expect(corridorHour(PORTLAND)).toBe(13);
  });

  it("falls back to the corridor when the server ships no zone", () => {
    at("2026-08-18T02:41:00Z");
    expect(corridorHour(null)).toBe(corridorHour(PORTLAND));
    expect(corridorHour(undefined)).toBe(corridorHour(PORTLAND));
  });

  it("keeps answering after a zone has been asked for once", () => {
    // The formatter is cached per zone; a cached instance must not freeze the
    // hour it was built at.
    at("2026-08-18T02:41:00Z");
    expect(corridorHour(PORTLAND)).toBe(19);
    vi.setSystemTime(new Date("2026-08-18T05:41:00Z"));
    expect(corridorHour(PORTLAND)).toBe(22);
  });
});

describe("corridorDayHour", () => {
  it("indexes the timetable the way Postgres bucketed it: Sunday = 0, corridor local", () => {
    at("2026-08-18T02:41:00Z"); // Monday evening in Portland, Tuesday in UTC
    expect(corridorDayHour(PORTLAND)).toEqual({ dow: 1, hour: 19 });
  });

  it("rolls the day over on the corridor's midnight, not UTC's", () => {
    at("2026-08-18T06:59:00Z"); // 11:59 PM Monday PDT
    expect(corridorDayHour(PORTLAND)).toEqual({ dow: 1, hour: 23 });
    vi.setSystemTime(new Date("2026-08-18T07:01:00Z")); // 12:01 AM Tuesday PDT
    expect(corridorDayHour(PORTLAND)).toEqual({ dow: 2, hour: 0 });
  });

  it("puts Sunday at 0, matching the grid's dow numbering", () => {
    at("2026-08-16T19:00:00Z"); // Sunday noon PDT
    expect(corridorDayHour(PORTLAND)).toEqual({ dow: 0, hour: 12 });
  });

  it("falls back to the corridor when the server ships no zone", () => {
    at("2026-08-18T02:41:00Z");
    expect(corridorDayHour(null)).toEqual(corridorDayHour(PORTLAND));
  });
});

describe("the pinned locale", () => {
  const T = "2026-08-17T19:15:00Z";

  it("writes the sheet's start times as a 2-digit en-US clock", () => {
    expect(formatShortTime(T)).toMatch(/^\d{2}:\d{2} (AM|PM)$/);
  });

  it("writes day headings weekday-first, month by name", () => {
    expect(formatDayHeading(T)).toMatch(
      /^(Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday), (January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}$/,
    );
  });

  it("writes the since-date month-first and abbreviated", () => {
    expect(formatShortDate(T)).toMatch(/^[A-Z][a-z]{2} \d{1,2}$/);
  });
});

describe("formatMinute", () => {
  it("writes the scrub label to the minute, with no seconds", () => {
    // The slider steps in whole minutes, so a seconds field would be
    // meaningless precision. Asserted as convention, not an exact string,
    // so the suite does not depend on the runner's timezone.
    expect(formatMinute("2026-08-17T19:15:42Z")).toMatch(
      /^\d{1,2}\/\d{1,2}\/\d{4}, \d{1,2}:\d{2} (AM|PM)$/,
    );
  });

  it("never outgrows the scrub row's 20ch reserve", () => {
    // LiveBoard's scrub CSS reserves min-width: 20ch for this label; an
    // instant that rendered wider would resize the track mid-drag. A late
    // December day walked hour by hour reaches the widest shape in any
    // runner timezone: 2-digit month, 2-digit day, 2-digit hour.
    for (let h = 0; h < 24; h++) {
      const iso = `2026-12-30T${String(h).padStart(2, "0")}:38:59Z`;
      expect(formatMinute(iso).length).toBeLessThanOrEqual(20);
    }
  });
});
