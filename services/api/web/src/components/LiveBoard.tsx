/** The board: the Brooklyn Sub through inner SE Portland, on the real map.
 *
 * One island owns everything live: the SSE connection, the selected crossing,
 * and the time scrubber. The core view is a Leaflet map on dark tiles with a
 * grade-crossing flasher at each featured crossing's true coordinates - the
 * geometry comes from the roster, not hand-drawn art, so re-featuring a
 * crossing puts its flasher where the crossing actually is. It shows the
 * crossings in FEATURED, not every crossing the API reports.
 */

import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import {
  type AnalyticsResponse,
  fetchAnalytics,
  heat,
  hourLabel,
  hourOfDay,
  percent,
  waitOutlook,
  worstHours,
} from "../lib/analytics";
import {
  COLORS,
  crossingLabel,
  type CrossingId,
  FEATURED,
  featuredOnly,
  GEOMETRY,
  mapPageUrl,
  RAIL_NAME,
  SOLO,
  type State,
} from "../lib/crossings";
import CrossingMap from "./CrossingMap";
import { type FeedHealth, feedNote } from "../lib/feed";
import { blockedSpans, stateAt, withTimes, type TimelineObs } from "../lib/scrub";
import { corridorHour, formatMinute, formatTime } from "../lib/time";

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
  feed?: FeedHealth;
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

/** How often the lanes re-pull their window unprompted. Half of ODOT's
 *  slowest camera cadence (3-10 minutes), so a span's end is never more
 *  than one cadence late. */
const REFRESH_MS = 5 * 60_000;

