/** The analytics contract and the little arithmetic the views share.
 *
 * Server buckets are corridor-local (America/Los_Angeles) with the Postgres
 * dow convention: index = dow * 24 + hour, dow 0 = Sunday.
 */

export interface HourSlot {
  blocked: number;
  scoreable: number;
}

export interface CrossingAnalytics {
  first_observed: string;
  last_observed: string;
  coverage_days: number;
  hour_of_week: HourSlot[];
  durations_seconds: number[];
  daily_blocked_minutes: Record<string, number>;
  blocked_share: number | null;
  sessions_closed: number;
  minutes_per_day: number;
}

export interface AnalyticsResponse {
  available: boolean;
  local_tz?: string;
  crossings: Record<string, CrossingAnalytics>;
}

let cached: Promise<AnalyticsResponse> | null = null;

/** One fetch per page load; the board panel and any future caller share it. */
export function fetchAnalytics(): Promise<AnalyticsResponse> {
  cached ??= fetch("/api/v1/analytics")
    .then((r) => {
      if (!r.ok) throw new Error(`analytics fetch failed: ${r.status}`);
      return r.json() as Promise<AnalyticsResponse>;
    })
    .catch((err) => {
      cached = null;
      throw err;
    });
  return cached;
}

/** Collapse the week grid to a 24-slot day profile. */
export function hourOfDay(a: CrossingAnalytics): HourSlot[] {
  const day: HourSlot[] = Array.from({ length: 24 }, () => ({ blocked: 0, scoreable: 0 }));
  a.hour_of_week.forEach((slot, i) => {
    day[i % 24].blocked += slot.blocked;
    day[i % 24].scoreable += slot.scoreable;
  });
  return day;
}

/** Blocked share of the scoreable checks in a slot. Internal: callers want
 * heat() for the tint or worstHours() for the prose, not the raw ratio. */
function share(slot: HourSlot): number {
  return slot.scoreable ? slot.blocked / slot.scoreable : 0;
}

/** Cell background for an hour slot, or undefined for untinted.
 *
 * One scale for the board's hour strip and the patterns timetable - they must
 * read as the same instrument. The floor (18%) keeps a rare-but-real hour
 * visible; the x2 saturates at a 50% blocked share, which is as bad as these
 * crossings get.
 */
export function heat(slot: HourSlot): string | undefined {
  const s = share(slot);
  if (s <= 0) return undefined;
  const pct = Math.round(18 + 82 * Math.min(1, s * 2));
  return `background: color-mix(in srgb, var(--signal-red) ${pct}%, var(--panel))`;
}

/** 12-hour clock label: compact "6A" for axes, "6 AM" for prose. Hours wrap,
 * so 24 is the midnight that closes an axis. */
export function hourLabel(h: number, style: "axis" | "prose" = "axis"): string {
  const x = ((h % 24) + 24) % 24;
  const clock = x % 12 === 0 ? 12 : x % 12;
  const meridiem = x < 12 ? "AM" : "PM";
  if (style === "prose") return `${clock} ${meridiem}`;
  return `${clock}${meridiem[0]}`;
}

/** "worst around 6-8 AM": the contiguous run of hours at or near the peak. */
export function worstHours(a: CrossingAnalytics): string | null {
  const day = hourOfDay(a);
  const shares = day.map((s) => (s.scoreable >= 4 ? share(s) : 0));
  const peak = Math.max(...shares);
  if (peak <= 0) return null;
  let start = shares.indexOf(peak);
  let end = start;
  while (shares[(start + 23) % 24] >= peak * 0.6 && end - start < 5) start--;
  while (shares[(end + 1) % 24] >= peak * 0.6 && end - start < 5) end++;
  const fmt = (h: number) => hourLabel(h, "prose");
  return start === end ? `around ${fmt(start)}` : `${fmt(start)}-${fmt(end + 1)}`;
}

export function percent(x: number | null): string {
  if (x === null) return "-";
  return x < 0.01 && x > 0 ? "<1%" : `${Math.round(x * 100)}%`;
}

/** What a driver staring at a blocked crossing wants to know: how much longer.
 *
 * The estimate conditions on how long the blockage has already run - the
 * record's durations that have been outlasted stop being evidence. The median
 * of what remains among comparable trains is the honest middle answer, and a
 * blockage that has outlasted every recorded train gets told exactly that.
 * Small-n humility: the sample size rides along in the line itself.
 */
export function waitOutlook(
  durationsSeconds: readonly number[],
  elapsedSeconds: number,
): string | null {
  if (durationsSeconds.length === 0) return null;
  const longer = durationsSeconds.filter((d) => d > elapsedSeconds);
  if (longer.length === 0) {
    const record = Math.round(Math.max(...durationsSeconds) / 60);
    return `already the longest on record (previous record ${record} min)`;
  }
  const remaining = longer
    .map((d) => (d - elapsedSeconds) / 60)
    .sort((a, b) => a - b);
  const median = remaining[Math.floor(remaining.length / 2)];
  // Round up to 5-minute steps: mock precision would overstate the data.
  const est = Math.max(5, Math.ceil(median / 5) * 5);
  return `trains like this usually clear within ~${est} min (${longer.length} recorded)`;
}
