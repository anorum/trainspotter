/** The board: a schematic of the Brooklyn Sub through inner SE Portland.
 *
 * One island owns everything live: the SSE connection, the selected crossing,
 * and the time scrubber. The map is a hand-drawn SVG of the real geometry -
 * the rail line running NW-SE, the cross streets, a signal head per crossing -
 * because a handful of fixed points need a dispatcher's board, not a tile map.
 * It draws the crossings in FEATURED, not every crossing the API reports.
 */

import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import {
  type AnalyticsResponse,
  fetchAnalytics,
  heat,
  hourLabel,
  hourOfDay,
  percent,
  worstHours,
} from "../lib/analytics";
import {
  closeUpOn,
  COLORS,
  crossingLabel,
  FEATURED,
  featuredOnly,
  FULL_CORRIDOR_VIEWBOX,
  GEOMETRY,
  RAIL,
  sessionsUrl,
  SOLO,
  type State,
} from "../lib/crossings";
import { stateAt, withTimes, type TimelineObs } from "../lib/scrub";
import { corridorHour, formatDateTime, formatTime } from "../lib/time";

interface CameraInfo {
  camera_id: string;
  name: string;
  captured_at: string | null;
  object_key: string | null;
}

interface Crossing {
  crossing_id: string;
  state: State;
  stale: boolean;
  since: string | null;
  open_session: { started_at: string } | null;
  latest_observation: { confidence: number; reason: string; captured_at: string } | null;
  cameras: CameraInfo[];
}

interface Status {
  generated_at: string;
  crossings: Crossing[];
}

interface SessionRow {
  crossing_id: string;
  started_at: string;
  ended_at: string | null;
}

/** Scrub windows. Hours, labelled the way a dispatcher would say them. */
const WINDOWS: [number, string][] = [
  [24, "24H"],
  [72, "3D"],
  [168, "7D"],
  [720, "30D"],
];

/** How long a failed history load silences the scrubber's retries. */
const RETRY_COOLDOWN_MS = 5_000;

