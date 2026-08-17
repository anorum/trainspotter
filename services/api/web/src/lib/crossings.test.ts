/** What FEATURED decides on the site's behalf: which rows a surface may render,
 * how deep the sheet may ask, and how the copy names the crossings.
 *
 * Every API reply is corridor-wide or nearly so - /status always is, /sessions
 * only narrows for a solo site - so what the site shows is decided here rather
 * than by the endpoint. Each surface that renders a reply goes through these,
 * and a surface that forgot would show a crossing the site withheld.
 */

import { describe, expect, it } from "vitest";
import {
  closeUpOn,
  type CrossingId,
  crossingLabel,
  FEATURED,
  featuredLabels,
  featuredOnly,
  FULL_CORRIDOR_VIEWBOX,
  GEOMETRY,
  RAIL,
  sessionsUrl,
  SOLO,
} from "./crossings";

/** An id that is not in the corridor at all, so this stays a real test of the
 *  rule whichever crossings FEATURED comes to hold. */
const WITHHELD = "SE_NOT_PRESENTED";

describe("featuredOnly", () => {
  it("keeps the presented crossings and drops the rest", () => {
    const rows = [
      { crossing_id: WITHHELD, started_at: "2026-08-17T06:00:00Z" },
      ...FEATURED.map((id) => ({ crossing_id: id, started_at: "2026-08-17T07:00:00Z" })),
    ];

    expect(featuredOnly(rows).map((r) => r.crossing_id)).toEqual(FEATURED);
  });

  it("keeps the reply's own order, which is newest-first on the sheet", () => {
    const rows = [
      { crossing_id: FEATURED[0], started_at: "2026-08-17T09:00:00Z" },
      { crossing_id: WITHHELD, started_at: "2026-08-17T08:00:00Z" },
      { crossing_id: FEATURED[0], started_at: "2026-08-17T07:00:00Z" },
    ];

    expect(featuredOnly(rows).map((r) => r.started_at)).toEqual([
      "2026-08-17T09:00:00Z",
      "2026-08-17T07:00:00Z",
    ]);
  });
});

describe("sessionsUrl", () => {
  it("spends the whole limit on the featured crossing while solo", () => {
    const url = new URL(sessionsUrl(200), "http://board.test");

    expect(url.pathname).toBe("/api/v1/sessions");
    expect(url.searchParams.get("limit")).toBe("200");
    // Unscoped, the limit would be shared with crossings the sheet then drops,
    // so a solo sheet would lose depth it is entitled to.
    expect(url.searchParams.get("crossing_id")).toBe(SOLO ? FEATURED[0] : null);
  });
});

describe("crossingLabel", () => {
  it("names a crossing the schematic places", () => {
    expect(crossingLabel("SE_12TH_CLINTON")).toBe("12th & Clinton");
  });

  it("falls back to the id for a crossing it cannot place", () => {
    // The history endpoints answer about the whole corridor, so an id with no
    // schematic entry still has to render as something.
    expect(crossingLabel(WITHHELD)).toBe(WITHHELD);
  });

  it("does not mistake an inherited object key for a placed crossing", () => {
    // A membership test that reaches Object.prototype would name this crossing
    // `undefined` and render a blank row.
    expect(crossingLabel("toString")).toBe("toString");
    expect(crossingLabel("constructor")).toBe("constructor");
  });
});

describe("featuredLabels", () => {
  it("names exactly what the site presents", () => {
    // The page heads take their crossing names from here, so this is the tie
    // between the tab, the meta description and FEATURED.
    const copy = featuredLabels();
    const placed = Object.keys(GEOMETRY) as CrossingId[];
    const withheld = placed.filter((id) => !FEATURED.includes(id));

    for (const id of FEATURED) expect(copy).toContain(crossingLabel(id));
    for (const id of withheld) expect(copy).not.toContain(crossingLabel(id));
  });

  it("reads as prose at every width the corridor can reach", () => {
    expect(featuredLabels(["SE_12TH_CLINTON"])).toBe("12th & Clinton");
    expect(featuredLabels(["SE_8TH_DIVISION", "SE_12TH_CLINTON"])).toBe(
      "8th & Division and 12th & Clinton",
    );
    expect(
      featuredLabels(["SE_8TH_DIVISION", "SE_12TH_CLINTON", "SE_11TH_MILWAUKIE"]),
    ).toBe("8th & Division, 12th & Clinton, and 11th & Milwaukie");
  });
});

/** How the drawn track meets a viewBox. The rail is one monotonic NW-SE
 *  diagonal, so it runs clean through a frame when the line's path crosses the
 *  frame's whole height *and* neither drawn end stops inside it - an end inside
 *  the frame is a stroke that halts in open space. */
function railThrough(viewBox: string) {
  const [minX, minY, width, height] = viewBox.split(" ").map(Number);
  const [maxX, maxY] = [minX + width, minY + height];
  const slope = (RAIL.y2 - RAIL.y1) / (RAIL.x2 - RAIL.x1);
  const yAt = (x: number) => RAIL.y1 + (x - RAIL.x1) * slope;
  const inFrame = (x: number, y: number) => x >= minX && x <= maxX && y >= minY && y <= maxY;
  return {
    crossesFrame: yAt(minX) < minY && yAt(maxX) > maxY,
    endsOutside: !inFrame(RAIL.x1, RAIL.y1) && !inFrame(RAIL.x2, RAIL.y2),
  };
}

describe("the drawn rail line", () => {
  it("runs through every frame the board can show", () => {
    // A close-up centred on an end crossing used to catch the line's own
    // endpoint, so the track stopped dead inside the frame with blank space
    // beyond it. Featuring a crossing is meant to be one id, and that includes
    // the one at either end of the corridor.
    const frames = [
      FULL_CORRIDOR_VIEWBOX,
      ...Object.values(GEOMETRY).map((g) => closeUpOn(g)),
    ];

    for (const frame of frames) {
      expect(railThrough(frame)).toEqual({ crossesFrame: true, endsOutside: true });
    }
  });

  it("still passes under every signal head it is drawn behind", () => {
    // The run-out only lengthens the line. If it ever rotated or shifted it,
    // the signal heads would float off the track. The widest stroke the rail is
    // drawn with is 10 units, so staying inside half of that keeps each head's
    // centre on the rail.
    const slope = (RAIL.y2 - RAIL.y1) / (RAIL.x2 - RAIL.x1);
    for (const g of Object.values(GEOMETRY)) {
      const drift = Math.abs(RAIL.y1 + (g.x - RAIL.x1) * slope - g.y);
      expect(drift).toBeLessThan(5);
    }
  });
});
