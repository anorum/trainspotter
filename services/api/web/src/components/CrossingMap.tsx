/** The board's core view: the real corridor on a dark basemap, with a
 * grade-crossing flasher at each featured crossing's true coordinates.
 * Re-featuring a crossing adds its flasher where the crossing actually is.
 *
 * The marker is the signature: a two-lamp signal housing, the form of the
 * flashers that guard the real crossing. CLEAR shows both lamps steady green,
 * UNKNOWN steady amber, and BLOCKED alternates the two lamps in red at
 * flasher cadence - the map says what the signals at the street are saying.
 *
 * Leaflet loads dynamically inside the effect: it touches `window` at import
 * time, and this component's markup is server-rendered by the island build.
 * The map fits bounds across every featured crossing, so re-featuring one
 * puts its flasher on the map with no further work here.
 */

import "leaflet/dist/leaflet.css";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useRef } from "preact/hooks";
import type * as Leaflet from "leaflet";
import { FEATURED, GEOMETRY, type State } from "../lib/crossings";

// OpenFreeMap: keyless and unmetered, with self-hosting as the escape
// hatch - chosen when CARTO put an API key (and a watermark) in front of
// its raster tiles and marked them for retirement. Vector, so the layer
// is MapLibre inside Leaflet; everything else about the map is untouched.
// Attribution comes from the style itself (OpenFreeMap, OpenMapTiles, and
// the OpenStreetMap data credit), so none is declared here.
const STYLE = "https://tiles.openfreemap.org/styles/dark";

export interface MapStates {
  /** crossing_id -> live aspect, stale collapsing to UNKNOWN upstream. */
  [crossingId: string]: State;
}

function flasherHtml(state: State, label: string): string {
  return (
    `<span class="flasher ${state}" aria-hidden="true"><i></i><i></i></span>` +
    `<span class="maplabel">${label}</span>`
  );
}

export default function CrossingMap({
  states,
  onSelect,
}: {
  states: MapStates;
  /** Present when the board offers a choice; absent on a solo board. */
  onSelect?: (crossingId: string) => void;
}) {
  const holder = useRef<HTMLDivElement>(null);
  const map = useRef<Leaflet.Map | null>(null);
  const markers = useRef<Map<string, Leaflet.Marker>>(new Map());
  const applied = useRef<Map<string, State>>(new Map());
  const leaflet = useRef<typeof Leaflet | null>(null);

  useEffect(() => {
    let disposed = false;
    void (async () => {
      const L = await import("leaflet");
      // Vite does not emit the worker maplibre's ESM build references from
      // an island's dynamic import (and hashed asset names would break the
      // worker's relative import of its shared chunk), leaving the map a
      // black canvas. prebuild copies worker + shared chunk verbatim into
      // public/maplibre/, and maplibre is pointed at that stable path.
      const { setWorkerUrl } = await import("maplibre-gl");
      setWorkerUrl("/maplibre/maplibre-gl-worker.mjs");
      // The layer factory comes from the plugin's own export, not the
      // `L.maplibreGL` it also patches on: that patch lands on Leaflet's
      // CommonJS object, which the frozen ESM namespace above never sees.
      const { maplibreGL } = await import("@maplibre/maplibre-gl-leaflet");
      if (disposed || !holder.current || map.current) return;
      leaflet.current = L;
      // The page scroll must survive crossing the card: no wheel zoom on
      // desktop, and no one-finger pan on touch - a swipe over the map keeps
      // scrolling the page. Pinch zoom (touchZoom) stays for touch users.
      const m = L.map(holder.current, {
        zoomControl: true,
        scrollWheelZoom: false,
        dragging: !L.Browser.mobile,
        attributionControl: true,
      });
      // Text-only prefix: the default embeds a flag SVG that the site's
      // global `svg` reset inflates to the size of the card.
      m.attributionControl.setPrefix(
        '<a href="https://leafletjs.com">Leaflet</a>',
      );
      maplibreGL({ style: STYLE }).addTo(m);
      const points = FEATURED.map((id) => {
        const g = GEOMETRY[id];
        return [g.lat, g.lon] as [number, number];
      });
      if (points.length === 1) m.setView(points[0], 15);
      else m.fitBounds(L.latLngBounds(points).pad(0.25));
      for (const id of FEATURED) {
        const g = GEOMETRY[id];
        const marker = L.marker([g.lat, g.lon], {
          icon: L.divIcon({
            className: "flasher-pin",
            html: flasherHtml(states[id] ?? "UNKNOWN", g.label),
            iconSize: [34, 18],
            iconAnchor: [17, 9],
          }),
          keyboard: Boolean(onSelect),
          title: g.label,
        }).addTo(m);
        if (onSelect) marker.on("click", () => onSelect(id));
        markers.current.set(id, marker);
        applied.current.set(id, states[id] ?? "UNKNOWN");
      }
      map.current = m;
    })();
    return () => {
      disposed = true;
      map.current?.remove();
      map.current = null;
      markers.current.clear();
    };
  }, []);

  // Aspect changes re-skin the existing markers; the map itself never
  // rebuilds. The board re-renders every second and hands us a fresh object
  // each time, so markers only touch the DOM when their aspect really moved.
  useEffect(() => {
    const L = leaflet.current;
    if (!L) return;
    for (const [id, marker] of markers.current) {
      const aspect = states[id] ?? "UNKNOWN";
      if (applied.current.get(id) === aspect) continue;
      applied.current.set(id, aspect);
      marker.setIcon(
        L.divIcon({
          className: "flasher-pin",
          html: flasherHtml(aspect, GEOMETRY[id as keyof typeof GEOMETRY].label),
          iconSize: [34, 18],
          iconAnchor: [17, 9],
        }),
      );
    }
  }, [states]);

  return <div class="crossing-map" ref={holder} />;
}
