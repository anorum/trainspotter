/** Patterns: when each crossing usually has a train across it.
 *
 * The centerpiece is a timetable grid per crossing - seven days by
 * twenty-four local hours, the railroad's own way of writing time - where
 * each cell's red is the share of camera checks that hour that saw a train.
 * The current hour wears the amber ring, tying the live board to its
 * history. Below it, how long blockages last and how the days have run.
 */

import { useEffect, useState } from "preact/hooks";
import {
  type AnalyticsResponse,
  type CrossingAnalytics,
  fetchAnalytics,
  heat,
  hourLabel,
  percent,
  worstHours,
} from "../lib/analytics";
import { FEATURED, crossingLabel } from "../lib/crossings";
import { corridorDayHour, formatShortDate } from "../lib/time";

// Postgres dow 0 = Sunday; the sheet reads Monday-first like a work week.
const DOW_ORDER = [1, 2, 3, 4, 5, 6, 0];
const DOW_LABEL = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"];
const DURATION_BINS: [number, string][] = [
  [10, "5-10m"],
  [20, "10-20m"],
  [30, "20-30m"],
  [45, "30-45m"],
  [60, "45-60m"],
  [Infinity, "60m+"],
];

export default function Patterns() {
  const [data, setData] = useState<AnalyticsResponse | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    fetchAnalytics().then(setData, () => setFailed(true));
  }, []);

  if (failed) {
    return <p class="empty">The history store is not answering; patterns will be back.</p>;
  }
  if (!data) return <p class="loading">Working through the record...</p>;
  if (!data.available) {
    return (
      <p class="empty">
        Patterns need the history store, which is not connected. The live
        board still works from the stream.
      </p>
    );
  }

  const nowLocal = corridorDayHour(data.local_tz);

  return (
    <div class="patterns">
      {FEATURED.map((id) => {
        const a = data.crossings[id];
        const worst = a && worstHours(a);
        return (
          <section key={id} class="crossing">
            <header>
              <h2>{crossingLabel(id)}</h2>
              {a ? (
                <p class="data sumline">
                  blocked {percent(a.blocked_share)} of checks · ~
                  {Math.round(a.minutes_per_day)} min/day · {a.sessions_closed} blockages
                  since {formatShortDate(a.first_observed)}
                  {worst && <> · worst {worst}</>}
                </p>
              ) : (
                <p class="data sumline">no observations yet</p>
              )}
            </header>
            {a && (
              <>
                <Timetable a={a} now={nowLocal} />
                <div class="pair">
                  <Durations a={a} />
                  <Daily a={a} />
                </div>
              </>
            )}
          </section>
        );
      })}
      <p class="note">
        Times are Portland local. A cell's red is the share of camera checks
        that hour, across all weeks on record, that saw a train across the
        street.
      </p>
      <style>{css}</style>
    </div>
  );
}

function Timetable({ a, now }: { a: CrossingAnalytics; now: { dow: number; hour: number } }) {
  return (
    <div class="timetable" role="img" aria-label="Hour-of-week blockage grid">
      <div class="corner" />
      {Array.from({ length: 24 }, (_, h) => (
        <div class="hour-label data">{h % 6 === 0 ? hourLabel(h) : ""}</div>
      ))}
      {DOW_ORDER.map((dow) => (
        <>
          <div class="dow-label display">{DOW_LABEL[dow]}</div>
          {Array.from({ length: 24 }, (_, h) => {
            const slot = a.hour_of_week[dow * 24 + h];
            const current = dow === now.dow && h === now.hour;
            return (
              <div
                class={`cell ${current ? "current" : ""} ${slot.scoreable === 0 ? "nodata" : ""}`}
                style={heat(slot)}
                title={`${DOW_LABEL[dow]} ${hourLabel(h)}: ${
                  slot.scoreable
                    ? `train seen in ${slot.blocked} of ${slot.scoreable} checks`
                    : "no checks yet"
                }`}
              />
            );
          })}
        </>
      ))}
    </div>
  );
}

