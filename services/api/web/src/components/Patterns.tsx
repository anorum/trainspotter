/** Patterns: when each crossing usually has a train across it.
 *
 * The centerpiece is a day profile - twenty-four local hours with every week
 * on record pooled into them - because that is the shape the record actually
 * has. The seven-by-twenty-four grid this replaced split the same evidence
 * across 168 cells of about fifty checks each and drew the resulting noise as
 * colour; pooled by hour there are hundreds of checks behind every bar. The
 * day of the week was measured and carries nothing worth drawing - weekdays
 * 12.3% of checks blocked, weekends 11.5% - so the page says that in a line
 * instead of repeating the same profile seven times.
 *
 * The current hour wears the amber ring, tying the live board to its history.
 */

import { useEffect, useState } from "preact/hooks";
import {
  type AnalyticsResponse,
  type CrossingAnalytics,
  bestHours,
  fetchAnalytics,
  hourLabel,
  hourOfDay,
  peakShare,
  percent,
  worstHours,
} from "../lib/analytics";
import { FEATURED, crossingLabel, sessionsUrl } from "../lib/crossings";
import { corridorHour, formatCorridorDayTime, formatShortDate } from "../lib/time";

interface SessionRow {
  crossing_id: string;
  started_at: string;
  duration_seconds: number | null;
  is_open: boolean;
  certified: boolean;
}

export default function Patterns() {
  const [data, setData] = useState<AnalyticsResponse | null>(null);
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    fetchAnalytics().then(setData, () => setFailed(true));
    // The closed sessions carry what the aggregates cannot: the dates behind
    // the record list.
    fetch(sessionsUrl(500))
      .then((r) => (r.ok ? r.json() : { sessions: [] }))
      .then((body) => setSessions(body.sessions), () => {});
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

  const nowHour = corridorHour(data.local_tz);

  return (
    <div class="patterns">
      {FEATURED.map((id) => {
        // Certified rows only: uncertified sightings would skew the medians low.
        const mine = sessions.filter(
          (r) => r.crossing_id === id && !r.is_open && r.certified && r.duration_seconds,
        );
        const a = data.crossings[id];
        return (
          <section key={id} class="crossing">
            <header>
              <h2>{crossingLabel(id)}</h2>
              {a ? (
                <p class="data sumline">
                  blocked {percent(a.blocked_share)} of checks · ~
                  {Math.round(a.minutes_per_day)} min/day · {a.sessions_closed} blockages
                  since {formatShortDate(a.first_observed)}
                </p>
              ) : (
                <p class="data sumline">no observations yet</p>
              )}
            </header>
            {a && (
              <>
                <Lede a={a} />
                <HourProfile a={a} nowHour={nowHour} />
                <div class="pair">
                  <HowLong a={a} />
                  <Longest sessions={mine} tz={data.local_tz} />
                </div>
              </>
            )}
          </section>
        );
      })}
      <p class="note">
        Times are Portland local. A bar is the share of camera checks in that
        hour, across every week on record, that saw a train across the street.
        The day of the week makes no difference worth drawing - weekdays run
        12% of checks blocked, weekends 12%.
      </p>
      <style>{css}</style>
    </div>
  );
}

function Lede({ a }: { a: CrossingAnalytics }) {
  const worst = worstHours(a);
  const best = bestHours(a);
  const peak = peakShare(a);
  if (!worst) return null;
  return (
    <p class="lede">
      Worst <strong>{worst}</strong>, when a camera check finds a train across
      the street about {percent(peak)} of the time.
      {best && (
        <>
          {" "}
          Between <strong>{best}</strong> it is nearly always clear.
        </>
      )}
    </p>
  );
}

/** The day profile: one bar per local hour, every week on record pooled.
 *
 * Height carries the share and opacity carries it a second time, so the
 * ranking survives a monochrome print or a red-blind reader. The readout
 * below replaces per-bar labels - twenty-four numbers would bury the shape
 * the chart exists to show. */
function HourProfile({ a, nowHour }: { a: CrossingAnalytics; nowHour: number }) {
  const day = hourOfDay(a);
  const shares = day.map((s) => (s.scoreable ? s.blocked / s.scoreable : 0));
  const maxShare = Math.max(...shares);
  const peakHour = shares.indexOf(maxShare);
  // A crossing with no train on record yet would divide by zero; the 1% floor
  // scales its flat day instead of blanking the chart.
  const scale = Math.max(maxShare, 0.01);
  const [focus, setFocus] = useState<number | null>(null);
  const shown = focus ?? peakHour;
  const shownSlot = day[shown];

  return (
    <div class="profile">
      <div
        class="bars"
        role="img"
        aria-label={`Blockage by hour of day. Worst at ${hourLabel(peakHour, "prose")}, ${percent(
          shares[peakHour],
        )} of checks blocked.`}
        onPointerLeave={() => setFocus(null)}
      >
        {day.map((slot, h) => (
          <div
            class={`slot ${h === shown ? "on" : ""} ${h === nowHour ? "now" : ""}`}
            onPointerEnter={() => setFocus(h)}
            title={`${hourLabel(h, "prose")}: train seen in ${slot.blocked} of ${
              slot.scoreable
            } checks`}
          >
            <span class="bar" style={`height:${Math.max(1.5, (100 * shares[h]) / scale)}%`} />
          </div>
        ))}
      </div>
      <div class="axis data" aria-hidden="true">
        {[0, 6, 12, 18].map((h) => (
          <span>{hourLabel(h)}</span>
        ))}
        <span>{hourLabel(24)}</span>
      </div>
      <p class="readout data">
        <strong>{hourLabel(shown, "prose")}</strong> - train seen in {shownSlot.blocked} of{" "}
        {shownSlot.scoreable} checks ({percent(shares[shown])})
        {focus === null && <span class="dim"> · the worst hour</span>}
      </p>
    </div>
  );
}

