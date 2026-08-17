/** The corridor's fixed facts, shared by every island.
 *
 * Schematic positions: the rail line runs NW to SE, true to the corridor.
 */

export type State = "CLEAR" | "BLOCKED" | "UNKNOWN";

export interface CrossingGeometry {
  x: number;
  y: number;
  label: string;
  street: string;
}

export const GEOMETRY = {
  SE_8TH_DIVISION: { x: 280, y: 130, label: "8th & Division", street: "SE DIVISION ST" },
  SE_12TH_CLINTON: { x: 480, y: 270, label: "12th & Clinton", street: "SE CLINTON ST" },
  SE_11TH_MILWAUKIE: { x: 680, y: 410, label: "11th & Milwaukie", street: "SE MILWAUKIE AVE" },
} satisfies Record<string, CrossingGeometry>;

/** A crossing the schematic can place. The board draws signal heads, cross
 *  streets and a close-up viewBox straight off these coordinates, so an id with
 *  no entry here has nowhere to be drawn. */
export type CrossingId = keyof typeof GEOMETRY;

/** The crossings the site presents, in corridor order. Every camera still
 *  captures and scores in the background, so the record keeps accumulating for
 *  the day the rest come back; the product's promise is just one crossing done
 *  properly. Featuring a crossing again is adding its id here - and the type
 *  makes featuring one the schematic cannot place a build error rather than a
 *  board that renders nothing. */
export const FEATURED: CrossingId[] = ["SE_12TH_CLINTON"];

/** Whether the site is presenting a single crossing, which changes what the
 *  views owe the reader: the board is detail-first rather than a corridor
 *  overview, and a chooser between one thing is furniture, not a control. */
export const SOLO = FEATURED.length === 1;

/** The sessions endpoint, scoped to what the site presents. Two surfaces ask
 *  for sessions - the board's lanes and the train sheet - and the endpoint
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
  return id in GEOMETRY;
}

/** How a crossing is named to the reader. Takes any id the API served, because
 *  the history endpoints answer about the whole corridor; an id the schematic
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