function Durations({ a }: { a: CrossingAnalytics }) {
  const counts = DURATION_BINS.map(() => 0);
  for (const s of a.durations_seconds) {
    const m = s / 60;
    counts[DURATION_BINS.findIndex(([top]) => m < top)]++;
  }
  const max = Math.max(1, ...counts);
  return (
    <div class="chart">
      <h3 class="display">How long they last</h3>
      {DURATION_BINS.map(([, label], i) => (
        <div class="hbar-row">
          <span class="data hbar-label">{label}</span>
          <span class="hbar-lane">
            <span class="hbar" style={`width:${(100 * counts[i]) / max}%`} />
          </span>
          <span class="data hbar-count">{counts[i] || ""}</span>
        </div>
      ))}
    </div>
  );
}

function Daily({ a }: { a: CrossingAnalytics }) {
  const days = Object.entries(a.daily_blocked_minutes).slice(-14);
  const max = Math.max(30, ...days.map(([, m]) => m));
  return (
    <div class="chart">
      <h3 class="display">Minutes blocked, by day</h3>
      {days.length === 0 ? (
        <p class="empty">No closed blockages on record yet.</p>
      ) : (
        <div class="days">
          {days.map(([day, minutes]) => (
            <div class="day-col" title={`${day}: ${minutes} min`}>
              <span class="vbar" style={`height:${Math.max(2, (100 * minutes) / max)}%`} />
              <span class="data day-tick">{Number(day.slice(8))}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

const css = `
.patterns .crossing { margin: 1.5rem 0 2.5rem; }
.patterns h2 { margin: 0; font-size: 1.5rem; }
.patterns .sumline { color: var(--muted); margin: 0.2rem 0 0.9rem; font-size: 0.85rem; }

.timetable { display: grid; grid-template-columns: 3.2rem repeat(24, 1fr); gap: 2px; }
.timetable .hour-label { color: var(--muted); font-size: 0.65rem; align-self: end; }
.timetable .dow-label { color: var(--muted); font-size: 0.8rem; letter-spacing: 0.08em; align-self: center; }
.timetable .cell { aspect-ratio: 5 / 4; background: var(--panel); border-radius: 2px; min-width: 0; }
.timetable .cell.nodata { background: repeating-linear-gradient(45deg, var(--panel), var(--panel) 2px, var(--ink) 2px, var(--ink) 4px); }
.timetable .cell.current { outline: 2px solid var(--signal-amber); outline-offset: -1px; }

.pair { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-top: 1.25rem; }
.chart h3 { margin: 0 0 0.6rem; font-size: 1rem; color: var(--muted); }
.hbar-row { display: grid; grid-template-columns: 5.5ch 1fr 3ch; align-items: center; gap: 0.6rem; margin: 0.25rem 0; }
.hbar-label { color: var(--muted); font-size: 0.75rem; text-align: right; }
.hbar-lane { height: 10px; background: var(--panel); border-radius: 3px; overflow: hidden; }
.hbar { display: block; height: 100%; background: var(--signal-red); opacity: 0.75; }
.hbar-count { color: var(--muted); font-size: 0.75rem; }

.days { display: flex; gap: 4px; align-items: flex-end; height: 90px; }
.day-col { flex: 1; display: flex; flex-direction: column; justify-content: flex-end; align-items: center; gap: 3px; height: 100%; }
.vbar { display: block; width: 100%; max-width: 22px; background: var(--signal-red); opacity: 0.75; border-radius: 2px 2px 0 0; }
.day-tick { color: var(--muted); font-size: 0.65rem; }

.note { color: var(--muted); font-size: 0.8rem; margin-top: 0.5rem; max-width: 60ch; }

@media (max-width: 640px) {
  .pair { grid-template-columns: 1fr; }
  .timetable { grid-template-columns: 2.4rem repeat(24, 1fr); gap: 1px; }
}
`;
