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

export const COLORS: Record<State, string> = {
  CLEAR: "var(--signal-green)",
  BLOCKED: "var(--signal-red)",
  UNKNOWN: "var(--signal-amber)",
};

export function crossingLabel(id: string): string {
  return GEOMETRY[id]?.label ?? id;
}
