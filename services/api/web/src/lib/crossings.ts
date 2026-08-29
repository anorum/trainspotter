/** The corridor's fixed facts, shared by every island. */

export type State = "CLEAR" | "BLOCKED" | "UNKNOWN";

export interface CrossingGeometry {
  label: string;
  /** Real-world position, from the roster's camera coordinates - the board's
   *  map places each flasher with these, and the Google Maps link derives
   *  from them too. */
  lat: number;
  lon: number;
}

export const GEOMETRY = {
  SE_8TH_DIVISION: { label: "8th & Division", lat: 45.50573, lon: -122.65745 },
  SE_12TH_CLINTON: { label: "12th & Clinton", lat: 45.5036, lon: -122.65381 },
  SE_11TH_MILWAUKIE: { label: "11th & Milwaukie", lat: 45.50329, lon: -122.65457 },
} satisfies Record<string, CrossingGeometry>;

/** The full Google Maps page for the crossing, for the expand link. */
export function mapPageUrl(g: CrossingGeometry): string {
  return `https://www.google.com/maps/search/?api=1&query=${g.lat},${g.lon}`;
}

/** The rail line's own name, the way a dispatcher's chart would carry it. */
export const RAIL_NAME = "UPRR BROOKLYN SUB";

/** A crossing the board's map can place. Flashers, labels, and the Google
 *  Maps link all come straight off these coordinates, so an id with no entry
 *  here has nowhere to be drawn. */
export type CrossingId = keyof typeof GEOMETRY;

/** The crossings the site presents, in corridor order. Every camera still
 *  captures and scores in the background, so the record keeps accumulating for
 *  the day the rest come back; the product's promise is just the featured
 *  crossings done properly. Featuring a crossing again is adding its id here -
 *  and the type makes featuring one the map cannot place a build error rather
 *  than a board that renders nothing. */
export const FEATURED: CrossingId[] = ["SE_12TH_CLINTON", "SE_8TH_DIVISION"];

/** Whether the site is presenting a single crossing, which changes what the
 *  views owe the reader: the board is detail-first rather than a corridor
 *  overview, and a chooser between one thing is furniture, not a control. */
export const SOLO = FEATURED.length === 1;

/** The sessions endpoint, scoped to what the site presents. Two surfaces ask
 *  for sessions - the train sheet and the patterns page - and the endpoint
 *  takes one crossing_id, so a solo site scopes the query and a wider one gets
 *  the corridor back and owes `featuredOnly` on the reply. Keeping the rule
 *  here means both surfaces change together when FEATURED does. */
export function sessionsUrl(limit: number): string {
  const scope = SOLO ? `&crossing_id=${FEATURED[0]}` : "";
  return `/api/v1/sessions?limit=${limit}${scope}`;
}

/** FEATURED as a membership test over any id the API might serve, which is a
 *  wider set than the ids the site presents. */
const PRESENTED: ReadonlySet<string> = new Set(FEATURED);

/** Drop rows about crossings the site does not present. The other half of the
 *  scoping rule above, and the half no query string can guarantee: /status is
 *  always corridor-wide, and /sessions only narrows for a solo site. A surface
 *  that renders an unfiltered reply would show a crossing the site has
 *  deliberately withheld, labelled as if it were on offer. */
export function featuredOnly<T extends { crossing_id: string }>(rows: T[]): T[] {
  return rows.filter((r) => PRESENTED.has(r.crossing_id));
}

export const COLORS: Record<State, string> = {
  CLEAR: "var(--signal-green)",
  BLOCKED: "var(--signal-red)",
  UNKNOWN: "var(--signal-amber)",
};

function isPlaced(id: string): id is CrossingId {
  return Object.hasOwn(GEOMETRY, id);
}

/** How a crossing is named to the reader. Takes any id the API served, because
 *  the history endpoints answer about the whole corridor; an id the map
 *  does not place falls back to itself rather than rendering blank. */
export function crossingLabel(id: string): string {
  return isPlaced(id) ? GEOMETRY[id].label : id;
}

/** The featured crossings named for page copy, in the site's register: one on
 *  its own, otherwise an and-joined list. The page heads read this rather than
 *  restating the roster, so a title, a browser tab and a search snippet cannot
 *  outlive a change to FEATURED. */
export function featuredLabels(ids: readonly string[] = FEATURED): string {
  const labels = ids.map(crossingLabel);
  if (labels.length < 3) return labels.join(" and ");
  return `${labels.slice(0, -1).join(", ")}, and ${labels[labels.length - 1]}`;
}

/** Agrees with FEATURED wherever page copy needs the noun. */
export const CROSSING_NOUN = SOLO ? "crossing" : "crossings";