export default function LiveBoard() {
  const [status, setStatus] = useState<Status | null>(null);
  // A solo board is detail-first: its one crossing starts open.
  const [selected, setSelected] = useState<string | null>(SOLO ? FEATURED[0] : null);
  const [scrubT, setScrubT] = useState<number | null>(null); // null = live
  const [windowHours, setWindowHours] = useState(24);
  const [timelines, setTimelines] = useState<Record<string, TimelineObs[]>>({});
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [analytics, setAnalytics] = useState<AnalyticsResponse | null>(null);
  // Two surfaces read the history store, and each owns its own flag: a
  // recovered timeline says nothing about lanes that were never refetched.
  // They share one note, so the board never stacks two outage lines.
  const [lanesFailed, setLanesFailed] = useState(false);
  const [timelineFailed, setTimelineFailed] = useState(false);
  // The value is never read; each tick just re-renders the live durations.
  const [, setTick] = useState(0);
  // Refs, not state: the guard must be visible to concurrent calls
  // *synchronously*, before any await - the scrubber fires loadTimelines on
  // every input event, and a state-based guard let every one of them race
  // through and refetch every featured crossing. The generation counter keeps an
  // overlapping narrower load (drag, then widen immediately) from landing
  // late and clobbering the wider data. appliedHours is the window whose
  // observations are actually in `timelines`, which is what a failed load
  // must fall back to.
  const loadedHours = useRef(0);
  const appliedHours = useRef(0);
  const loadGeneration = useRef(0);
  // A failed load rolls the guard back, so without this every further input
  // event would start another round of timeline fetches - one per featured
  // crossing - against an already sick store.
  const retryAfter = useRef(0);
  // The lanes can fail while a timeline load is mid-flight, so the recovery
  // check below the awaits must read live state, not the render's closure.
  const lanesFailedRef = useRef(false);
  const markLanes = (failed: boolean) => {
    lanesFailedRef.current = failed;
    setLanesFailed(failed);
  };

  // The panel's habit line loads the first time any crossing is selected.
  useEffect(() => {
    if (selected && !analytics) fetchAnalytics().then(setAnalytics, () => {});
  }, [selected]);

  // The lanes under the scrubber: blockages findable by eye before scrubbing,
  // so the history is worth a drag in the first place. Empty lanes are a
  // claim about the corridor, so a store that cannot answer says so instead -
  // and stays retryable, because this is the one surface with no live feed.
  const loadLanes = () =>
    fetch(sessionsUrl(500))
      .then((r) => {
        if (!r.ok) throw new Error(`sessions ${r.status}`);
        return r.json();
      })
      .then(
        (body) => {
          setSessions(body.sessions);
          markLanes(false);
        },
        () => markLanes(true),
      );

  useEffect(() => {
    // If the first status fetch fails the page stays on "Contacting the
    // board..." and the EventSource below retries into the same state.
    fetch("/api/v1/status")
      .then((r) => (r.ok ? r.json() : null))
      .then((body) => body && setStatus(body), () => {});
    void loadLanes();
    const source = new EventSource("/api/v1/events");
    source.addEventListener("status", (e) => setStatus(JSON.parse((e as MessageEvent).data)));
    const timer = setInterval(() => setTick((t) => t + 1), 1000);
    return () => {
      source.close();
      clearInterval(timer);
    };
  }, []);

  // Scrub data loads lazily the first time the slider moves off "now", and
  // reloads when the window widens past what has been fetched.
  const loadTimelines = async (hours: number) => {
    if (loadedHours.current >= hours) {
      // Gate on committed data, not the guard: a wider load still in flight
      // owns loadedHours and has proved nothing about the store yet.
      if (appliedHours.current >= hours) setTimelineFailed(false);
      return;
    }
    if (Date.now() < retryAfter.current) {
      // Still unavailable, and the scrubber now spans a window the data does
      // not cover - say so rather than render the gap as dead detectors.
      setTimelineFailed(true);
      return;
    }
    loadedHours.current = hours;
    const generation = ++loadGeneration.current;
    try {
      const entries = await Promise.all(
        FEATURED.map(async (id) => {
          const r = await fetch(`/api/v1/timeline?crossing_id=${id}&hours=${hours}`);
          if (!r.ok) throw new Error(`timeline ${r.status}`);
          return [id, withTimes((await r.json()).observations)] as const;
        }),
      );
      if (generation === loadGeneration.current) {
        setTimelines(Object.fromEntries(entries));
        appliedHours.current = hours;
        retryAfter.current = 0;
        setTimelineFailed(false);
        // The store is evidently back; the lanes have no other way to learn it.
        if (lanesFailedRef.current) void loadLanes();
      }
    } catch {
      // Fall back to the window actually on screen so a later scrub retries;
      // a superseding load owns the guard now and must not be rolled back.
      if (generation === loadGeneration.current) {
        loadedHours.current = appliedHours.current;
        retryAfter.current = Date.now() + RETRY_COOLDOWN_MS;
        setTimelineFailed(true);
      }
    }
  };

  const scrubbing = scrubT !== null;
  // Sessions parsed once per fetch, not per 1-second tick: the lanes only
  // need each row's epoch bounds, and 500 ISO parses per render is real work
  // on a phone. null end = still open, substituted with `now` at render.
  const laneSpans = useMemo(() => {
    const by: Record<string, [number, number | null][]> = Object.fromEntries(
      FEATURED.map((id) => [id, []]),
    );
    for (const s of sessions) {
      by[s.crossing_id]?.push([
        new Date(s.started_at).getTime(),
        s.ended_at ? new Date(s.ended_at).getTime() : null,
      ]);
    }
    return by;
  }, [sessions]);
  const board = useMemo(() => {
    if (!status) return null;
    if (!scrubbing) return status;
    // Time-travel view: each crossing shows its state at the scrubbed instant,
    // by the same rules the live reducer applies to the same question. They
    // live in lib/scrub.ts, where they are tested. The open session and the
    // latest observation are dropped because both are statements about now,
    // and `since` with them: the live board's is the instant consensus
    // changed state, which a walk over one crossing's rows cannot recover, and
    // a "since" that tracks the slider rather than the train would make a
    // twenty-minute blockage unreadable as one. The tile renders the bare
    // state instead.
    const at = new Date(scrubT!);
    const atMs = at.getTime();
    return {
      generated_at: at.toISOString(),
      crossings: featuredOnly(status.crossings).map((c) => {
        const { state, stale, frames } = stateAt(timelines[c.crossing_id] ?? [], atMs);
        return {
          ...c,
          state,
          stale,
          since: null,
          open_session: null,
          latest_observation: null,
          cameras: c.cameras.map((cam) => {
            const f = frames.get(cam.camera_id);
            return f
              ? { ...cam, object_key: f.object_key, captured_at: f.captured_at }
              : { ...cam, object_key: null, captured_at: null };
          }),
        };
      }),
    };
  }, [status, scrubT, timelines]);

  if (!board) return <p class="loading">Contacting the board...</p>;

  // The one place the reply is scoped and ordered for presentation:
  // featuredOnly drops the withheld crossings (keeping the reply's order, as
  // the sheet needs), and the board additionally renders in FEATURED order so
  // the rows line up with the lanes under the scrubber, which are drawn from
  // FEATURED directly.
  const featured = featuredOnly(board.crossings);
  const shown = FEATURED.flatMap((id) => featured.filter((c) => c.crossing_id === id));
  const chosen = shown.find((c) => c.crossing_id === selected) ?? null;
  const now = Date.now();
  const windowStart = now - windowHours * 3600 * 1000;
  const lanes = FEATURED.map((id) =>
    laneSpans[id]
      .map(([a, b]) => [a, b ?? now] as [number, number])
      .filter(([a, b]) => b > windowStart && a < now),
  );

  const viewBox = SOLO ? closeUpOn(GEOMETRY[FEATURED[0]]) : FULL_CORRIDOR_VIEWBOX;

  return (
    <div class="board">
      <svg
        viewBox={viewBox}
        role="img"
        aria-label={SOLO ? "Map of the crossing" : "Map of the crossings"}
      >
        {/* the rail line: double stroke reads as track */}
        <line {...RAIL} stroke="var(--hairline)" stroke-width="10" />
        <line {...RAIL} stroke="var(--ink)" stroke-width="6" />
        <line {...RAIL} stroke="var(--muted)" stroke-width="2" stroke-dasharray="1 14" />
        {FEATURED.map((id) => {
          const g = GEOMETRY[id];
          const crossing = shown.find((c) => c.crossing_id === id);
          const state: State = crossing?.state ?? "UNKNOWN";
          return (
            <g key={id}>
              <line
                x1={g.x - 190} y1={g.y} x2={g.x + 190} y2={g.y}
                stroke="var(--hairline)" stroke-width="3"
              />
              <text x={g.x - 185} y={g.y - 10} class="street">{g.street}</text>
              <g
                role={SOLO ? "img" : "button"}
                tabIndex={SOLO ? undefined : 0}
                aria-label={`${g.label}: ${state}`}
                onClick={SOLO ? undefined : () => setSelected(id)}
                onKeyDown={SOLO ? undefined : (e) => e.key === "Enter" && setSelected(id)}
                style={SOLO ? undefined : "cursor: pointer"}
              >
                <circle
                  cx={g.x} cy={g.y} r="22"
                  fill="var(--panel)"
                  stroke={selected === id ? "var(--crossbuck)" : "var(--hairline)"}
                  stroke-width="2"
                />
                <circle
                  cx={g.x} cy={g.y} r="12"
                  fill={COLORS[state]}
                  class={state === "BLOCKED" ? "pulse" : ""}
                />
                <text x={g.x + 32} y={g.y + 36} class="label">{g.label}</text>
              </g>
            </g>
          );
        })}
      </svg>

      <div class="scrub">
        <button
          class={scrubbing ? "" : "live"}
          onClick={() => setScrubT(null)}
          aria-pressed={!scrubbing}
        >
          {scrubbing ? "Back to live" : "● Live"}
        </button>
        <div class="track">
          <input
            type="range"
            min={windowStart}
            max={now}
            step={60_000}
            value={scrubT ?? now}
            aria-label={`Time travel through the last ${windowHours} hours`}
            onInput={(e) => {
              // Read the value before anything async: awaiting first lets a
              // state update re-render this controlled input back to "now",
              // and the late read then always returns the reset value.
              const v = Number((e.target as HTMLInputElement).value);
              void loadTimelines(windowHours);
              // Snap-to-live only inside half a step. At a full step the
              // first ArrowLeft lands exactly on the threshold and snaps
              // straight back, locking keyboard users out of the past.
              setScrubT(v >= now - 30_000 ? null : v);
            }}
          />
          {/* One lane per crossing in corridor order; red is a recorded
              blockage at that instant. The track is itself a timeline. */}
          <div class="lanes" aria-hidden="true">
            {lanes.map((intervals) => (
              <div class="lane">
                {intervals.map(([a, b]) => (
                  <span
                    class="blocked-seg"
                    style={`left:${(100 * (Math.max(a, windowStart) - windowStart)) / (now - windowStart)}%;width:${Math.max(0.4, (100 * (Math.min(b, now) - Math.max(a, windowStart))) / (now - windowStart))}%`}
                  />
                ))}
              </div>
            ))}
          </div>
        </div>
        <div class="windows" role="group" aria-label="Scrub window">
          {WINDOWS.map(([hours, label]) => (
            <button
              class={windowHours === hours ? "win on" : "win"}
              aria-pressed={windowHours === hours}
              onClick={async () => {
                setWindowHours(hours);
                if (scrubbing) {
                  const newStart = Date.now() - hours * 3600 * 1000;
                  if (scrubT !== null && scrubT < newStart) setScrubT(newStart);
                  await loadTimelines(hours);
                }
              }}
            >
              {label}
            </button>
          ))}
        </div>
        <span class="data when">
          {scrubbing ? formatDateTime(new Date(scrubT!).toISOString()) : "now"}
        </span>
      </div>

      {/* Without this the board would render a history-store outage as empty
          lanes and crossings with no recent signal - a corridor with nothing
          on it and dead detectors, rather than a store that cannot answer. */}
      {(lanesFailed || timelineFailed) && (
        <p class="empty scrub-note">
          The history store is not answering; the past will be back.
        </p>
      )}

      <div class="crossings-list">
        {shown.map((c) => {
          // On a solo board the row is a summary, not a chooser: a button
          // whose only action is selecting the already-selected crossing
          // would take focus and do nothing, and a selected treatment on the
          // only row is a stuck highlight rather than information.
          const Row = SOLO ? "div" : "button";
          return (
            <Row
              class={`row ${!SOLO && selected === c.crossing_id ? "chosen" : ""}`}
              style={SOLO ? "cursor: default" : undefined}
              onClick={SOLO ? undefined : () => setSelected(c.crossing_id)}
            >
              <span class="dot" style={`background:${COLORS[c.state]}`} />
              <span class="display name">{crossingLabel(c.crossing_id)}</span>
              <span class="data">{stateLine(c)}</span>
            </Row>
          );
        })}
      </div>

      {chosen && (
        <section class="detail">
          <header>
            <h2>{crossingLabel(chosen.crossing_id)}</h2>
            {/* The live region is the state word alone, not the panel. The
                panel holds the blockage ticker, whose text is recomputed every
                second, and a solo board has the panel open from first paint -
                announcing the whole thing would read "Blocked for 12m 36s"
                once a second forever. The word changes when the answer does. */}
            <span
              class="data state-word"
              style={`color:${COLORS[chosen.state]}`}
              aria-live="polite"
            >
              {chosen.stale ? "UNKNOWN (stale)" : chosen.state}
            </span>
            {/* Nothing to go back to on a solo board: closing would leave the
                page empty and the only way back is reselecting the one row. */}
            {!SOLO && (
              <button class="close" onClick={() => setSelected(null)} aria-label="Close">
                ✕
              </button>
            )}
          </header>
          {/* The ticker runs only while the state itself is BLOCKED. An open
              session can outlive the blockage by design (it closes after ten
              quiet minutes plus watermark lag), and a red "blocked for 28m"
              beside a green CLEAR is nonsense presentation of sensible data.
              No scrub guard here: the board memo already nulls open_session
              and latest_observation while scrubbing. */}
          {chosen.state === "BLOCKED" && chosen.open_session && (
            <p class="data ticker">
              Blocked for {duration(chosen.open_session.started_at)}
            </p>
          )}
          {chosen.latest_observation && (
            <p class="reason">
              {chosen.latest_observation.reason} (confidence{" "}
              {Math.round(chosen.latest_observation.confidence * 100)}%)
            </p>
          )}
          <Habits crossingId={chosen.crossing_id} analytics={analytics} />
          <div class="cameras">
            {chosen.cameras.map((cam) => (
              <figure key={cam.camera_id}>
                {cam.object_key ? (
                  <img
                    src={`/api/v1/frames/${cam.object_key}`}
                    alt={`${cam.name} camera view`}
                    loading="lazy"
                  />
                ) : (
                  <div class="noframe">No frame yet</div>
                )}
                <figcaption>
                  {cam.name}
                  {cam.captured_at && (
                    <span class="data"> · {formatTime(cam.captured_at)}</span>
                  )}
                </figcaption>
              </figure>
            ))}
          </div>
        </section>
      )}

      <style>{css}</style>
    </div>
  );
}

