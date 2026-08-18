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
  /** Real-world position, from the roster's camera coordinates - the map
   *  card and the "open in Google Maps" link both derive from it. */
  lat: number;
  lon: number;
}

export const GEOMETRY = {
  SE_8TH_DIVISION: {
    x: 280, y: 130, label: "8th & Division", street: "SE DIVISION ST",
    lat: 45.50494, lon: -122.6536,
  },
  SE_12TH_CLINTON: {
    x: 480, y: 270, label: "12th & Clinton", street: "SE CLINTON ST",
    lat: 45.5036, lon: -122.65381,
  },
  SE_11TH_MILWAUKIE: {
    x: 680, y: 410, label: "11th & Milwaukie", street: "SE MILWAUKIE AVE",
    lat: 45.50329, lon: -122.65457,
  },
} satisfies Record<string, CrossingGeometry>;

/** The pannable in-place map for a crossing's card - keyless Google embed. */
export function mapEmbedUrl(g: CrossingGeometry): string {
  return `https://maps.google.com/maps?q=${g.lat},${g.lon}&z=16&output=embed`;
}

/** The full Google Maps page for the crossing, for the expand link. */
export function mapPageUrl(g: CrossingGeometry): string {
  return `https://www.google.com/maps/search/?api=1&query=${g.lat},${g.lon}`;
}

/** The rail line's own name, the way a dispatcher's chart would carry it. */
export const RAIL_NAME = "UPRR BROOKLYN SUB";

/** A crossing the schematic can place. The board draws signal heads, cross
 *  streets and a close-up viewBox straight off these coordinates, so an id with
 *  no entry here has nowhere to be drawn. */
export type CrossingId = keyof typeof GEOMETRY;

/** The whole NW-SE diagonal, which is what the schematic is drawn in. */
export const FULL_CORRIDOR_VIEWBOX = "0 0 960 520";

/** The corridor's own extent: the diagonal the crossings above sit on. */
const CORRIDOR = { x1: 120, y1: 20, x2: 840, y2: 520 };

/** How far past each end of the corridor the track keeps going, as a share of
 *  the corridor's own span. A window on one crossing has to show line entering
 *  and leaving frame; drawn only to the corridor's extent, a close-up on either
 *  end crossing catches the line's own endpoint and the track stops dead inside
 *  the frame with blank space beyond it. Every viewBox crops the run-out. */
const RUN_OUT = 0.45;

const [OVERRUN_X, OVERRUN_Y] = [
  (CORRIDOR.x2 - CORRIDOR.x1) * RUN_OUT,
  (CORRIDOR.y2 - CORRIDOR.y1) * RUN_OUT,
];

/** The rail line as drawn: the corridor plus its run-out at both ends. */
export const RAIL = {
  x1: CORRIDOR.x1 - OVERRUN_X,
  y1: CORRIDOR.y1 - OVERRUN_Y,
  x2: CORRIDOR.x2 + OVERRUN_X,
  y2: CORRIDOR.y2 + OVERRUN_Y,
};

/** A window on one crossing's stretch of the corridor, offset so the signal
 *  head sits above centre and its label has room beneath. A solo board frames
 *  this instead of the full diagonal, which would leave one dot in open space. */
export function closeUpOn(g: CrossingGeometry): string {
  return `${g.x - 300} ${g.y - 140} 600 300`;
}

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
  return Object.hasOwn(GEOMETRY, id);
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
