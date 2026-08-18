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

const hourFormats = new Map<string, Intl.DateTimeFormat>();

/** Current hour (0-23) on the corridor's clock.
 *
 * Formatter construction is the expensive part of Intl and this runs every
 * render tick, so instances are cached per zone. `hour12: false` engines
 * report midnight as "24", hence the modulo.
 */
export function corridorHour(localTz: string | null | undefined): number {
  const tz = localTz ?? DEFAULT_LOCAL_TZ;
  let fmt = hourFormats.get(tz);
  if (!fmt) {
    fmt = new Intl.DateTimeFormat(LOCALE, {
      timeZone: tz,
      hour: "numeric",
      hour12: false,
    });
    hourFormats.set(tz, fmt);
  }
  return Number(fmt.format(new Date())) % 24;
}

/** Current day-of-week (0=Sunday) and hour on the corridor's clock. */
export function corridorDayHour(localTz: string | null | undefined): {
  dow: number;
  hour: number;
} {
  const tz = localTz ?? DEFAULT_LOCAL_TZ;
  const parts = new Intl.DateTimeFormat(LOCALE, {
    timeZone: tz,
    weekday: "short",
    hour: "numeric",
    hour12: false,
  }).formatToParts(new Date());
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const weekday = parts.find((p) => p.type === "weekday")?.value ?? "Sun";
  const hour = Number(parts.find((p) => p.type === "hour")?.value ?? "0") % 24;
  return { dow: days.indexOf(weekday), hour };
}