/** The crossing's habit at a glance: the summary line and one day in 24 cells. */
function Habits({
  crossingId,
  analytics,
}: {
  crossingId: string;
  analytics: AnalyticsResponse | null;
}) {
  const a = analytics?.available ? analytics.crossings[crossingId] : undefined;
  // Derivations change once per analytics fetch; the panel re-renders every
  // tick for the live durations, so they are memoized rather than re-walked.
  // Above every early return: the hook must run on every render, whatever the
  // props say.
  const habits = useMemo(
    () => (a && a.blocked_share !== null ? { worst: worstHours(a), day: hourOfDay(a) } : null),
    [a],
  );
  if (!analytics?.available || !a || !habits) return null;
  const { worst, day } = habits;
  const localHour = corridorHour(analytics.local_tz);
  return (
    <div class="habits">
      <p class="data habit-line">
        blocked {percent(a.blocked_share)} of checks · ~
        {Math.round(a.minutes_per_day)} min/day
        {worst && <> · worst {worst}</>}
        {" · "}
        <a href="/patterns/">patterns</a>
      </p>
      <div class="hourstrip" aria-label="Typical blockage share by hour">
        {day.map((slot, h) => (
          <span
            class={`hs-cell ${h === localHour ? "hs-now" : ""}`}
            style={heat(slot)}
            title={`${h}:00 - ${
              slot.scoreable
                ? `train in ${slot.blocked} of ${slot.scoreable} checks`
                : "no checks yet"
            }`}
          />
        ))}
      </div>
      <div class="hourticks data" aria-hidden="true">
        {[0, 6, 12, 18, 24].map((h) => (
          <span>{hourLabel(h)}</span>
        ))}
      </div>
    </div>
  );
}

