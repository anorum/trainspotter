/** The board: a schematic of the Brooklyn Sub through inner SE Portland.
 *
 * One island owns everything live: the SSE connection, the selected crossing,
 * and the time scrubber. The map is a hand-drawn SVG of the real geometry -
 * the rail line running NW-SE, the cross streets, a signal head per crossing -
 * because three fixed points need a dispatcher's board, not a tile map.
 */

import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { COLORS, GEOMETRY, type State } from "../lib/crossings";

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

interface TimelineObs {
  captured_at: string;
  state: State;
  object_key: string | null;
  camera_id: string;
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

export default function LiveBoard() {
  const [status, setStatus] = useState<Status | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [scrubT, setScrubT] = useState<number | null>(null); // null = live
  const [windowHours, setWindowHours] = useState(24);
  const [timelines, setTimelines] = useState<Record<string, TimelineObs[]>>({});
  const [loadedHours, setLoadedHours] = useState(0);
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [tick, setTick] = useState(0);
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    fetch("/api/v1/status").then((r) => r.json()).then(setStatus);
    // The lanes under the scrubber: blockages findable by eye before
    // scrubbing, so the history is worth a drag in the first place.
    fetch("/api/v1/sessions?limit=500")
      .then((r) => r.json())
      .then((body) => setSessions(body.sessions));
    const source = new EventSource("/api/v1/events");
    source.addEventListener("status", (e) => setStatus(JSON.parse((e as MessageEvent).data)));
    sourceRef.current = source;
    const timer = setInterval(() => setTick((t) => t + 1), 1000);
    return () => {
      source.close();
      clearInterval(timer);
    };
  }, []);

  // Scrub data loads lazily the first time the slider moves off "now", and
  // reloads when the window widens past what has been fetched.
  const loadTimelines = async (hours: number) => {
    if (loadedHours >= hours) return;
    const loaded: Record<string, TimelineObs[]> = {};
    for (const id of Object.keys(GEOMETRY)) {
      const r = await fetch(`/api/v1/timeline?crossing_id=${id}&hours=${hours}`);
      loaded[id] = (await r.json()).observations;
    }
    setTimelines(loaded);
    setLoadedHours(hours);
  };

  const scrubbing = scrubT !== null;
  const board = useMemo(() => {
    if (!status) return null;
    if (!scrubbing) return status;
    // Time-travel view: each crossing shows its state at the scrubbed instant.
    const at = new Date(scrubT!);
    return {
      generated_at: at.toISOString(),
      crossings: status.crossings.map((c) => {
        const past = (timelines[c.crossing_id] ?? []).filter(
          (o) => new Date(o.captured_at) <= at,
        );
        const last = past[past.length - 1];
        const frames = new Map<string, TimelineObs>();
        for (const o of past) if (o.object_key) frames.set(o.camera_id, o);
        return {
          ...c,
          state: last ? last.state : ("UNKNOWN" as State),
          stale: !last,
          since: last?.captured_at ?? null,
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
  }, [status, scrubbing, scrubT, timelines]);

  if (!board) return <p class="loading">Contacting the board...</p>;

  const chosen = board.crossings.find((c) => c.crossing_id === selected) ?? null;
  const now = Date.now();
  const windowStart = now - windowHours * 3600 * 1000;
  const corridor = Object.keys(GEOMETRY);
  const lanes = corridor.map((id) =>
    sessions
      .filter((s) => s.crossing_id === id)
      .map((s) => {
        const a = new Date(s.started_at).getTime();
        const b = s.ended_at ? new Date(s.ended_at).getTime() : now;
        return [a, b] as [number, number];
      })
      .filter(([a, b]) => b > windowStart && a < now),
  );

  return (
    <div class="board">
      <svg viewBox="0 0 960 520" role="img" aria-label="Map of the three crossings">
        {/* the rail line: double stroke reads as track */}
        <line x1="120" y1="20" x2="840" y2="520" stroke="var(--hairline)" stroke-width="10" />
        <line x1="120" y1="20" x2="840" y2="520" stroke="var(--ink)" stroke-width="6" />
        <line
          x1="120" y1="20" x2="840" y2="520"
          stroke="var(--muted)" stroke-width="2" stroke-dasharray="1 14"
        />
        {Object.entries(GEOMETRY).map(([id, g]) => {
          const crossing = board.crossings.find((c) => c.crossing_id === id);
          const state: State = crossing?.state ?? "UNKNOWN";
          return (
            <g key={id}>
              <line
                x1={g.x - 190} y1={g.y} x2={g.x + 190} y2={g.y}
                stroke="var(--hairline)" stroke-width="3"
              />
              <text x={g.x - 185} y={g.y - 10} class="street">{g.street}</text>
              <g
                role="button"
                tabIndex={0}
                aria-label={`${g.label}: ${state}`}
                onClick={() => setSelected(id)}
                onKeyDown={(e) => e.key === "Enter" && setSelected(id)}
                style="cursor: pointer"
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
                if (scrubbing) await loadTimelines(hours);
              }}
            >
              {label}
            </button>
          ))}
        </div>
        <span class="data when">
          {scrubbing ? new Date(scrubT!).toLocaleString() : "now"}
        </span>
      </div>

      <div class="crossings-list">
        {board.crossings.map((c) => {
          const g = GEOMETRY[c.crossing_id];
          return (
            <button
              class={`row ${selected === c.crossing_id ? "chosen" : ""}`}
              onClick={() => setSelected(c.crossing_id)}
            >
              <span class="dot" style={`background:${COLORS[c.state]}`} />
              <span class="display name">{g?.label ?? c.crossing_id}</span>
              <span class="data">{stateLine(c, tick)}</span>
            </button>
          );
        })}
      </div>

      {chosen && (
        <section class="detail" aria-live="polite">
          <header>
            <h2>{GEOMETRY[chosen.crossing_id]?.label}</h2>
            <span class="data state-word" style={`color:${COLORS[chosen.state]}`}>
              {chosen.stale ? "UNKNOWN (stale)" : chosen.state}
            </span>
            <button class="close" onClick={() => setSelected(null)} aria-label="Close">
              ✕
            </button>
          </header>
          {/* The ticker runs only while the state itself is BLOCKED. An open
              session can outlive the blockage by design (it closes after ten
              quiet minutes plus watermark lag), and a red "blocked for 28m"
              beside a green CLEAR is nonsense presentation of sensible data. */}
          {chosen.state === "BLOCKED" && chosen.open_session && !scrubbing && (
            <p class="data ticker">
              Blocked for {duration(chosen.open_session.started_at, tick)}
            </p>
          )}
          {chosen.latest_observation && !scrubbing && (
            <p class="reason">
              {chosen.latest_observation.reason} (confidence{" "}
              {Math.round(chosen.latest_observation.confidence * 100)}%)
            </p>
          )}
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
                    <span class="data"> · {new Date(cam.captured_at).toLocaleTimeString()}</span>
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

function stateLine(c: Crossing, _tick: number): string {
  if (c.stale) return "no recent signal";
  if (c.state === "BLOCKED" && c.open_session) {
    return `blocked ${duration(c.open_session.started_at, _tick)}`;
  }
  if (c.since) {
    return `${c.state.toLowerCase()} since ${new Date(c.since).toLocaleTimeString()}`;
  }
  return c.state.toLowerCase();
}

function duration(startIso: string, _tick: number): string {
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(startIso).getTime()) / 1000));
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m ${String(s).padStart(2, "0")}s`;
}

const css = `
.board svg { width: 100%; height: auto; display: block; }
.board .street { fill: var(--muted); font-family: var(--data); font-size: 11px; letter-spacing: 0.1em; }
.board .label { fill: var(--crossbuck); font-family: var(--display); font-size: 20px; letter-spacing: 0.04em; text-transform: uppercase; }
.pulse { animation: pulse 2.4s ease-in-out infinite; }
@keyframes pulse { 0%,100% { opacity: 1 } 50% { opacity: 0.45 } }

.scrub { display: flex; align-items: center; gap: 0.75rem; padding: 0.5rem 0 1rem; }
.scrub .track { flex: 1; }
.scrub input { width: 100%; display: block; accent-color: var(--signal-amber); }
.scrub button { background: var(--panel); color: var(--crossbuck); border: 1px solid var(--hairline); border-radius: 4px; padding: 0.3rem 0.8rem; cursor: pointer; font-family: var(--display); letter-spacing: 0.05em; }
.scrub button.live { color: var(--signal-green); }
.scrub .when { color: var(--muted); min-width: 11ch; text-align: right; font-size: 0.85rem; }
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
.cameras { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1rem; margin-top: 1rem; }
.cameras figure { margin: 0; }
.cameras img { width: 100%; border-radius: 4px; border: 1px solid var(--hairline); }
.cameras .noframe { aspect-ratio: 4/3; display: grid; place-items: center; color: var(--muted); border: 1px dashed var(--hairline); border-radius: 4px; }
.cameras figcaption { color: var(--muted); font-size: 0.85rem; margin-top: 0.35rem; }
.loading { color: var(--muted); }
`;
