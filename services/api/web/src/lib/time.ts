/** The one place the site decides how a moment is written.
 *
 * Locale is pinned to en-US everywhere: the scrub row reserves exactly the
 * widest en-US datetime (20ch, see LiveBoard's scrub CSS), and a board that
 * pinned one label while the sheet followed the browser would show two date
 * conventions on one screen.
 *
 * The corridor's clock is a separate decision from the locale: analytics
 * buckets are Portland-local (the server ships `local_tz`), and both the hour
 * strip and the patterns timetable draw their amber "now" ring from it - two
 * surfaces that must agree about which cell is the current one.
 */

export const LOCALE = "en-US";

/** Server-shipped corridor timezone, with the same fallback everywhere. */
export const DEFAULT_LOCAL_TZ = "America/Los_Angeles";

export const formatTime = (iso: string): string =>
  new Date(iso).toLocaleTimeString(LOCALE);

/** "8/17/2026, 4:42 AM" - the scrub label. The slider steps in whole
 *  minutes, so displayed seconds are meaningless precision (the step grid is
 *  anchored at render time and carries its arbitrary sub-minute offset). */
export const formatMinute = (iso: string): string =>
  new Date(iso).toLocaleString(LOCALE, {
    year: "numeric", month: "numeric", day: "numeric",
    hour: "numeric", minute: "2-digit",
  });

/** "8:14 PM" - the feed note's verdict time, written the way it would be
 *  said. The sheet's columns keep the 2-digit hour below for alignment; a
 *  sentence has nothing to line up. */
export const formatClockTime = (iso: string): string =>
  new Date(iso).toLocaleTimeString(LOCALE, { hour: "numeric", minute: "2-digit" });

/** "8:05 PM" - the sheet's compact start/frame times. */
export const formatShortTime = (iso: string): string =>
  new Date(iso).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });

/** "Aug 17" - the analytics since-date. */
export const formatShortDate = (iso: string): string =>
  new Date(iso).toLocaleDateString(LOCALE, { month: "short", day: "numeric" });

/** "Sunday, August 17" - the sheet's day headings. */
export const formatDayHeading = (iso: string): string =>
  new Date(iso).toLocaleDateString(LOCALE, {
    weekday: "long",
    month: "long",
    day: "numeric",
  });

/** "Monday, August 17, 11:30 PM" on the corridor's clock - the record list.
 *  The sheet's day-heading and short-time conventions, composed, but pinned
 *  to the corridor zone so the page's "Times are Portland local" note holds. */
export function formatCorridorDayTime(
  iso: string,
  localTz: string | null | undefined,
): string {
  const timeZone = localTz ?? DEFAULT_LOCAL_TZ;
  const d = new Date(iso);
  const day = d.toLocaleDateString(LOCALE, {
    timeZone,
    weekday: "long",
    month: "long",
    day: "numeric",
  });
  const time = d.toLocaleTimeString(LOCALE, {
    timeZone,
    hour: "2-digit",
    minute: "2-digit",
  });
  return `${day}, ${time}`;
}

const hourFormats = new Map<string, Intl.DateTimeFormat>();

/** Formatter construction is the expensive part of Intl and the corridor hour
 *  runs every render tick, so instances are cached per zone. */
function cachedHourFormat(localTz: string | null | undefined): Intl.DateTimeFormat {
  const tz = localTz ?? DEFAULT_LOCAL_TZ;
  let fmt = hourFormats.get(tz);
  if (!fmt) {
    fmt = new Intl.DateTimeFormat(LOCALE, { timeZone: tz, hour: "numeric", hour12: false });
    hourFormats.set(tz, fmt);
  }
  return fmt;
}

/** Current hour (0-23) on the corridor's clock. `hour12: false` engines
 *  report midnight as "24", hence the modulo. */
export function corridorHour(localTz: string | null | undefined): number {
  return corridorHourOf(new Date().toISOString(), localTz);
}

/** Hour (0-23) of a given instant on the corridor's clock. */
export function corridorHourOf(iso: string, localTz: string | null | undefined): number {
  return Number(cachedHourFormat(localTz).format(new Date(iso))) % 24;
}