function stateLine(c: Crossing): string {
  if (c.stale) return "no recent signal";
  if (c.state === "BLOCKED" && c.open_session) {
    return `blocked ${duration(c.open_session.started_at)}`;
  }
  if (c.since) {
    return `${c.state.toLowerCase()} since ${formatTime(c.since)}`;
  }
  return c.state.toLowerCase();
}

function duration(startIso: string): string {
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(startIso).getTime()) / 1000));
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m ${String(s).padStart(2, "0")}s`;
}

const css = `
.board svg { width: 100%; height: auto; display: block; }
.board .street { fill: var(--muted); font-family: var(--data); font-size: 11px; letter-spacing: 0.1em; }
.board .label { fill: var(--crossbuck); font-family: var(--display); font-size: 20px; letter-spacing: 0.04em; text-transform: uppercase; }

.scrub { display: flex; flex-wrap: wrap; align-items: center; gap: 0.75rem; padding: 0.5rem 0 1rem; }
.scrub .track { flex: 1; min-width: 0; }
.scrub input { width: 100%; display: block; accent-color: var(--signal-amber); }
.scrub button { background: var(--panel); color: var(--crossbuck); border: 1px solid var(--hairline); border-radius: 4px; padding: 0.3rem 0.8rem; cursor: pointer; font-family: var(--display); letter-spacing: 0.05em; }
/* The button and timestamp both relabel the instant a drag leaves "live",
   and they share the slider's flex row: unreserved, the relabel resizes the
   track under the held pointer and corrupts the drag's pointer-x mapping.
   Each reserves at least its widest text: 23ch is the longest en-US
   toLocaleString datetime ("10/30/2026, 12:38:58 AM"), and the timestamp
   pins that locale rather than the browser's so the reserve is exact
   everywhere. 23ch holds 23 characters only because .data is a monospace
   family: every glyph there carries the '0' advance that ch measures, so
   the reserve is void if --data stops being fixed-pitch. The button's
   7.5rem is instead a measurement of its wider label, "Back to live", in
   --display: nothing derives it, so re-measure it if either label changes. */
