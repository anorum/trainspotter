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

export const GEOMETRY: Record<string, CrossingGeometry> = {
  SE_8TH_DIVISION: { x: 280, y: 130, label: "8th & Division", street: "SE DIVISION ST" },
  SE_12TH_CLINTON: { x: 480, y: 270, label: "12th & Clinton", street: "SE CLINTON ST" },
  SE_11TH_MILWAUKIE: { x: 680, y: 410, label: "11th & Milwaukie", street: "SE MILWAUKIE AVE" },
};

/** The crossings the site presents, in corridor order. Every camera still
 *  captures and scores in the background, so the record keeps accumulating for
 *  the day the rest come back; the product's promise is just one crossing done
 *  properly. Featuring a crossing again is adding its id here. */
export const FEATURED: string[] = ["SE_12TH_CLINTON"];

/** Whether the site is presenting a single crossing, which changes what the
 *  views owe the reader: the board is detail-first rather than a corridor
 *  overview, and a chooser between one thing is furniture, not a control. */
export const SOLO = FEATURED.length === 1;

export const COLORS: Record<State, string> = {
  CLEAR: "var(--signal-green)",
  BLOCKED: "var(--signal-red)",
  UNKNOWN: "var(--signal-amber)",
};

export function crossingLabel(id: string): string {
  return GEOMETRY[id]?.label ?? id;
}