/** How long a blockage runs, as the answers a driver actually wants: the
 * middle case, and the odds of it being quick or long. */
function HowLong({ a }: { a: CrossingAnalytics }) {
  const mins = a.durations_seconds.map((s) => s / 60).sort((x, y) => x - y);
  if (mins.length === 0) {
    return (
      <div class="chart">
        <h3 class="display">How long it lasts</h3>
        <p class="empty">No closed blockages on record yet.</p>
      </div>
    );
  }
  const median = Math.round(mins[Math.floor(mins.length / 2)]);
  const n = mins.length;
  const bands = [
    { label: "under 10 min", tone: "quick", count: mins.filter((m) => m < 10).length },
    { label: "10-30 min", tone: "mid", count: mins.filter((m) => m >= 10 && m <= 30).length },
    { label: "over 30 min", tone: "long", count: mins.filter((m) => m > 30).length },
  ];
  return (
    <div class="chart">
      <h3 class="display">How long it lasts</h3>
      <p class="hero data">
        <strong>{median} min</strong> <span class="dim">typical</span>
      </p>
      <div
        class="split"
        role="img"
        aria-label={`Of ${n} blockages: ${bands
          .map((b) => `${b.count} ${b.label}`)
          .join(", ")}.`}
      >
        {bands.map((b) => (
          <span
            class={`seg ${b.tone}`}
            style={`flex-grow:${Math.max(b.count, 0.001)}`}
            title={`${b.count} of ${n} blockages ${b.label}`}
          />
        ))}
      </div>
      <ul class="legend data">
        {bands.map((b) => (
          <li>
            <span class={`key ${b.tone}`} />
            {b.label} <span class="dim">{Math.round((100 * b.count) / n)}%</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

const css = `
.patterns .crossing { margin: 1.5rem 0 2.5rem; }
.patterns h2 { margin: 0; font-size: 1.5rem; }
.patterns .sumline { color: var(--muted); margin: 0.2rem 0 0.9rem; font-size: 0.85rem; }

.pair { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-top: 1.25rem; }
.chart h3 { margin: 0 0 0.6rem; font-size: 1rem; color: var(--muted); }
.patterns .lede { margin: 0 0 1rem; font-size: 1.05rem; max-width: 60ch; }
.patterns .lede strong { color: var(--signal-red); font-weight: 600; }
.patterns .dim { color: var(--muted); }

/* The day profile: thin bars, a 2px gutter, rounded data-ends anchored to a
   baseline that is the only grid line the chart needs. */
.profile { margin-bottom: 1.5rem; }
.profile .bars { display: flex; gap: 2px; align-items: flex-end; height: 132px;
  border-bottom: 1px solid var(--hairline); }
.profile .slot { flex: 1; height: 100%; display: flex; align-items: flex-end; min-width: 0; }
.profile .bar { display: block; width: 100%; background: var(--signal-red);
  opacity: 0.62; border-radius: 3px 3px 0 0; transition: opacity 120ms ease; }
.profile .slot.on .bar { opacity: 1; }
.profile .slot.now .bar { outline: 2px solid var(--signal-amber); outline-offset: 1px; }
.profile .axis { display: flex; justify-content: space-between; color: var(--muted);
  font-size: 0.7rem; margin-top: 0.3rem; }
.profile .readout { font-size: 0.85rem; margin: 0.5rem 0 0; }

/* How long it lasts: one hero number, then the split that answers "a minute
   or half an hour". */
.chart .hero { font-size: 0.9rem; color: var(--muted); margin: 0 0 0.6rem; }
.chart .hero strong { font-size: 1.6rem; color: var(--crossbuck); font-weight: 600; }
.split { display: flex; gap: 2px; height: 14px; }
.split .seg { border-radius: 3px; min-width: 3px; }
.split .seg.quick, .legend .key.quick { background: color-mix(in srgb, var(--signal-red) 35%, var(--panel)); }
.split .seg.mid, .legend .key.mid { background: color-mix(in srgb, var(--signal-red) 65%, var(--panel)); }
.split .seg.long, .legend .key.long { background: var(--signal-red); }
.legend { list-style: none; padding: 0; margin: 0.6rem 0 0; display: flex;
  flex-wrap: wrap; gap: 0.15rem 1rem; font-size: 0.78rem; }
.legend li { display: flex; align-items: center; gap: 0.35rem; }
.legend .key { width: 9px; height: 9px; border-radius: 2px; flex: none; }

.note { color: var(--muted); font-size: 0.8rem; margin-top: 0.5rem; max-width: 60ch; }
.record { margin: 0.5rem 0 0; padding-left: 1.2rem; display: flex; flex-direction: column; gap: 0.3rem; }
.record strong { color: var(--signal-red); }

@media (max-width: 640px) {
  .pair { grid-template-columns: 1fr; }
}
`;

/** The record book: the five longest blockages, with their dates. */
function Longest({ sessions, tz }: { sessions: SessionRow[]; tz?: string }) {
  const top = [...sessions]
    .sort((a, b) => b.duration_seconds! - a.duration_seconds!)
    .slice(0, 5);
  if (top.length < 3) return null;
  return (
    <div class="chart">
      <h3 class="display">Longest on record</h3>
      <ol class="data record">
        {top.map((r) => (
          <li>
            <strong>{Math.round(r.duration_seconds! / 60)} min</strong>
            {" · "}
            {formatCorridorDayTime(r.started_at, tz)}
          </li>
        ))}
      </ol>
    </div>
  );
}