.scrub > button { min-width: 7.5rem; }
.scrub button.live { color: var(--signal-green); }
.scrub .when { color: var(--muted); min-width: 23ch; text-align: right; font-size: 0.85rem; }
/* Under ~768px the single row cannot hold Live + track + windows + timestamp
   without squeezing the slider down to a stub; drop the track onto its own
   row, where sibling relabels cannot resize it. Both flanking labels keep
   their reserves here too, so the header wraps onto the same lines whether
   the timestamp reads "now" or a full datetime, and no relabel moves any
   row - including the track's - under a held pointer. */
@media (max-width: 768px) {
  .scrub .track { flex-basis: 100%; order: 10; }
  .scrub .when { margin-left: auto; }
}
.scrub-note { margin: -0.5rem 0 1rem; font-size: 0.85rem; }
.lanes { display: grid; gap: 2px; padding: 2px 8px 0; }
.lane { position: relative; height: 3px; background: var(--panel); border-radius: 2px; }
.blocked-seg { position: absolute; top: 0; height: 100%; background: var(--signal-red); border-radius: 2px; }
.windows { display: flex; gap: 2px; }
.win { padding: 0.3rem 0.5rem; font-size: 0.8rem; color: var(--muted); }
.win.on { color: var(--crossbuck); border-color: var(--signal-amber); }

