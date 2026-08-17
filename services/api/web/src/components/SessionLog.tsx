/** The train sheet: every recorded blockage, newest first.
 *
 * Dispatchers log movements on a train sheet; this is ours. Rows group by
 * day, each carries an inline duration bar (width is information - scanning
 * the sheet shows which blockages were the bad ones), and expanding a row
 * pulls the event-recorder tape: frames sampled evenly across the session,
 * timestamped, from the camera that saw the most.
 */

import { useEffect, useState } from "preact/hooks";
import { COLORS, FEATURED, SOLO, crossingLabel, featuredOnly, sessionsUrl } from "../lib/crossings";

interface Session {
  session_id: string;
  crossing_id: string;
  started_at: string;
  ended_at: string | null;
  duration_seconds: number | null;
  is_open: boolean;
  detector_version: string;
}

interface TimelineObs {
  captured_at: string;
  state: string;
  object_key: string | null;
  camera_id: string;
}

// The sheet shows only the featured crossings, so on a solo sheet the chips
// would be a one-option chooser: the lone id is the standing filter instead.
const FILTERS = SOLO ? FEATURED : ["ALL", ...FEATURED];
const STRIP_FRAMES = 10;
const PAD_MS = 2 * 60_000;

export default function SessionLog() {
  const [sessions, setSessions] = useState<Session[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [filter, setFilter] = useState(FILTERS[0]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [strips, setStrips] = useState<Record<string, TimelineObs[]>>({});
  const [stripFailed, setStripFailed] = useState<Record<string, boolean>>({});
  const [featured, setFeatured] = useState<Record<string, string>>({});

  useEffect(() => {
    fetch(sessionsUrl(200))
      .then((r) => {
        if (!r.ok) throw new Error(`sessions ${r.status}`);
        return r.json();
      })
      .then(
        (body) => setSessions(body.sessions),
        () => setFailed(true),
      );
  }, []);

  const toggle = async (s: Session) => {
    if (expanded === s.session_id) {
      setExpanded(null);
      return;
    }
    setExpanded(s.session_id);
    if (strips[s.session_id]) return;
    setStripFailed((prev) => ({ ...prev, [s.session_id]: false }));
    const from = new Date(new Date(s.started_at).getTime() - PAD_MS).toISOString();
    const to = s.ended_at
      ? new Date(new Date(s.ended_at).getTime() + PAD_MS).toISOString()
      : new Date().toISOString();
    try {
      const r = await fetch(
        `/api/v1/timeline?crossing_id=${s.crossing_id}&from=${from}&to=${to}`,
      );
      if (!r.ok) throw new Error(`timeline ${r.status}`);
      const observations: TimelineObs[] = (await r.json()).observations;
      setStrips((prev) => ({ ...prev, [s.session_id]: tape(observations) }));
    } catch {
      // Deliberately not cached: only a store that answered can say a session
      // kept no frames, and leaving the strip unset lets a re-expand retry.
      setStripFailed((prev) => ({ ...prev, [s.session_id]: true }));
    }
  };

  if (failed) {
    return <p class="empty">The history store is not answering; the sheet will be back.</p>;
  }
  if (!sessions) return <p class="loading">Pulling the sheet...</p>;

  // Plain derivations: at <=200 rows there is nothing worth memoizing.
  const shown = featuredOnly(sessions).filter((s) => filter === "ALL" || s.crossing_id === filter);
  const longest = Math.max(1, ...shown.map((s) => s.duration_seconds ?? 0));
  const byDay: [string, Session[]][] = [];
  for (const s of shown) {
    const day = new Date(s.started_at).toLocaleDateString(undefined, {
      weekday: "long",
      month: "long",
      day: "numeric",
    });
    const last = byDay[byDay.length - 1];
    if (last && last[0] === day) last[1].push(s);
    else byDay.push([day, [s]]);
  }

  return (
    <div class="sheet">
      {!SOLO && (
        <div class="filters" role="group" aria-label="Filter by crossing">
          {FILTERS.map((f) => (
            <button
              class={filter === f ? "chip on" : "chip"}
              aria-pressed={filter === f}
              onClick={() => setFilter(f)}
            >
              {f === "ALL" ? "All crossings" : crossingLabel(f)}
            </button>
          ))}
        </div>
      )}

      {shown.length === 0 && (
        <p class="empty">No blockages on record for this crossing yet.</p>
      )}

      {byDay.map(([day, rows]) => (
        <section key={day}>
          <h2 class="day">{day}</h2>
          {rows.map((s) => {
            const open = expanded === s.session_id;
            const strip = strips[s.session_id];
            const pick =
              featured[s.session_id] ?? strip?.[Math.floor((strip.length - 1) / 2)]?.object_key;
            return (
              <article key={s.session_id} class={open ? "entry open" : "entry"}>
                <button
                  class="row"
                  aria-expanded={open}
                  onClick={() => toggle(s)}
                >
                  <span
                    class={s.is_open ? "aspect pulse" : "aspect"}
                    style={`background:${COLORS.BLOCKED}`}
                  />
                  <span class="display name">{crossingLabel(s.crossing_id)}</span>
                  <span class="data start">
                    {new Date(s.started_at).toLocaleTimeString([], {
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </span>
                  <span class="bar-lane" aria-hidden="true">
                    <span
                      class="bar"
                      style={`width:${(100 * (s.duration_seconds ?? 0)) / longest}%`}
                    />
                  </span>
                  <span class="data dur">
                    {s.is_open ? "in progress" : human(s.duration_seconds)}
                  </span>
                </button>

                {open && (
                  <div class="tape">
                    {!strip && !stripFailed[s.session_id] && (
                      <p class="loading">Pulling the tape...</p>
                    )}
                    {!strip && stripFailed[s.session_id] && (
                      <p class="empty">
                        The history store is not answering; the tape will be back.
                      </p>
                    )}
                    {strip && strip.length === 0 && (
                      <p class="empty">No frames kept for this session.</p>
                    )}
                    {strip && strip.length > 0 && (
                      <>
                        {pick && (
                          <img
                            class="feature"
                            src={`/api/v1/frames/${pick}`}
                            alt={`${crossingLabel(s.crossing_id)} during the blockage`}
                          />
                        )}
                        <div class="stills" role="list">
                          {strip.map((o) => (
                            <figure
                              key={o.object_key}
                              role="listitem"
                              class={o.object_key === pick ? "still picked" : "still"}
                              onClick={() =>
                                o.object_key &&
                                setFeatured((prev) => ({
                                  ...prev,
                                  [s.session_id]: o.object_key!,
                                }))
                              }
                            >
                              <img
                                src={`/api/v1/frames/${o.object_key}`}
                                alt=""
                                loading="lazy"
                              />
                              <figcaption class="data">
                                {new Date(o.captured_at).toLocaleTimeString([], {
                                  hour: "2-digit",
                                  minute: "2-digit",
                                })}
                              </figcaption>
                            </figure>
                          ))}
                        </div>
                        <p class="scored data">scored by {s.detector_version}</p>
                      </>
                    )}
                  </div>
                )}
              </article>
            );
          })}
        </section>
      ))}
      <style>{css}</style>
    </div>
  );
}

/** The event-recorder tape: frames from the camera that saw the most of the
 * session, sampled evenly so a two-hour blockage and a six-minute one both
 * read in one strip. One viewpoint, not an interleave of two - a strip that
 * jumps between cameras reads as a glitch, not a record. */
function tape(observations: TimelineObs[]): TimelineObs[] {
  const withFrames = observations.filter((o) => o.object_key);
  const votes = new Map<string, number>();
  for (const o of withFrames) {
    if (o.state === "BLOCKED") votes.set(o.camera_id, (votes.get(o.camera_id) ?? 0) + 1);
  }
  const dominant =
    [...votes.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? withFrames[0]?.camera_id;
  const one = withFrames.filter((o) => o.camera_id === dominant);
  if (one.length <= STRIP_FRAMES) return one;
  const picked: TimelineObs[] = [];
  for (let i = 0; i < STRIP_FRAMES; i++) {
    picked.push(one[Math.round((i * (one.length - 1)) / (STRIP_FRAMES - 1))]);
  }
  return [...new Set(picked)];
}

function human(seconds: number | null): string {
  if (seconds == null) return "-";
  const m = Math.round(seconds / 60);
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`;
}

const css = `
.filters { display: flex; gap: 0.5rem; flex-wrap: wrap; padding: 0.25rem 0 1rem; }
.chip { background: var(--panel); color: var(--muted); border: 1px solid var(--hairline); border-radius: 999px; padding: 0.25rem 0.9rem; cursor: pointer; font-family: var(--display); letter-spacing: 0.05em; text-transform: uppercase; font-size: 0.9rem; }
.chip.on { color: var(--crossbuck); border-color: var(--signal-amber); }

.day { font-size: 1.1rem; color: var(--muted); border-bottom: 1px solid var(--hairline); padding-bottom: 0.3rem; margin: 1.5rem 0 0.5rem; }

.entry { border-bottom: 1px solid var(--hairline); }
.row { display: grid; grid-template-columns: 14px minmax(9rem, max-content) 9ch 1fr 11ch; align-items: center; gap: 0.9rem; width: 100%; padding: 0.6rem 0.25rem; background: none; border: 0; color: var(--crossbuck); cursor: pointer; text-align: left; font-size: 1rem; }
.entry.open .row { background: var(--panel); }
.aspect { width: 12px; height: 12px; border-radius: 50%; }
.name { font-size: 1.15rem; }
.start, .dur { color: var(--muted); font-size: 0.85rem; white-space: nowrap; }
.dur { text-align: right; }
.bar-lane { height: 6px; background: none; border-radius: 3px; overflow: hidden; }
.bar { display: block; height: 100%; min-width: 3px; background: var(--signal-red); border-radius: 3px; opacity: 0.75; }

.tape { padding: 0.75rem 0.25rem 1.25rem; }
.feature { width: 100%; max-width: 720px; display: block; border: 1px solid var(--hairline); border-radius: 4px; margin-bottom: 0.75rem; }
.stills { display: flex; gap: 0.5rem; overflow-x: auto; padding-bottom: 0.5rem; }
.still { margin: 0; flex: 0 0 auto; cursor: pointer; }
.still img { height: 72px; display: block; border-radius: 3px; border: 2px solid transparent; }
.still.picked img { border-color: var(--signal-amber); }
.still figcaption { color: var(--muted); font-size: 0.7rem; text-align: center; margin-top: 0.15rem; }
.scored { color: var(--muted); font-size: 0.75rem; margin: 0.5rem 0 0; }

@media (max-width: 640px) {
  .row { grid-template-columns: 12px 1fr 9ch 11ch; gap: 0.6rem; }
  .bar-lane { display: none; }
}
`;