export default function LiveBoard() {
  const [status, setStatus] = useState<Status | null>(null);
  // Whether the board itself could not be reached, as opposed to a load
  // still in flight - the two look identical on an empty page and must
  // not read the same.
  const [unreachable, setUnreachable] = useState(false);
  // A solo board is detail-first: its one crossing starts open.
  const [selected, setSelected] = useState<string | null>(SOLO ? FEATURED[0] : null);
  // Whether the arrival auto-open has already fired; the effect below the
  // board memo, which is where it can first see data, explains the rule.
  const autoOpened = useRef(false);
  const [scrubT, setScrubT] = useState<number | null>(null); // null = live
  const [windowHours, setWindowHours] = useState(24);
  const [timelines, setTimelines] = useState<Record<string, TimelineObs[]>>({});
  const [analytics, setAnalytics] = useState<AnalyticsResponse | null>(null);
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
  // The mount effect's timer and SSE listener close over the first render,
  // so they read the live window through a ref the button handler keeps in
  // step with setWindowHours.
  const windowHoursRef = useRef(24);
  // The panel's habit line loads the first time any crossing is selected.
  useEffect(() => {
    if (selected && !analytics) fetchAnalytics().then(setAnalytics, () => {});
  }, [selected]);

  useEffect(() => {
    // A first load that fails must say so: "Contacting the board..." forever
    // reads as a slow page rather than a board that is not answering, and the
    // difference is the whole point of a status site. Both paths clear the
    // flag on success, so a board that comes back heals the page.
    fetch("/api/v1/status")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error("status"))))
      .then(
        (body) => {
          setStatus(body);
          setUnreachable(false);
        },
        () => setUnreachable(true),
      );
    // The lanes need the window's timeline before any scrub: blockages
    // findable by eye are what make the history worth a drag at all.
    void loadTimelines(24);
    // In a `let` because the connection is rebuilt below: EventSource
    // reconnects on its own only after network-level failures. An HTTP-level
    // failure - a non-200 or non-event-stream reply, like an edge-served 502
    // page while the origin is down - closes it permanently per the WHATWG
    // spec, and a CLOSED source never retries.
    let source: EventSource;
    const connect = () => {
      const es = new EventSource("/api/v1/events");
      // Two triggers keep the loaded window live rather than a mount-time
      // snapshot: a status event means consensus moved, so new observations
      // exist to fetch; the timer catches span-ending CLEAR frames that
      // change no crossing's state and so push no event - without it an
      // ongoing span would freeze at blockedSpans' freshness cap while the
      // SSE-fed board stayed red.
      es.addEventListener("status", (e) => {
        setStatus(JSON.parse((e as MessageEvent).data));
        setUnreachable(false);
        void refreshTimelines();
      });
      // The flag only decides what an empty page says while the source
      // retries - or, when CLOSED, until the refresh timer rebuilds it.
      es.addEventListener("error", () => {
        if (es.readyState === EventSource.CLOSED) setUnreachable(true);
      });
      source = es;
    };
    connect();
    const timer = setInterval(() => setTick((t) => t + 1), 1000);
    // A tab whose initial load failed has nothing applied to refresh, so
    // the timer retries the full load instead - through the normal
    // retryAfter cooldown - and an untouched tab heals. Only the timer:
    // retrying on every status event would hammer a store that is already
    // sick. The same tick revives a permanently CLOSED EventSource; the
    // events endpoint pushes a snapshot on connect, so one successful
    // reconnect restores both the board and the live feed, and "this page
    // keeps trying" stays true.
    const refresh = setInterval(() => {
      if (source.readyState === EventSource.CLOSED) connect();
      if (appliedHours.current === 0) void loadTimelines(windowHoursRef.current);
      else void refreshTimelines();
    }, REFRESH_MS);
    return () => {
      source.close();
      clearInterval(timer);
      clearInterval(refresh);
    };
  }, []);

  const fetchWindow = (hours: number) =>
    Promise.all(
      FEATURED.map(async (id) => {
        const r = await fetch(`/api/v1/timeline?crossing_id=${id}&hours=${hours}`);
        if (!r.ok) throw new Error(`timeline ${r.status}`);
        return [id, withTimes((await r.json()).observations)] as const;
      }),
    );

  // Re-pull the window already applied, which loadTimelines' guard would
  // call a no-op. Skips while a wider load is in flight - that load will
  // land fresher data itself, and superseding it here would strand
  // loadedHours above what the store ever delivered.
  const refreshTimelines = async () => {
    const hours = appliedHours.current;
    if (hours === 0 || hours !== loadedHours.current) return;
    if (Date.now() < retryAfter.current) return;
    const generation = ++loadGeneration.current;
    try {
      const entries = await fetchWindow(hours);
      if (generation === loadGeneration.current) {
        setTimelines(Object.fromEntries(entries));
        retryAfter.current = 0;
        // The note may be up because a wider window's load failed; this
        // refresh proved nothing about that window, so only a refresh that
        // covers the one on screen may take the note down.
        if (hours >= windowHoursRef.current) setTimelineFailed(false);
      }
    } catch {
      // The data on screen still covers the window, so no failure note;
      // the cooldown keeps a sick store from being re-polled by every
      // status event.
      if (generation === loadGeneration.current) {
        retryAfter.current = Date.now() + RETRY_COOLDOWN_MS;
      }
    }
  };

  // The window's full load: eager on mount so the lanes render before any
  // scrub, and again whenever the window widens past what has been fetched.
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
      const entries = await fetchWindow(hours);
      if (generation === loadGeneration.current) {
        setTimelines(Object.fromEntries(entries));
        appliedHours.current = hours;
        retryAfter.current = 0;
        setTimelineFailed(false);
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
  // The lanes are the scrubber's own rule swept over the loaded window, so a
  // red span under the slider and a red board at that instant can never
  // disagree - one-frame trains included.
  const laneSpans = useMemo(() => {
    const by: Record<string, [number, number][]> = {};
    for (const id of FEATURED) by[id] = blockedSpans(timelines[id] ?? []);
    return by;
  }, [timelines]);
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

  // A corridor board still owes the reader an answer on arrival: the first
  // paint that has crossings opens the one that most needs attention - a live
  // blockage, else the first featured. Once only: a reader who closes
  // the panel has chosen the overview, and data updates must not reopen it
  // under them.
  useEffect(() => {
    if (SOLO || autoOpened.current || selected !== null || !board) return;
    // In the board's declared order, not the reply's: the fallback must be
    // the first row the reader sees.
    const rows = FEATURED.flatMap((id) =>
      featuredOnly(board.crossings).filter((c) => c.crossing_id === id),
    );
    if (!rows.length) return;
    autoOpened.current = true;
    const urgent = rows.find((c) => c.state === "BLOCKED" && !c.stale);
    setSelected((urgent ?? rows[0]).crossing_id);
  }, [board]);

  if (!board) {
    return unreachable ? (
      <p class="empty board-down">
        The board is not answering. The crossing is still there - we just
        cannot see it right now. This page keeps trying.
      </p>
    ) : (
      <p class="loading">Contacting the board...</p>
    );
  }

  // The one place the reply is scoped and ordered for presentation:
  // featuredOnly drops the withheld crossings (keeping the reply's order, as
  // the sheet needs), and the board additionally renders in FEATURED order so
  // the rows line up with the lanes under the scrubber, which are drawn from
  // FEATURED directly.
  const featured = featuredOnly(board.crossings);
  const shown = FEATURED.flatMap((id) => featured.filter((c) => c.crossing_id === id));
  const chosen = shown.find((c) => c.crossing_id === selected) ?? null;
  // Whose fault stale pictures are. Read from the live status, not the
  // scrubbed board: the verdict is a statement about the feed now, and it
  // holds while the slider is anywhere.
  const note = feedNote(status?.feed);
  const now = Date.now();
  const windowStart = now - windowHours * 3600 * 1000;
  const lanes = FEATURED.map((id) =>
    laneSpans[id]
      .map(([a, b]) => [a, Math.min(b, now)] as [number, number])
      .filter(([a, b]) => b > windowStart && a < now),
  );

  return (
    <div class="board">
      {/* The core view is the real corridor: flashers at true coordinates,
          so each re-featured crossing appears where it actually is. While
          scrubbing, the flashers show the aspect at the scrubbed instant. */}
      <div
        class="board-map"
        role="region"
        aria-label={SOLO ? "Map of the crossing" : "Map of the crossings"}
      >
        <CrossingMap
          states={Object.fromEntries(
            shown.map((c) => [c.crossing_id, c.stale ? "UNKNOWN" : c.state]),
          )}
          onSelect={SOLO ? undefined : setSelected}
        />
        <span class="railchip data">{RAIL_NAME}</span>
      </div>

      {note && <p class="feednote">{note}</p>}

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
                windowHoursRef.current = hours;
                setWindowHours(hours);
                if (scrubbing) {
                  const newStart = Date.now() - hours * 3600 * 1000;
                  if (scrubT !== null && scrubT < newStart) setScrubT(newStart);
                }
                // The lanes redraw for the wider window whether or not a
                // scrub is in progress, so the data must widen with it.
                await loadTimelines(hours);
              }}
            >
              {label}
            </button>
          ))}
        </div>
        <span class="data when">
          {scrubbing ? formatMinute(new Date(scrubT!).toISOString()) : "now"}
        </span>
      </div>

      {/* Without this the board would render a history-store outage as empty
          lanes and crossings with no recent signal - a corridor with nothing
          on it and dead detectors, rather than a store that cannot answer. */}
      {timelineFailed && (
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
            <a
              class="data maplink"
              href={mapPageUrl(GEOMETRY[chosen.crossing_id as CrossingId])}
              target="_blank"
              rel="noopener"
            >
              Google Maps ↗
            </a>
            {/* Nothing to go back to on a solo board: closing would leave the
                page empty and the only way back is reselecting the one row. */}
            {!SOLO && (
              <button class="close" onClick={() => setSelected(null)} aria-label="Close">
                ✕
              </button>
            )}
          </header>
          {/* The ticker runs only while the state itself is BLOCKED. An open
              session can outlive the blockage by design (it closes one session
              gap plus watermark lag after the last BLOCKED reading; the gap
              lives in blockade/sessions.py), and a red "blocked for 28m"
              beside a green CLEAR is nonsense presentation of sensible data.
              No scrub guard here: the board memo already nulls open_session
              and latest_observation while scrubbing. */}
          {chosen.state === "BLOCKED" && chosen.open_session && (
            <>
              <p class="data ticker">
                Blocked for {duration(chosen.open_session.started_at)}
              </p>
              {(() => {
                // The record answers the question the ticker raises.
                const a = analytics?.crossings[chosen.crossing_id];
                const line =
                  a &&
                  waitOutlook(
                    a.durations_seconds,
                    (Date.now() - new Date(chosen.open_session.started_at).getTime()) / 1000,
                  );
                return line ? <p class="data outlook">{line}</p> : null;
              })()}
            </>
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


.scrub { display: flex; flex-wrap: wrap; align-items: center; gap: 0.75rem; padding: 0.5rem 0 1rem; }
.scrub .track { flex: 1; min-width: 0; }
.scrub input { width: 100%; display: block; accent-color: var(--signal-amber); }
.scrub button { background: var(--panel); color: var(--crossbuck); border: 1px solid var(--hairline); border-radius: 4px; padding: 0.3rem 0.8rem; cursor: pointer; font-family: var(--display); letter-spacing: 0.05em; }
/* The button and timestamp both relabel the instant a drag leaves "live",
   and they share the slider's flex row: unreserved, the relabel resizes the
   track under the held pointer and corrupts the drag's pointer-x mapping.
   Each reserves at least its widest text: 20ch is the longest en-US
   minute-precision datetime ("12/30/2026, 10:38 PM"), and the timestamp
   pins that locale rather than the browser's so the reserve is exact
   everywhere. 20ch holds 20 characters only because .data is a monospace
   family: every glyph there carries the '0' advance that ch measures, so
   the reserve is void if --data stops being fixed-pitch. The button's
   7.5rem is instead a measurement of its wider label, "Back to live", in
   --display: nothing derives it, so re-measure it if either label changes. */
.scrub > button { min-width: 7.5rem; }
.scrub button.live { color: var(--signal-green); }
.scrub .when { color: var(--muted); min-width: 20ch; text-align: right; font-size: 0.85rem; }
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
button.row:not(.chosen):hover { background: color-mix(in srgb, var(--panel) 60%, var(--ink)); }
.row.chosen { background: var(--ink); box-shadow: inset 3px 0 0 var(--signal-amber); }
.row .dot { width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }
.row .name { flex: 1; font-size: 1.15rem; }
.row .data { color: var(--muted); font-size: 0.85rem; }

.detail { margin-top: 1rem; background: var(--panel); border: 1px solid var(--hairline); border-radius: 6px; padding: 1rem 1.25rem; }
.detail header { display: flex; align-items: baseline; gap: 1rem; }
.detail h2 { margin: 0; font-size: 1.5rem; flex: 1; }
.detail .state-word { font-size: 1rem; }
.detail .close { background: none; border: 0; color: var(--muted); cursor: pointer; font-size: 1rem; }
.detail .ticker { color: var(--signal-red); font-size: 1.1rem; margin: 0.5rem 0 0; }
.detail .outlook { color: var(--muted); margin: 0.15rem 0 0; }
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
.board-map { position: relative; margin: 0.5rem 0 0.75rem; }
.feednote { margin: 0.5rem 0 0; padding: 0.4rem 0.8rem; border: 1px solid var(--signal-amber); border-radius: 4px; color: var(--signal-amber); background: color-mix(in srgb, var(--signal-amber) 8%, transparent); }
.board-map .crossing-map { width: 100%; height: clamp(300px, 45vh, 460px); border: 1px solid var(--hairline); border-radius: 6px; background: var(--panel); position: relative; z-index: 0; }
.railchip { position: absolute; top: 10px; right: 10px; z-index: 500; background: rgba(0,0,0,0.55); color: var(--muted); font-size: 0.7rem; letter-spacing: 0.3em; padding: 0.25rem 0.6rem 0.25rem 0.8rem; border: 1px solid var(--hairline); border-radius: 4px; pointer-events: none; }
.maplabel { position: absolute; left: 42px; top: 0; white-space: nowrap; color: var(--crossbuck); font-family: var(--display); font-size: 17px; letter-spacing: 0.05em; text-transform: uppercase; text-shadow: 0 1px 4px rgba(0,0,0,0.9); }
.maplink { color: var(--signal-amber); text-decoration: none; margin-left: auto; }
.maplink:hover, .maplink:focus-visible { text-decoration: underline; }
/* The flasher: a two-lamp signal housing, the form of the hardware at the
   real crossing. BLOCKED alternates the lamps at flasher cadence; CLEAR and
   UNKNOWN hold both steady in their aspect. */
.flasher { display: flex; gap: 4px; padding: 3px 4px; background: var(--ink); border: 1px solid var(--hairline); border-radius: 9px; box-shadow: 0 1px 4px rgba(0,0,0,0.6); }
.flasher i { width: 10px; height: 10px; border-radius: 50%; background: var(--muted); }
.flasher.CLEAR i { background: var(--signal-green); }
.flasher.UNKNOWN i { background: var(--signal-amber); }
.flasher.BLOCKED i { background: var(--signal-red); box-shadow: 0 0 6px var(--signal-red); animation: flash 1s steps(1) infinite; }
.flasher.BLOCKED i + i { animation-delay: 0.5s; }
@keyframes flash { 0%, 49% { opacity: 1; } 50%, 100% { opacity: 0.15; box-shadow: none; } }
@media (prefers-reduced-motion: reduce) { .flasher.BLOCKED i { animation: none; } }
/* Leaflet chrome, re-dressed in the board's tokens. */
.board-map .leaflet-control-zoom a { background: var(--panel); color: var(--crossbuck); border-color: var(--hairline); }
.board-map .leaflet-control-attribution { background: rgba(0,0,0,0.55); color: var(--muted); font-size: 0.6rem; }
.board-map .leaflet-control-attribution a { color: var(--muted); }
`;