.crossings-list { display: grid; gap: 1px; background: var(--hairline); border: 1px solid var(--hairline); border-radius: 6px; overflow: hidden; }
.row { display: flex; align-items: center; gap: 0.75rem; padding: 0.65rem 1rem; background: var(--panel); border: 0; color: var(--crossbuck); cursor: pointer; text-align: left; font-size: 1rem; }
.row.chosen { background: var(--ink); }
.row .dot { width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }
.row .name { flex: 1; font-size: 1.15rem; }
.row .data { color: var(--muted); font-size: 0.85rem; }

.detail { margin-top: 1rem; background: var(--panel); border: 1px solid var(--hairline); border-radius: 6px; padding: 1rem 1.25rem; }
.detail header { display: flex; align-items: baseline; gap: 1rem; }
.detail h2 { margin: 0; font-size: 1.5rem; flex: 1; }
.detail .state-word { font-size: 1rem; }
.detail .close { background: none; border: 0; color: var(--muted); cursor: pointer; font-size: 1rem; }
.detail .ticker { color: var(--signal-red); font-size: 1.1rem; margin: 0.5rem 0 0; }
.detail .reason { color: var(--muted); margin: 0.35rem 0 0; font-size: 0.9rem; }
.habits { margin-top: 0.75rem; }
.habit-line { color: var(--muted); font-size: 0.85rem; margin: 0 0 0.4rem; }
.habit-line a { color: var(--signal-amber); }
.hourstrip { display: grid; grid-template-columns: repeat(24, 1fr); gap: 2px; }
.hs-cell { height: 10px; background: var(--panel); border-radius: 2px; }
.hs-now { outline: 2px solid var(--signal-amber); outline-offset: -1px; }
.hourticks { display: flex; justify-content: space-between; color: var(--muted); font-size: 0.65rem; margin-top: 2px; }
.cameras { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1rem; margin-top: 1rem; }
.cameras figure { margin: 0; }
.cameras img { width: 100%; border-radius: 4px; border: 1px solid var(--hairline); }
.cameras .noframe { aspect-ratio: 4/3; display: grid; place-items: center; color: var(--muted); border: 1px dashed var(--hairline); border-radius: 4px; }
.cameras figcaption { color: var(--muted); font-size: 0.85rem; margin-top: 0.35rem; }
`;
