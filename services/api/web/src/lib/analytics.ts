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

export function share(slot: HourSlot): number {
  return slot.scoreable ? slot.blocked / slot.scoreable : 0;
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
  const fmt = (h: number) => {
    const x = ((h % 24) + 24) % 24;
    if (x === 0) return "12 AM";
    if (x === 12) return "12 PM";
    return x < 12 ? `${x} AM` : `${x - 12} PM`;
  };
  return start === end ? `around ${fmt(start)}` : `${fmt(start)}-${fmt(end + 1)}`;
}

export function percent(x: number | null): string {
  if (x === null) return "-";
  return x < 0.01 && x > 0 ? "<1%" : `${Math.round(x * 100)}%`;
}
