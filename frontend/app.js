/* ==========================================================================
   MSFS Tablet Tracker — client application
   --------------------------------------------------------------------------
   - Real-time WebSocket with automatic reconnection
   - Worldwide map: viewport-loaded airports, runways, gates and parking
     stands (OSM), SimBrief plan, flight trail, prediction vector
   - Smart auto-zoom, disengaged on the first user gesture
   - PFD, HSI, vertical profile + TOD alert, frequencies, METAR,
     landing rating, logbook, replay, night mode, fullscreen
   ========================================================================== */

"use strict";

/* ----------------------------- Preferences ------------------------------ */
const prefs = {
  get: (k, d) => localStorage.getItem("mtt_" + k) ?? d,
  set: (k, v) => localStorage.setItem("mtt_" + k, v),
};

/* --------------------------- Toast notifications ------------------------- */
function showToast(msg, kind = "info") {
  const box = document.getElementById("toasts");
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

/* ---------------------------- MSFS status pill --------------------------- */
const statusPill = document.getElementById("status-pill");
function setStatus(mode, text) {
  statusPill.className = "";
  statusPill.id = "status-pill";
  statusPill.classList.add(mode);
  document.getElementById("status-text").textContent = text;
}

/* ----------------------- Only one panel open at a time ------------------- */
const achPanel = document.getElementById("ach-panel");
const PANEL_IDS = {
  "metar-panel": "btn-metar", "freqs-panel": "btn-freqs",
  "logbook-panel": "btn-logbook", "ach-panel": "btn-logbook",
  "settings": "btn-settings",
};
/* Tap anywhere OUTSIDE an open panel to close it (touch-friendly:
   no need to reach the Close button, which is kept anyway). The panel's
   own toolbar button is excluded so its toggle keeps working. */
document.addEventListener("pointerdown", (ev) => {
  for (const [pid, bid] of Object.entries(PANEL_IDS)) {
    const panel = document.getElementById(pid);
    if (panel.classList.contains("hidden")) continue;
    if (panel.contains(ev.target)) continue;                        // inside
    if (document.getElementById(bid).contains(ev.target)) continue; // toggle
    panel.classList.add("hidden");
    document.getElementById(bid).classList.remove("active");
    if (pid === "metar-panel") clearInterval(metarTimer);
  }
});

function closePanels(except) {
  for (const [pid, bid] of Object.entries(PANEL_IDS)) {
    if (pid === except) continue;
    document.getElementById(pid).classList.add("hidden");
    document.getElementById(bid).classList.remove("active");
  }
  if (except !== "metar-panel") clearInterval(metarTimer);
}

function esc(s) {
  return String(s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* --------------------------------- Map ----------------------------------- */
const map = L.map("map", {
  center: [46.5, 2.5],
  zoom: 6,
  zoomControl: false,
  attributionControl: true,
});
L.control.zoom({ position: "topleft" }).addTo(map);
L.control.scale({ imperial: false, position: "bottomleft" }).addTo(map);

/* Base maps ---------------------------------------------------------------- */
const baseLayers = {
  "Map (OSM)": L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: "© OpenStreetMap",
  }),
  "Satellite (Esri)": L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 19, attribution: "© Esri" }
  ),
  "Topo (OpenTopoMap)": L.tileLayer("https://tile.opentopomap.org/{z}/{x}/{y}.png", {
    maxZoom: 17, attribution: "© OpenTopoMap (CC-BY-SA)",
  }),
};
const overlays = {};

function buildOpenAipLayer() {
  const key = prefs.get("oaipKey", "");
  if (!key) return null;
  return L.tileLayer(
    `https://api.tiles.openaip.net/api/data/openaip/{z}/{x}/{y}.png?apiKey=${key}`,
    { maxZoom: 14, opacity: 0.9, attribution: "© OpenAIP (CC-BY-NC)" }
  );
}
let oaipLayer = buildOpenAipLayer();
if (oaipLayer) overlays["VFR chart (OpenAIP)"] = oaipLayer;

baseLayers[prefs.get("baseLayer", "Map (OSM)")]?.addTo(map) ||
  baseLayers["Map (OSM)"].addTo(map);
const layerControl = L.control.layers(baseLayers, overlays, { position: "topleft" }).addTo(map);
map.on("baselayerchange", (e) => prefs.set("baseLayer", e.name));

/* ----------------------------- Aircraft icon ----------------------------- */
const AIRCRAFT_SVG = `
<svg width="44" height="44" viewBox="0 0 44 44" xmlns="http://www.w3.org/2000/svg">
  <g class="ac-rot" style="transform-origin:22px 22px;">
    <path d="M22 4 L25 14 L38 22 L38 26 L25 22 L24.5 32 L29 36 L29 39 L22 37
             L15 39 L15 36 L19.5 32 L19 22 L6 26 L6 22 L19 14 Z"
          fill="#ffd21f" stroke="#1b1b1b" stroke-width="1.6" stroke-linejoin="round"/>
  </g>
</svg>`;

const aircraftIcon = L.divIcon({
  className: "aircraft-icon", html: AIRCRAFT_SVG,
  iconSize: [44, 44], iconAnchor: [22, 22],
});
const aircraftMarker = L.marker([46.5, 2.5], {
  icon: aircraftIcon, zIndexOffset: 1000, interactive: true,
}).addTo(map);
aircraftMarker.bindPopup("Waiting for data…");

function setAircraftHeading(deg) {
  const el = aircraftMarker.getElement()?.querySelector(".ac-rot");
  if (el) el.style.transform = `rotate(${deg}deg)`;
}

/* ------------------------------ Flight trail ----------------------------- */
const TRAIL_MAX_POINTS = 4000;
/* A jump larger than this between two live positions can only be a
   relocation (flight finished loading, slew), never real flight — the
   trail restarts from the new spot instead of drawing a line across the
   map. Matches the server-side TELEPORT_NM. */
const TELEPORT_NM = 50;
const trail = L.polyline([], { color: "#ff9500", weight: 3, opacity: 0.85 }).addTo(map);
/* Trail with altitude, mirrored from the server: also feeds the real
   altitude curve on the vertical profile. profileX holds each point's
   distance along the SimBrief route (null when off-plan/no plan). */
let trailData = [];   // [{lat, lon, alt}]
let profileX = [];    // distance along route, aligned with trailData

function addTrailPoint(lat, lon, alt, exactX) {
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return; // no NaN in the trail
  let pts = trail.getLatLngs();
  const last = pts[pts.length - 1];
  if (last) {
    if (Math.abs(last.lat - lat) < 1e-4 && Math.abs(last.lng - lon) < 1e-4) return;
    // Teleport guard: MSFS relocating the aircraft (flight finished loading,
    // slew) must not draw a straight line across the map — restart the trail
    // from the new position. The server re-syncs shortly after; this keeps
    // the live 3 Hz trail clean in the meantime.
    if (distNM(last.lat, last.lng, lat, lon) > TELEPORT_NM) {
      trail.setLatLngs([]); trailData = []; profileX = [];
      pts = [];
    }
  }
  trailData.push({ lat, lon, alt: alt ?? 0, x: exactX ?? null });
  // Exact along-route distance from the server (same number as the plan
  // bar); windowed local projection only if the server didn't send one.
  let x = exactX;
  if (x == null) {
    x = flownDistNM(lat, lon);
    const prev = lastNonNull(profileX);
    const prevPt = trailData.length > 1 ? trailData[trailData.length - 2] : null;
    if (x != null && prev != null) {
      const moved = prevPt ? distNM(prevPt.lat, prevPt.lon, lat, lon) : 0.3;
      x = Math.min(Math.max(x, prev), prev + moved * 1.5 + 0.3);
    }
  }
  profileX.push(x);
  if (pts.length >= TRAIL_MAX_POINTS) {
    // Overflow: drop the oldest point and rebuild the polyline once
    pts.shift(); trailData.shift(); profileX.shift();
    pts.push(L.latLng(lat, lon));
    trail.setLatLngs(pts);
  } else {
    // Common case: incremental append instead of resetting the whole
    // array into Leaflet on every frame
    trail.addLatLng([lat, lon]);
  }
}

function lastNonNull(arr) {
  for (let i = arr.length - 1; i >= 0; i--) if (arr[i] != null) return arr[i];
  return null;
}

function setTrailFromServer(points) {
  trail.setLatLngs(points.map((p) => [p.lat, p.lon]));
  trailData = points.map((p) => ({ lat: p.lat, lon: p.lon, alt: p.alt ?? 0,
                                   x: p.x ?? null }));
  rebuildProfileX();
}

function rebuildProfileX() {
  // Exact server-recorded route distances when available (v1.2.1+ data);
  // client-side projection only as a fallback for older recordings.
  profileX = trailData.some((p) => p.x != null)
    ? trailData.map((p) => p.x)
    : projectTrackXs(trailData);
  drawProfile();
}

/* Distance flown along the loaded plan for an arbitrary position.
   True SEGMENT projection (closest point ON the route line), not the
   nearest-waypoint shortcut: that one could snap far FORWARD whenever
   the track passed close to a distant fix, which the monotonic clamp
   then locked in permanently (cruise curve pinned at the destination). */
function flownDistNM(lat, lon) {
  if (!currentPlan) return null;
  const wps = currentPlan.waypoints;
  const cosLat = Math.cos(lat * Math.PI / 180);
  let bestX = 0, bestD = Infinity;
  for (let i = 0; i < wps.length - 1; i++) {
    const a = wps[i], b = wps[i + 1];
    // local flat-earth frame in NM (fine at leg scale)
    const ax = (a.lon - lon) * 60 * cosLat, ay = (a.lat - lat) * 60;
    const bx = (b.lon - lon) * 60 * cosLat, by = (b.lat - lat) * 60;
    const dx = bx - ax, dy = by - ay;
    const len2 = dx * dx + dy * dy;
    const t = len2 > 0 ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / len2)) : 0;
    const px = ax + t * dx, py = ay + t * dy;      // closest point (rel.)
    const d = Math.hypot(px, py);                   // perpendicular dist
    if (d < bestD) {
      bestD = d;
      bestX = planCum[i] + t * (planCum[i + 1] - planCum[i]);
    }
  }
  return Math.max(0, Math.min(planTotal, bestX));
}

/* Projects a whole track onto the plan with a WINDOWED advance: each
   step may progress at most by the ground distance actually flown
   (plus margin), and never regress. Kills both backward jitter in
   turns and forward teleports near route doglegs. */
function projectTrackXs(pts) {
  if (!currentPlan) return pts.map(() => null);
  const xs = [];
  let prevX = null, prevPt = null;
  for (const p of pts) {
    const raw = flownDistNM(p.lat, p.lon);
    if (raw == null) { xs.push(null); continue; }
    let x = raw;
    if (prevX != null && prevPt != null) {
      const moved = distNM(prevPt.lat, prevPt.lon, p.lat, p.lon);
      x = Math.min(Math.max(raw, prevX), prevX + moved * 1.5 + 0.3);
    }
    xs.push(x);
    prevX = x; prevPt = p;
  }
  return xs;
}

/* ------------------------- Prediction vector -----------------------------
   Line ahead of the aircraft: estimated position in 1, 2 and 5 minutes at
   the current heading and ground speed. Great to anticipate intercepts. */
const predLayer = L.layerGroup().addTo(map);
layerControl.addOverlay(predLayer, "Prediction 1/2/5 min");

/* The line and the three dots are created once and repositioned on every
   frame (recreating them 3-5×/s caused needless Leaflet churn). */
const PRED_MINS = [1, 2, 5];
const predLine = L.polyline([], {
  color: "#35c8ff", weight: 2, opacity: 0.8, dashArray: "6 6",
});
const predDots = PRED_MINS.map((min) => L.circleMarker([0, 0], {
  radius: 3, color: "#35c8ff", fillColor: "#35c8ff", fillOpacity: 1, weight: 1,
}).bindTooltip(`${min} min`));

function updatePrediction(s) {
  if (s.on_ground || s.gs_kt < 40) { predLayer.clearLayers(); return; }
  // Ground TRACK, not heading: with crosswind the aircraft doesn't go
  // where the nose points — the prediction must follow the actual path.
  const brg = s.trk_true_deg ?? s.hdg_true_deg ?? s.hdg_deg;
  const pts = [[s.lat, s.lon]];
  PRED_MINS.forEach((min, i) => {
    const p = destPoint(s.lat, s.lon, brg, (s.gs_kt / 60) * min);
    pts.push(p);
    predDots[i].setLatLng(p);
    if (!predLayer.hasLayer(predDots[i])) predDots[i].addTo(predLayer);
  });
  predLine.setLatLngs(pts);
  if (!predLayer.hasLayer(predLine)) predLine.addTo(predLayer);
}

/* =========================================================================
   SMART AUTO-ZOOM
   ========================================================================= */
let autoFollow = true;
let programmaticMove = false;

const btnRecenter = document.getElementById("btn-recenter");

function targetZoom(state) {
  const agl = state.alt_agl_ft ?? 0;
  const gs = state.gs_kt ?? 0;
  if (state.on_ground) return gs > 40 ? 15 : 17;
  if (agl < 1500) return 14;
  if (agl < 5000) return 12;
  if (agl < 12000) return 10;
  return 8;
}

let autoZoom = true;   // set from prefs below

function followAircraft(state) {
  if (!autoFollow || replayMode) return;
  programmaticMove = true;
  // Auto-zoom off: keep centering the aircraft but respect whatever
  // zoom level the pilot chose with the map buttons.
  const z = autoZoom ? targetZoom(state) : map.getZoom();
  map.setView([state.lat, state.lon], z, { animate: true, duration: 0.4 });
}

function userInteracted() {
  if (programmaticMove || !autoFollow) return;
  autoFollow = false;
  btnRecenter.classList.remove("active");
  btnRecenter.classList.add("pulse");
}
map.on("dragstart", userInteracted);
map.on("zoomstart", () => {
  // With auto-zoom disabled, the +/- buttons are the normal way to set
  // the zoom: they must not disengage the aircraft tracking. Dragging
  // the map away still does.
  if (!programmaticMove && autoZoom) userInteracted();
});
map.on("moveend", () => { programmaticMove = false; });

btnRecenter.addEventListener("click", () => {
  autoFollow = true;
  btnRecenter.classList.add("active");
  btnRecenter.classList.remove("pulse");
  if (lastState) followAircraft(lastState);
});

/* =========================================================================
   WORLDWIDE AIRPORTS — viewport-based loading
   Wherever the map is panned in the world, airports in the visible area
   are loaded (major ones first at low zoom, everything at high zoom).
   ========================================================================= */
const airportLayer = L.layerGroup().addTo(map);
const gateLayer = L.layerGroup().addTo(map);
layerControl.addOverlay(airportLayer, "Airports");
layerControl.addOverlay(gateLayer, "Gates / stands");

let vpTimer = null;
map.on("moveend", () => {
  clearTimeout(vpTimer);
  vpTimer = setTimeout(refreshViewport, 350);   // debounce while panning
});

async function refreshViewport() {
  const b = map.getBounds(), z = map.getZoom();
  const qs = `s=${b.getSouth().toFixed(4)}&w=${b.getWest().toFixed(4)}` +
             `&n=${b.getNorth().toFixed(4)}&e=${b.getEast().toFixed(4)}&zoom=${z}`;
  try {
    const r = await fetch(`/api/airports_bbox?${qs}`);
    const { airports } = await r.json();
    drawAirports(airports || [], z);
  } catch (e) { /* database still downloading: will retry on next pan */ }

  // Gates / stands: only when zoomed onto an airport and the layer is on
  if (z >= 14 && map.hasLayer(gateLayer)) loadGates(b, z);
  else gateLayer.clearLayers();

  refreshAirspaces();
}

function drawAirports(list, z) {
  airportLayer.clearLayers();
  for (const ap of list) {
    // Labels appear one zoom level later than the markers: a lighter
    // map at wide zooms, names once you actually look at an area.
    const permanent =
      (ap.type === "large_airport" && z >= 7) ||
      (ap.type === "medium_airport" && z >= 10) ||
      (ap.type === "small_airport" && z >= 12);
    L.circleMarker([ap.lat, ap.lon], {
      radius: ap.type === "large_airport" ? 6 : ap.type === "medium_airport" ? 4 : 3,
      color: "#7a4fd0", weight: 2, fillColor: "#b393f0", fillOpacity: 0.7,
    })
      .bindTooltip(`<span class="airport-label">${esc(ap.ident)}</span>`, {
        permanent, direction: "right", offset: [8, 0],
      })
      .bindPopup(`<b>${esc(ap.ident)}</b> — ${esc(ap.name)}`)
      .addTo(airportLayer);

    for (const rw of ap.runways || []) {
      L.polyline([[rw.le_lat, rw.le_lon], [rw.he_lat, rw.he_lon]],
        { color: "#3d3d3d", weight: 5, opacity: 0.9 })
        .bindTooltip(`${rw.le_ident}/${rw.he_ident} · ${rw.length_ft} ft · ${esc(rw.surface)}`)
        .addTo(airportLayer);
    }
  }
}

/* Named boarding gates and parking stands (OSM) */
let gatesLoading = false;
async function loadGates(bounds, z) {
  if (gatesLoading) return;
  gatesLoading = true;
  try {
    const qs = `s=${bounds.getSouth().toFixed(4)}&w=${bounds.getWest().toFixed(4)}` +
               `&n=${bounds.getNorth().toFixed(4)}&e=${bounds.getEast().toFixed(4)}`;
    const r = await fetch(`/api/gates?${qs}`);
    if (!r.ok) return;                       // area too large or Overpass down
    const { gates } = await r.json();
    gateLayer.clearLayers();
    for (const g of gates || []) {
      const isGate = g.kind === "gate";
      L.circleMarker([g.lat, g.lon], {
        radius: isGate ? 4 : 3,
        color: isGate ? "#06657e" : "#666",
        fillColor: isGate ? "#35c8ff" : "#bbb",
        fillOpacity: 0.95, weight: 1,
      })
        .bindTooltip(`<span class="gate-label">${esc(g.name)}</span>`, {
          permanent: z >= 16,                // names shown at close zoom
          direction: "top", offset: [0, -4],
        })
        .bindPopup(`${isGate ? "Gate" : "Stand"} <b>${esc(g.name)}</b>`)
        .addTo(gateLayer);
    }
  } catch (e) { /* silent: server cache will serve it next time */ }
  finally { gatesLoading = false; }
}

/* ==================== "All my flights" heatmap overlay ====================
   Every archived flight drawn as a translucent line: repeated routes
   stack up and glow — a personal heatmap. Loaded lazily on first
   enable, refreshed on every re-enable (new flights included). */
const historyLayer = L.layerGroup();
layerControl.addOverlay(historyLayer, "All my flights");

async function loadHistoryLayer() {
  try {
    const r = await fetch("/api/flights/tracks");
    const { tracks } = await r.json();
    historyLayer.clearLayers();
    for (const t of tracks || []) {
      if (t.length < 2) continue;
      L.polyline(t, { color: "#ff9500", weight: 4, opacity: 0.8,
                      interactive: false }).addTo(historyLayer);
    }
    if (!tracks?.length)
      showToast("No archived flights yet — fly first!", "info");
  } catch {
    showToast("Could not load flight history.", "error");
  }
}
map.on("overlayadd", (e) => {
  if (e.name === "All my flights") loadHistoryLayer();
});

/* ==================== Online traffic (VATSIM / IVAO) ======================
   Small cyan aircraft refreshed every 20 s around the own position.
   Network selected in Settings; the server caches the upstream feed so
   every tablet shares one request. */
const trafficLayer = L.layerGroup().addTo(map);
layerControl.addOverlay(trafficLayer, "Online traffic");
let trafficTimer = null;
const NET_COLORS = { vatsim: ["#ff9500", "#7a4400"],   // fill, stroke
                     ivao:   ["#35c8ff", "#0a4d5e"] };
const netErrorShown = {};   // one toast per network per session

const TFC_SVG = (hdg, net) => `
<svg width="26" height="26" viewBox="0 0 26 26" xmlns="http://www.w3.org/2000/svg">
  <g style="transform:rotate(${hdg}deg);transform-origin:13px 13px;">
    <path d="M13 3 L15 9 L22 13 L22 15 L15 13 L14.5 19 L17 21 L17 23 L13 22
             L9 23 L9 21 L11.5 19 L11 13 L4 15 L4 13 L11 9 Z"
          fill="${NET_COLORS[net][0]}" stroke="${NET_COLORS[net][1]}" stroke-width="1"/>
  </g>
</svg>`;

/* Both networks can be enabled at once: VATSIM orange, IVAO cyan. */
function enabledNetworks() {
  const nets = [];
  if (prefs.get("netVatsim", "0") === "1") nets.push("vatsim");
  if (prefs.get("netIvao", "0") === "1") nets.push("ivao");
  return nets;
}

function restartTraffic() {
  clearInterval(trafficTimer);
  trafficLayer.clearLayers();
  if (!enabledNetworks().length) return;
  trafficTick();
  trafficTimer = setInterval(trafficTick, 20000);
}

async function trafficTick() {
  const c = lastState ? [lastState.lat, lastState.lon]
                      : [map.getCenter().lat, map.getCenter().lng];
  const results = await Promise.all(enabledNetworks().map(async (net) => {
    try {
      const r = await fetch(`/api/traffic?network=${net}&lat=${c[0]}&lon=${c[1]}&radius=250`);
      const data = await r.json();
      if (!r.ok) throw new Error(data.error);
      return { net, traffic: data.traffic || [] };
    } catch (e) {
      if (!netErrorShown[net]) {
        netErrorShown[net] = true;
        showToast(`${net.toUpperCase()}: ${e.message}`, "error");
      }
      return { net, traffic: [] };
    }
  }));
  trafficLayer.clearLayers();
  for (const { net, traffic } of results) {
    for (const t of traffic) {
      L.marker([t.lat, t.lon], {
        icon: L.divIcon({ className: "tfc-icon", html: TFC_SVG(t.hdg, net),
                          iconSize: [26, 26], iconAnchor: [13, 13] }),
        zIndexOffset: 500,
      })
        .bindTooltip(`<span class="tfc-label">${esc(t.callsign)} ` +
          `FL${String(Math.round(t.alt_ft / 100)).padStart(3, "0")}</span>`,
          { permanent: true, direction: "right", offset: [12, 0],
            className: "tfc-tooltip" })
        .bindPopup(`<b>${esc(t.callsign)}</b> (${net.toUpperCase()})<br>` +
          `${Math.round(t.alt_ft)} ft · ${t.gs_kt} kt · ${t.dist_nm} NM away`)
        .addTo(trafficLayer);
    }
  }
}

/* ==================== Vector airspaces + entry alerts ====================
   Requires the free OpenAIP key (Settings). Fetched per viewport from
   zoom 8, drawn as colored outlines; entering an airspace vertically and
   laterally raises a toast, once per airspace. */
const airspaceLayer = L.layerGroup();
layerControl.addOverlay(airspaceLayer, "Airspaces");
let airspaceData = [];        // normalized airspaces currently drawn
let insideAirspaces = new Set();

const ASP_COLORS = { A: "#d1273a", B: "#c77800", C: "#0a6fc1", D: "#0a6fc1",
                     E: "#5c6b7a", F: "#5c6b7a", G: "#5c6b7a", SUA: "#d1273a" };

async function refreshAirspaces() {
  const key = prefs.get("oaipKey", "");
  if (!key || map.getZoom() < 8 || !map.hasLayer(airspaceLayer)) {
    if (!map.hasLayer(airspaceLayer)) { airspaceLayer.clearLayers(); airspaceData = []; }
    return;
  }
  const b = map.getBounds();
  try {
    const r = await fetch(`/api/airspaces?s=${b.getSouth().toFixed(3)}` +
      `&w=${b.getWest().toFixed(3)}&n=${b.getNorth().toFixed(3)}` +
      `&e=${b.getEast().toFixed(3)}&key=${encodeURIComponent(key)}`);
    if (!r.ok) return;
    const { airspaces } = await r.json();
    airspaceData = airspaces || [];
    airspaceLayer.clearLayers();
    for (const a of airspaceData) {
      const low = (a.lower_ft === 0 && a.lower_ref === "GND") ? "GND"
        : `${a.lower_ft} ft${a.lower_ref === "GND" ? " AGL" : ""}`;
      const up = `${a.upper_ft} ft${a.upper_ref === "GND" ? " AGL" : ""}`;
      L.polygon(a.ring, {
        color: ASP_COLORS[a.class] || "#5c6b7a",
        weight: 1.5, fillOpacity: 0.04, opacity: 0.7,
      }).bindTooltip(
        `${esc(a.name)} (${esc(a.class)}) ${low}–${up}`,
        { sticky: true }
      ).addTo(airspaceLayer);
    }
  } catch { /* key invalid or offline: silent, tooltip layer stays empty */ }
}

/* Ray-casting point-in-polygon on a [lat, lon] ring */
function pointInRing(lat, lon, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [yi, xi] = ring[i], [yj, xj] = ring[j];
    if (((yi > lat) !== (yj > lat)) &&
        (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi)) inside = !inside;
  }
  return inside;
}

/* Altitude to compare against a limit, honoring its reference datum:
   GND limits are AGL, MSL limits are true altitude. STD (flight levels)
   are compared against true altitude too — no QNH is available here, an
   accepted approximation. Older cached areas lack the ref: treated as MSL
   (the previous behavior). */
function aspAlt(s, ref) {
  return ref === "GND" ? (s.alt_agl_ft ?? s.alt_ft) : s.alt_ft;
}

function checkAirspaces(s) {
  if (!airspaceData.length || s.on_ground) return;
  const now = new Set();
  for (const a of airspaceData) {
    if (aspAlt(s, a.lower_ref) < a.lower_ft ||
        aspAlt(s, a.upper_ref) > a.upper_ft) continue;
    const id = a.name + a.lower_ft;
    if (pointInRing(s.lat, s.lon, a.ring)) {
      now.add(id);
      if (!insideAirspaces.has(id))
        showToast(`Entering ${a.name} (class ${a.class})`, "info");
    }
  }
  insideAirspaces = now;
}

/* ==================== SimBrief flight plan on the map ===================== */
const planLayer = L.layerGroup().addTo(map);
layerControl.addOverlay(planLayer, "Flight plan");
let currentPlan = null;
let planCum = [];       // cumulative distance (NM) at each waypoint
let planTotal = 0;

function drawFlightPlan(plan) {
  currentPlan = plan;
  planLayer.clearLayers();
  if (!plan?.waypoints?.length) return;

  // Cumulative distances along the route (vertical profile + TOD alert)
  planCum = [0];
  for (let i = 1; i < plan.waypoints.length; i++) {
    const a = plan.waypoints[i - 1], b = plan.waypoints[i];
    planCum.push(planCum[i - 1] + distNM(a.lat, a.lon, b.lat, b.lon));
  }
  planTotal = planCum[planCum.length - 1];

  const pts = plan.waypoints.map((w) => [w.lat, w.lon]);
  L.polyline(pts, { color: "#ffffff", weight: 6, opacity: 0.6 }).addTo(planLayer);
  L.polyline(pts, { color: "#e13fd0", weight: 3, opacity: 0.95, dashArray: "1 8" })
    .addTo(planLayer);

  for (const w of plan.waypoints) {
    L.circleMarker([w.lat, w.lon], {
      radius: 3, color: "#e13fd0", fillColor: "#fff", fillOpacity: 1, weight: 2,
    }).bindTooltip(`<span class="wp-label">${esc(w.ident)}</span>`, {
      // Names always visible (no tap needed); hidden below zoom 7 via
      // a body class so a world view doesn't turn into label soup.
      permanent: true, direction: "top", offset: [0, -4],
      className: "wp-tooltip",
    }).addTo(planLayer);
  }
  updateWpLabelVisibility();

  const o = plan.origin, d = plan.destination;
  L.marker([o.lat, o.lon]).bindPopup(`Departure: <b>${esc(o.icao)}</b><br>${esc(o.name)}`).addTo(planLayer);
  L.marker([d.lat, d.lon]).bindPopup(`Arrival: <b>${esc(d.icao)}</b><br>${esc(d.name)}`).addTo(planLayer);

  drawRunwayExtensions(d.icao);
  rebuildProfileX();
  document.getElementById("plan-bar").classList.remove("hidden");
  document.body.classList.add("has-plan");   // sys-bar slides down
  document.getElementById("plan-route").textContent = `${o.icao} → ${d.icao}`;
  document.getElementById("sb-status").textContent =
    `Plan loaded: ${o.icao} → ${d.icao}, ${plan.distance_nm} NM, FL${Math.round(plan.cruise_alt_ft / 100)}.`;
  drawProfile();
}

/* ==================== Extended runway centerlines =========================
   Dashed approach axes extending 10 NM from each destination runway
   threshold, with 3/5/10 NM distance dots — the visual aid for lining up
   on final. Drawn automatically when a SimBrief plan loads. */
const approachLayer = L.layerGroup().addTo(map);
layerControl.addOverlay(approachLayer, "Approach axes");

/* ================== Map display options: persisted state ==================
   Every overlay toggle is saved and restored per device. The defaults
   below apply on first run (or after clearing browser data). */
const OVERLAY_DEFAULTS = {
  "Prediction 1/2/5 min": true,
  "Airports": true,
  "Gates / stands": true,
  "All my flights": false,
  "Online traffic": true,
  "Airspaces": false,
  "Flight plan": true,
  "Approach axes": false,
  "VFR chart (OpenAIP)": false,
};
let ovState = {};
try { ovState = JSON.parse(prefs.get("mapOverlays", "{}")) || {}; } catch { ovState = {}; }

function overlayWanted(name) {
  return name in ovState ? !!ovState[name] : !!OVERLAY_DEFAULTS[name];
}

/* Named map of the statically-registered overlays (the dynamic VFR chart
   is added separately when a key is set). Reused by the initial apply and
   by the "clean map" / "restore defaults" buttons in Settings. */
const OVERLAY_LAYERS = {
  "Prediction 1/2/5 min": predLayer,
  "Airports": airportLayer,
  "Gates / stands": gateLayer,
  "All my flights": historyLayer,
  "Online traffic": trafficLayer,
  "Airspaces": airspaceLayer,
  "Flight plan": planLayer,
  "Approach axes": approachLayer,
};

/* Every overlay currently registered, including the dynamic VFR chart. */
function allOverlays() {
  const o = { ...OVERLAY_LAYERS };
  if (oaipLayer) o["VFR chart (OpenAIP)"] = oaipLayer;
  return o;
}

/* Apply saved/default state to the statically-registered overlays */
for (const [name, layer] of Object.entries(OVERLAY_LAYERS)) {
  if (overlayWanted(name)) layer.addTo(map);
  else map.removeLayer(layer);
}

/* Hide every overlay for a clean map (just the aircraft + its trail).
   Removing a layer fires 'overlayremove', which unchecks the layer-control
   box AND persists the new state, so a clean map survives reloads. */
function hideAllOverlays() {
  for (const layer of Object.values(allOverlays()))
    if (map.hasLayer(layer)) map.removeLayer(layer);
}

/* Put the overlays back to their first-run defaults. */
function restoreDefaultOverlays() {
  for (const [name, layer] of Object.entries(allOverlays())) {
    if (OVERLAY_DEFAULTS[name]) { if (!map.hasLayer(layer)) layer.addTo(map); }
    else if (map.hasLayer(layer)) map.removeLayer(layer);
  }
}

/* Persist every toggle from the layers control */
map.on("overlayadd", (e) => {
  ovState[e.name] = true;
  prefs.set("mapOverlays", JSON.stringify(ovState));
});
map.on("overlayremove", (e) => {
  ovState[e.name] = false;
  prefs.set("mapOverlays", JSON.stringify(ovState));
});

async function drawRunwayExtensions(icao) {
  approachLayer.clearLayers();
  try {
    const r = await fetch(`/api/airport/${icao}`);
    if (!r.ok) return;
    const { airport } = await r.json();
    for (const rw of airport.runways || []) {
      const brg = bearing(rw.le_lat, rw.le_lon, rw.he_lat, rw.he_lon);
      // One extension beyond each threshold, pointing away from the runway
      for (const [thr, out] of [
        [[rw.le_lat, rw.le_lon], (brg + 180) % 360],   // approach to LE end
        [[rw.he_lat, rw.he_lon], brg],                 // approach to HE end
      ]) {
        const end = destPoint(thr[0], thr[1], out, 10);
        L.polyline([thr, end], {
          color: "#1a9a50", weight: 2, opacity: 0.8, dashArray: "8 8",
        }).addTo(approachLayer);
        for (const d of [3, 5, 10]) {
          L.circleMarker(destPoint(thr[0], thr[1], out, d), {
            radius: 3, color: "#1a9a50", fillColor: "#fff",
            fillOpacity: 1, weight: 2,
          }).bindTooltip(`${d} NM`).addTo(approachLayer);
        }
      }
    }
  } catch { /* airport DB still loading: axes appear on next plan load */ }
}

/* Waypoint & traffic labels: hidden at very wide zooms so a dezoomed
   map doesn't turn into callsign soup. Toggled live on every zoom. */
function updateWpLabelVisibility() {
  document.body.classList.toggle("hide-wp-labels", map.getZoom() < 7);
  document.body.classList.toggle("hide-tfc-labels", map.getZoom() < 8);
}
map.on("zoomend", updateWpLabelVisibility);
updateWpLabelVisibility();

/* ================================== HSI =================================== */
const hsiCanvas = document.getElementById("hsi");
const hsiCtx = hsiCanvas.getContext("2d");

function drawHSI(hdg, bearingToWp) {
  const ctx = hsiCtx, W = hsiCanvas.width, cx = W / 2, cy = W / 2, R = W / 2 - 10;
  // Instrument colors follow the disc, which follows the theme:
  // day → fixed dark ticks on the light disc; night → the theme's own
  // font color (--text), so the compass matches the rest of the UI.
  const night = document.body.classList.contains("night");
  const fg = night
    ? (getComputedStyle(document.body).getPropertyValue("--text").trim() || "#d7e6f2")
    : "#000000";   // day (default theme): plain black for maximum contrast
  const acc = night ? "#35c8ff" : "#1d9fd6";

  ctx.clearRect(0, 0, W, W);
  ctx.save();
  ctx.translate(cx, cy);

  ctx.save();
  ctx.rotate((-hdg * Math.PI) / 180);
  ctx.strokeStyle = fg; ctx.fillStyle = fg;
  ctx.font = "bold 15px monospace"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
  for (let a = 0; a < 360; a += 10) {
    const rad = (a * Math.PI) / 180;
    const long = a % 30 === 0;
    ctx.beginPath();
    ctx.moveTo(Math.sin(rad) * R, -Math.cos(rad) * R);
    ctx.lineTo(Math.sin(rad) * (R - (long ? 12 : 6)), -Math.cos(rad) * (R - (long ? 12 : 6)));
    ctx.lineWidth = long ? 2 : 1;
    ctx.stroke();
    if (long) {
      const lbl = a % 90 === 0 ? "NESW"[a / 90] : String(a / 10);
      ctx.save();
      ctx.translate(Math.sin(rad) * (R - 24), -Math.cos(rad) * (R - 24));
      ctx.rotate(rad);
      ctx.fillText(lbl, 0, 0);
      ctx.restore();
    }
  }
  if (bearingToWp !== null) {
    const rad = (bearingToWp * Math.PI) / 180;
    ctx.strokeStyle = "#e13fd0"; ctx.lineWidth = 4; ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(Math.sin(rad) * (R - 30), -Math.cos(rad) * (R - 30));
    ctx.lineTo(Math.sin(rad + Math.PI) * (R - 55), -Math.cos(rad + Math.PI) * (R - 55));
    ctx.stroke();
  }
  ctx.restore();

  ctx.fillStyle = acc;
  ctx.beginPath();
  ctx.moveTo(0, -R + 2); ctx.lineTo(-7, -R + 14); ctx.lineTo(7, -R + 14);
  ctx.closePath(); ctx.fill();
  ctx.strokeStyle = fg; ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(0, -12); ctx.lineTo(0, 12);
  ctx.moveTo(-10, 0); ctx.lineTo(10, 0);
  ctx.stroke();
  ctx.restore();
}

/* =========================================================================
   VERTICAL PROFILE + TOD ALERT
   Planned altitude (SimBrief) against distance along the route, with the
   real aircraft position and the TOC / TOD markers.
   ========================================================================= */
const profileStrip = document.getElementById("profile-strip");
const profCanvas = document.getElementById("profile-canvas");

document.getElementById("btn-profile").addEventListener("click", () => {
  const show = profileStrip.classList.contains("hidden");
  profileStrip.classList.toggle("hidden", !show);
  document.getElementById("btn-profile").classList.toggle("active", show);
  if (show) drawProfile();
});

function drawProfile() {
  if (profileStrip.classList.contains("hidden") || !currentPlan) return;
  const dpr = window.devicePixelRatio || 1;
  const w = profCanvas.clientWidth, h = profCanvas.clientHeight;
  profCanvas.width = w * dpr; profCanvas.height = h * dpr;
  const ctx = profCanvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const css = getComputedStyle(document.body);
  const fg = css.getPropertyValue("--text"), mut = css.getPropertyValue("--muted");
  const wps = currentPlan.waypoints;
  const maxAlt = Math.max(currentPlan.cruise_alt_ft || 0,
                          ...wps.map((x) => x.alt_ft || 0), 1000) * 1.12;
  const PAD = { l: 44, r: 10, t: 8, b: 16 };
  const X = (d) => PAD.l + (d / planTotal) * (w - PAD.l - PAD.r);
  const Y = (a) => h - PAD.b - (a / maxAlt) * (h - PAD.t - PAD.b);

  // Altitude grid
  ctx.strokeStyle = mut; ctx.fillStyle = mut;
  ctx.font = "10px monospace"; ctx.lineWidth = 0.5; ctx.textAlign = "right";
  const step = maxAlt > 20000 ? 10000 : maxAlt > 8000 ? 5000 : 2000;
  for (let a = 0; a <= maxAlt; a += step) {
    ctx.globalAlpha = 0.4;
    ctx.beginPath(); ctx.moveTo(PAD.l, Y(a)); ctx.lineTo(w - PAD.r, Y(a)); ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillText(a >= 1000 ? (a / 1000) + "k" : a, PAD.l - 4, Y(a) + 3);
  }

  // Planned profile
  ctx.strokeStyle = "#e13fd0"; ctx.lineWidth = 2;
  ctx.beginPath();
  wps.forEach((wp, i) => {
    const x = X(planCum[i]), y = Y(wp.alt_ft || 0);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();

  // TOC / TOD markers
  ctx.textAlign = "center"; ctx.font = "bold 10px monospace";
  wps.forEach((wp, i) => {
    if (wp.ident === "TOC" || wp.ident === "TOD") {
      const x = X(planCum[i]);
      ctx.strokeStyle = "#c77800"; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(x, PAD.t); ctx.lineTo(x, h - PAD.b); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "#c77800";
      ctx.fillText(wp.ident, x, PAD.t + 9);
    }
  });

  // Real flown altitude: dashed orange, same color as the map trail.
  // While replaying, the curve shown is the REPLAYED flight's own track
  // (its altitudes + its projected distances); exiting replay redraws
  // with the live session data automatically.
  const srcN = replayMode ? rpPoints.length : trailData.length;
  const srcX = replayMode ? rpProfileX : profileX;
  const altAt = (i) => replayMode
    ? (rpPoints[i].alt_m ?? 0) / 0.3048
    : Math.max(0, trailData[i].alt);
  ctx.strokeStyle = "#ff9500"; ctx.lineWidth = 2; ctx.setLineDash([5, 4]);
  ctx.beginPath();
  let started = false;
  for (let i = 0; i < srcN; i++) {
    if (srcX[i] == null) continue;
    const x = X(srcX[i]), y = Y(altAt(i));
    started ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    started = true;
  }
  ctx.stroke();
  ctx.setLineDash([]);

  // Replay cursor on the profile (hollow orange ring)
  if (rpProfileXY) {
    ctx.strokeStyle = "#ff9500"; ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.arc(X(rpProfileXY[0]), Y(rpProfileXY[1]), 6, 0, Math.PI * 2);
    ctx.stroke();
  }

  // Real aircraft position (hidden while replaying another flight)
  if (!replayMode && lastState?.plan_progress) {
    const flown = Math.max(0, planTotal - lastState.plan_progress.dist_remaining_nm);
    const x = X(Math.min(flown, planTotal)), y = Y(lastState.alt_ft);
    ctx.fillStyle = "#ffd21f"; ctx.strokeStyle = "#1b1b1b"; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.arc(x, y, 5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
  }
  ctx.fillStyle = fg; ctx.textAlign = "left"; ctx.font = "10px monospace";
  ctx.fillText("NM →", w - PAD.r - 34, h - 4);
}

/* "TOD in X NM" alert in the flight plan bar */
function updateTodAlert(s) {
  const el = document.getElementById("plan-tod");
  const p = s.plan_progress;
  if (!p || !currentPlan || s.on_ground) { el.classList.add("hidden"); return; }
  const todIdx = currentPlan.waypoints.findIndex((w) => w.ident === "TOD");
  if (todIdx < 0) { el.classList.add("hidden"); return; }
  // Remaining route distance between the TOD and the destination:
  const todToDest = planTotal - planCum[todIdx];
  const distToTod = p.dist_remaining_nm - todToDest;
  if (distToTod > 0 && distToTod <= 30) {
    document.getElementById("tod-dist").textContent = Math.round(distToTod);
    el.classList.remove("hidden");
  } else el.classList.add("hidden");
}

/* ================================ ALERTS ================================== */
const alertBanner = document.getElementById("alert-banner");

function updateAlerts(state) {
  let msg = null;
  if (state.stall) msg = "⚠ STALL";
  else if (state.overspeed) msg = "⚠ OVERSPEED";
  if (msg) {
    alertBanner.textContent = msg;
    alertBanner.classList.remove("hidden");
  } else alertBanner.classList.add("hidden");
}

/* ======================= System bar: clock, chrono, chips ================= */
setInterval(() => {
  const n = new Date();
  document.getElementById("sys-zulu").textContent =
    String(n.getUTCHours()).padStart(2, "0") + ":" +
    String(n.getUTCMinutes()).padStart(2, "0") + ":" +
    String(n.getUTCSeconds()).padStart(2, "0") + "Z";
  if (chronoT0) {
    const el = Math.floor((Date.now() - chronoT0) / 1000);
    document.getElementById("sys-chrono").textContent =
      "CHR " + String(Math.floor(el / 60)).padStart(2, "0") + ":" +
      String(el % 60).padStart(2, "0");
  }
}, 1000);

/* Chrono: tap cycles start → pause → reset.
   Grey at zero, orange while running, green while paused. */
let chronoT0 = null, chronoStopped = false;
document.getElementById("sys-chrono").addEventListener("click", () => {
  const el = document.getElementById("sys-chrono");
  if (!chronoT0 && !chronoStopped) {            // start
    chronoT0 = Date.now();
    el.classList.add("run"); el.classList.remove("pause");
  } else if (chronoT0) {                        // pause
    chronoT0 = null; chronoStopped = true;
    el.classList.remove("run"); el.classList.add("pause");
  } else {                                      // reset
    chronoStopped = false;
    el.textContent = "CHR 00:00";
    el.classList.remove("run", "pause");
  }
});

function updateSystems(s) {
  const gear = document.getElementById("sys-gear");
  gear.classList.toggle("on", !!s.gear_down);
  gear.textContent = s.gear_down ? "GEAR ▼" : "GEAR";
  const fl = document.getElementById("sys-flaps");
  fl.classList.toggle("on", s.flaps_pct > 0);
  fl.textContent = s.flaps_pct > 0 ? `FLAPS ${s.flaps_pct}%` : "FLAPS";

  // Fuel: quantity + endurance; red below 45 min endurance
  const fuel = document.getElementById("sys-fuel");
  if (s.fuel_kg != null) {
    const end = s.endurance_min != null
      ? `${String(Math.floor(s.endurance_min / 60)).padStart(2, "0")}:${String(s.endurance_min % 60).padStart(2, "0")}`
      : "--:--";
    fuel.textContent = `FUEL ${Math.round(s.fuel_kg).toLocaleString("en-US")} kg · ${end}`;
    fuel.classList.toggle("low",
      s.endurance_min != null && s.endurance_min < 45 && !s.on_ground);
  }
}

/* Fuel on board at destination, checked against the SimBrief reserve */
function updateFob(s) {
  const el = document.getElementById("plan-fob");
  const p = s.plan_progress;
  if (!p || p.ete_min == null || s.fuel_kg == null || !s.fuel_flow_kgh) {
    el.classList.add("hidden"); return;
  }
  const fob = Math.round(s.fuel_kg - s.fuel_flow_kgh * p.ete_min / 60);
  const dest = document.getElementById("fob-dest");
  dest.textContent = fob.toLocaleString("en-US");
  const reserve = currentPlan?.fuel?.reserve_kg || 0;
  dest.classList.toggle("low", reserve > 0 && fob < reserve);
  el.classList.remove("hidden");
}

/* ============================== PFD (strip) =============================== */
function updatePFD(s) {
  document.getElementById("pfd-ias").textContent = Math.round(s.ias_kt);
  document.getElementById("pfd-gs").textContent = Math.round(s.gs_kt);
  document.getElementById("pfd-hdg").textContent =
    String(Math.round(s.hdg_deg)).padStart(3, "0");
  document.getElementById("pfd-alt").textContent =
    Math.round(s.alt_ft).toLocaleString("en-US");

  const vsEl = document.getElementById("pfd-vs");
  const vs = Math.round(s.vs_fpm / 50) * 50;
  vsEl.textContent = (vs > 0 ? "+" : "") + vs;
  vsEl.classList.toggle("climb", vs > 100);
  vsEl.classList.toggle("descend", vs < -100);

  document.getElementById("pfd-wind").textContent =
    `${String(Math.round(s.wind_dir_deg)).padStart(3, "0")}/${Math.round(s.wind_kt)}`;
  document.getElementById("pfd-oat").textContent = `${Math.round(s.oat_c)}°C`;
}

/* ================== Flight plan progress (dist / ETE) ===================== */
function updatePlanProgress(s) {
  const p = s.plan_progress;
  if (!p || !currentPlan) return null;
  document.getElementById("plan-next").textContent = p.next_wp ?? "---";
  document.getElementById("plan-dist").textContent = p.dist_remaining_nm ?? "---";
  document.getElementById("plan-ete").textContent =
    p.ete_min != null
      ? `${String(Math.floor(p.ete_min / 60)).padStart(2, "0")}:${String(p.ete_min % 60).padStart(2, "0")}`
      : "--:--";
  const wp = currentPlan.waypoints.find((w) => w.ident === p.next_wp);
  if (!wp) return null;
  // bearing() is TRUE (computed from coordinates) but the HSI rose is
  // MAGNETIC: convert using the sim's own variation (true − magnetic
  // heading), otherwise the needle is off by up to ~20° in high-variation
  // regions (North America, New Zealand…).
  const magvar = s.hdg_true_deg != null ? s.hdg_true_deg - s.hdg_deg : 0;
  return (bearing(s.lat, s.lon, wp.lat, wp.lon) - magvar + 360) % 360;
}

/* ========================================================================
   WEBSOCKET — real-time reception, automatic reconnection
   ======================================================================== */
let ws = null, lastState = null, reconnectDelay = 1000;
const connBanner = document.getElementById("conn-banner");
/* The yellow banner can be dismissed with its ✕: it stays hidden for a
   minute, then reappears on the next error event if the problem persists
   (status frames / reconnect attempts keep firing, so it self-restores). */
let connMutedUntil = 0;
function showConnBanner(text) {
  if (Date.now() < connMutedUntil) return;
  document.getElementById("conn-text").textContent = text;
  connBanner.classList.remove("hidden");
}
document.getElementById("conn-close").addEventListener("click", () => {
  connBanner.classList.add("hidden");
  connMutedUntil = Date.now() + 60000;
});

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    connBanner.classList.add("hidden");
    reconnectDelay = 1000;
  };

  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); }
    catch (_) { return; }   // malformed frame: drop it, keep the socket alive
    if (msg.type === "state") onState(msg.data);
    else if (msg.type === "flightplan") {
      if (msg.data) {
        drawFlightPlan(msg.data);
        if (!metarPanel.classList.contains("hidden")) loadMetars();
        if (!freqsPanel.classList.contains("hidden")) loadFreqs();
      } else {                   // free flight: plan unloaded server-side
        rpSessionPlan = null;    // a replay exit must not resurrect it
        clearPlanDisplay();
      }
    }
    else if (msg.type === "flightplan_error")
      document.getElementById("sb-status").textContent = msg.data.detail;
    else if (msg.type === "trail") setTrailFromServer(msg.data.points || []);
    else if (msg.type === "landing") showLandingCard(msg.data);
    else if (msg.type === "takeoff") { /* nothing: logbook fills at landing */ }
    else if (msg.type === "status" && !msg.data.connected) {
      setStatus("simlost", "SIM ?");
      showConnBanner(`MSFS not connected — ${msg.data.detail}`);
    }
  };

  ws.onclose = () => {
    setStatus("offline", "OFFLINE");
    showConnBanner("Server unreachable — reconnecting…");
    // Jitter so several tablets don't all retry in lockstep after a
    // server restart.
    setTimeout(connect, reconnectDelay * (0.75 + Math.random() * 0.5));
    reconnectDelay = Math.min(reconnectDelay * 1.6, 10000);
  };
  ws.onerror = () => ws.close();
}

function onState(s) {
  lastState = s;
  connBanner.classList.add("hidden");
  setStatus("ok", "MSFS");

  if (!replayMode) {
    aircraftMarker.setLatLng([s.lat, s.lon]);
    setAircraftHeading(s.hdg_true_deg ?? s.hdg_deg);
  }
  aircraftMarker.setPopupContent(
    `<b>${s.on_ground ? "On ground" : "Airborne"}</b><br>` +
    `Alt ${Math.round(s.alt_ft)} ft (AGL ${Math.round(s.alt_agl_ft)} ft)<br>` +
    `GS ${Math.round(s.gs_kt)} kt · V/S ${Math.round(s.vs_fpm)} fpm<br>` +
    `Wind ${Math.round(s.wind_dir_deg)}°T/${Math.round(s.wind_kt)} kt · ` +
    `${Math.round(s.oat_c)} °C · vis ${(s.visibility_m / 1000).toFixed(1)} km`
  );

  addTrailPoint(s.lat, s.lon, s.alt_ft, s.plan_progress?.dist_flown_nm);
  followAircraft(s);
  updatePFD(s);
  updateSystems(s);
  updateComState(s);
  updateFob(s);
  updateAlerts(s);
  updatePrediction(s);
  updateTodAlert(s);
  checkAirspaces(s);
  const brg = updatePlanProgress(s);
  drawHSI(s.hdg_deg, brg);
  drawProfile();
}

/* ============================= METAR panel ================================ */
const metarPanel = document.getElementById("metar-panel");
const metarContent = document.getElementById("metar-content");
const btnMetar = document.getElementById("btn-metar");
let metarTimer = null;

async function loadMetars(force = false) {
  const ids = currentPlan
    ? `${currentPlan.origin.icao},${currentPlan.destination.icao}` : "";
  metarContent.innerHTML = `<p class="hint">Loading METARs…</p>`;
  const params = new URLSearchParams();
  if (ids) params.set("ids", ids);
  if (force) params.set("force", "1");   // ⟳ = genuine upstream fetch
  const qs = params.toString();
  try {
    const r = await fetch(`/api/metar${qs ? "?" + qs : ""}`);
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "server error");
    renderMetars(data.metars || []);
    document.getElementById("metar-updated").textContent =
      "at " + new Date().toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  } catch (e) {
    metarContent.innerHTML = `<p class="metar-error">${esc(e.message)}</p>`;
  }
}

function renderMetars(metars) {
  if (!metars.length) {
    metarContent.innerHTML = `<p class="hint">No METAR published for these airports.</p>`;
    return;
  }
  const roleOf = (icao) => {
    if (!currentPlan) return "";
    if (icao === currentPlan.origin.icao) return "Departure";
    if (icao === currentPlan.destination.icao) return "Arrival";
    return "";
  };
  metarContent.innerHTML = metars.map((m) => {
    const cat = m.flt_cat || "UNK";
    let age = "";
    if (m.time) {
      const t = new Date(m.time.replace(" ", "T") + (m.time.endsWith("Z") ? "" : "Z"));
      const min = Math.max(0, Math.round((Date.now() - t.getTime()) / 60000));
      age = `${min} min ago`;
    }
    const parts = [];
    if (m.wind_kt != null) {
      const dir = m.wind_dir === "VRB" ? "VRB"
        : String(Math.round(m.wind_dir ?? 0)).padStart(3, "0") + "°";
      parts.push(`Wind ${dir}/${m.wind_kt} kt` + (m.gust_kt ? ` (gust ${m.gust_kt})` : ""));
    }
    if (m.visib != null) {
      const v = parseFloat(m.visib);
      parts.push("Vis " + (isNaN(v) ? m.visib + " SM"
        : (v >= 6 ? "≥ 10 km" : (v * 1.609).toFixed(1) + " km")));
    }
    if (m.temp_c != null) parts.push(`${Math.round(m.temp_c)} °C / dew point ${Math.round(m.dewp_c ?? 0)} °C`);
    if (m.altim != null) parts.push(`QNH ${Math.round(m.altim)} hPa`);

    return `<div class="metar-card">
      <div class="metar-title">
        <b>${esc(m.icao)}</b>
        <span class="flt-badge flt-${cat}">${cat}</span>
        <span class="role">${roleOf(m.icao)}</span>
        <span class="metar-age">${age}</span>
      </div>
      <div class="metar-raw">${esc(m.raw || "")}</div>
      <div class="metar-decoded">${esc(parts.join(" · "))}</div>
    </div>`;
  }).join("");
}

function openMetarPanel(open) {
  if (open) closePanels("metar-panel");
  metarPanel.classList.toggle("hidden", !open);
  btnMetar.classList.toggle("active", open);
  clearInterval(metarTimer);
  if (open) {
    loadMetars();
    metarTimer = setInterval(loadMetars, 5 * 60 * 1000);
  }
}
btnMetar.addEventListener("click", () =>
  openMetarPanel(metarPanel.classList.contains("hidden")));
document.getElementById("metar-close").addEventListener("click", () => openMetarPanel(false));
document.getElementById("metar-refresh").addEventListener("click",
  () => loadMetars(true));

/* ========================= FREQUENCIES panel ==============================
   Departure/arrival airport frequencies (ATIS, Delivery, Ground, Tower,
   Approach…) + sequence of FIRs (centers) along the route.                */
const freqsPanel = document.getElementById("freqs-panel");
const freqsContent = document.getElementById("freqs-content");
const btnFreqs = document.getElementById("btn-freqs");

async function loadFreqs() {
  freqsContent.innerHTML = `<p class="hint">Loading…</p>`;
  try {
    const r = await fetch("/api/freqs");
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "server error");
    const roleOf = (icao) => {
      if (!currentPlan) return "";
      if (icao === currentPlan.origin.icao) return " · Departure";
      if (icao === currentPlan.destination.icao) return " · Arrival";
      return "";
    };
    // Current COM1 state — the element is refreshed live on every frame
    // (see updateComState), no manual panel refresh needed.
    let html = `<div class="com-state" id="com-state">COM1 ---.---</div>`;
    html += (data.airports || []).map((ap) => {
      const rows = (ap.freqs || []).map((f) =>
        `<div class="freq-row tappable" data-mhz="${f.mhz}">
           <span class="freq-type">${esc(f.type)}</span>
           <span class="freq-mhz">${f.mhz.toFixed(3)}</span>
           <span class="freq-desc">${esc(f.desc)}</span>
           <span class="freq-set">SET</span>
         </div>`).join("") ||
        `<p class="hint">No frequencies on record for this airport.</p>`;
      return `<div class="freq-card">
        <h4><b>${esc(ap.icao)}</b> ${esc(ap.name)}${roleOf(ap.icao)}<a class="charts-btn" href="https://chartfox.org/${encodeURIComponent(ap.icao)}" target="_blank" rel="noopener" title="Open charts on ChartFox">Charts</a></h4>${rows}
      </div>`;
    }).join("");

    if ((data.firs || []).length) {
      html += `<div class="freq-card">
        <h4>FIRs (centers) along the route</h4>
        <p class="fir-seq">${data.firs.map((f) => `<b>${esc(f)}</b>`).join(" → ")}</p>
        <p class="hint">Center frequencies vary by sector and altitude:
        check the online controller (VATSIM/IVAO) or the en-route charts
        for the active sector frequency.</p>
      </div>`;
    }
    const srcNote = `<p class="hint" style="margin-top:10px">Source:
      OurAirports (community database). Frequencies may differ slightly
      from your ATC software (BeyondATC, SayIntentions, VATSIM/IVAO…) or
      the sim's own navdata — when in doubt, the frequency shown by your
      ATC tool is the one to tune.</p>`;
    freqsContent.innerHTML = (html || `<p class="hint">No data.</p>`) + srcNote;
  } catch (e) {
    freqsContent.innerHTML = `<p class="metar-error">${esc(e.message)}</p>`;
  }
}
/* Live COM1 line in the Frequencies panel: updated on every state frame,
   so tapping a frequency shows the change instantly. */
function updateComState(s) {
  const el = document.getElementById("com-state");
  if (!el || s.com1_active == null) return;
  el.innerHTML = `COM1 <b>${s.com1_active.toFixed(3)}</b>
    · STBY ${s.com1_stby?.toFixed(3) ?? "---.---"}
    <span class="hint"> — tap a frequency below to set standby</span>`;
}

function openFreqsPanel(open) {
  if (open) closePanels("freqs-panel");
  freqsPanel.classList.toggle("hidden", !open);
  btnFreqs.classList.toggle("active", open);
  if (open) loadFreqs();
}
btnFreqs.addEventListener("click", () =>
  openFreqsPanel(freqsPanel.classList.contains("hidden")));
document.getElementById("freqs-close").addEventListener("click", () => openFreqsPanel(false));
document.getElementById("freqs-refresh").addEventListener("click", loadFreqs);

/* Tap a frequency → COM1 standby in the sim */
freqsContent.addEventListener("click", async (ev) => {
  const row = ev.target.closest(".freq-row.tappable");
  if (!row) return;
  const mhz = parseFloat(row.dataset.mhz);
  if (!(mhz >= 118 && mhz <= 137)) {
    showToast(`${mhz.toFixed(3)} is outside the COM band`, "error");
    return;
  }
  try {
    const r = await fetch("/api/radio", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mhz, com: 1 }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error);
    showToast(`COM1 standby set to ${mhz.toFixed(3)} — swap when ready`, "success");
  } catch (e) {
    showToast(e.message || "Radio not set", "error");
  }
});

/* ======================= Landing rating + LOGBOOK ========================= */
const landingCard = document.getElementById("landing-card");
let lcTimer = null;
let lcDlTimer = null;   // pending flight-card auto-download

function downloadFlightCard(fid) {
  const a = document.createElement("a");
  a.href = `/api/flight/${fid}/card.png`;
  a.download = `${fid}.png`;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function showLandingCard(e) {
  document.getElementById("lc-rating").textContent = e.rating;
  document.getElementById("lc-vs").textContent = e.touchdown_fpm;
  document.getElementById("lc-detail").textContent =
    `${e.g_force} G · ${e.ias_kt} kt` +
    (e.destination ? ` · ${e.destination.ident}` : "");
  const col = { "BUTTER": "#1a9a50", "SMOOTH": "#38b06a", "GOOD": "#0a6fc1",
                "FIRM": "#c77800", "HARD": "#d1273a", "CRASH?": "#7a1020" };
  document.getElementById("lc-rating").style.color = col[e.rating] || "";
  // Newly unlocked badges: celebrate them right on the landing card
  const badgesBox = document.getElementById("lc-badges");
  if (e.new_badges && e.new_badges.length) {
    badgesBox.innerHTML =
      `<div class="lc-badge-hint">🎉 New achievement${e.new_badges.length > 1 ? "s" : ""}!</div>` +
      e.new_badges.map((b) =>
        `<div class="lc-badge tier-${b.tier}">${b.emoji} <b>${esc(b.name)}</b></div>`
      ).join("");
    badgesBox.classList.remove("hidden");
  } else {
    badgesBox.classList.add("hidden");
    badgesBox.innerHTML = "";
  }

  // Flight card: offer (and attempt) the download right at landing
  const lcCard = document.getElementById("lc-card");
  clearTimeout(lcDlTimer);   // a quick touch-and-go must not download the previous card
  if (e.track_id) {
    lcCard.classList.remove("hidden");
    lcCard.onclick = () => downloadFlightCard(e.track_id);
    // Auto-prompt: best effort — some browsers require the button tap
    lcDlTimer = setTimeout(
      () => { try { downloadFlightCard(e.track_id); } catch (_) {} }, 800);
  } else {
    lcCard.classList.add("hidden");
  }
  landingCard.classList.remove("hidden");
  clearTimeout(lcTimer);
  lcTimer = setTimeout(() => landingCard.classList.add("hidden"), 20000);
}
document.getElementById("lc-close").addEventListener("click", () =>
  landingCard.classList.add("hidden"));

const logbookPanel = document.getElementById("logbook-panel");
const btnLogbook = document.getElementById("btn-logbook");

async function loadLogbook() {
  const c = document.getElementById("logbook-content");
  c.innerHTML = `<p class="hint">Loading…</p>`;
  try {
    const r = await fetch("/api/logbook");
    const { entries } = await r.json();
    if (!entries?.length) {
      c.innerHTML = `<p class="hint">No flights recorded yet. The logbook
        fills automatically at every landing.</p>`;
      return;
    }
    // Summary statistics computed from all entries
    const totMin = entries.reduce((a, e) => a + (e.duration_min || 0), 0);
    const totNm = entries.reduce((a, e) => a + (e.dist_nm || 0), 0);
    const apts = new Set();
    entries.forEach((e) => {
      if (e.origin?.ident) apts.add(e.origin.ident);
      if (e.destination?.ident) apts.add(e.destination.ident);
    });
    const tds = entries.map((e) => Math.abs(e.touchdown_fpm || 0)).filter(Boolean);
    const avgTd = tds.length ? Math.round(tds.reduce((a, b) => a + b) / tds.length) : 0;
    const bestTd = tds.length ? Math.min(...tds) : 0;
    // Achievements summary card at the top of the logbook
    let achHead = "";
    try {
      const r = await fetch("/api/achievements");
      const data = await r.json();
      const recent = data.badges.filter((b) => b.unlocked).slice(-3).reverse();
      achHead = `<div class="ach-summary">
        <div class="ach-summary-title">
          <b>🏆 Achievements</b>
          <span>${data.unlocked} / ${data.total} unlocked</span>
          <button id="ach-open" class="mini-btn">View all</button>
        </div>
        ${recent.length ? `<div class="ach-summary-recent">${recent.map((b) =>
          `<span class="ach-chip tier-${b.tier}" title="${esc(b.name)}">${b.emoji} ${esc(b.name)}</span>`
        ).join("")}</div>` : `<p class="hint">Fly to unlock your first badge!</p>`}
      </div>`;
    } catch (_) { /* silent: logbook still works without the summary */ }

    const statsHtml = `<div class="log-stats">
      <div class="log-stat"><div class="v">${entries.length}</div><div class="l">Flights</div></div>
      <div class="log-stat"><div class="v">${(totMin / 60).toFixed(1)}h</div><div class="l">Total time</div></div>
      <div class="log-stat"><div class="v">${Math.round(totNm).toLocaleString("en-US")}</div><div class="l">NM flown</div></div>
      <div class="log-stat"><div class="v">${apts.size}</div><div class="l">Airports</div></div>
      <div class="log-stat"><div class="v">${avgTd}</div><div class="l">Avg fpm</div></div>
      <div class="log-stat"><div class="v">${bestTd}</div><div class="l">Best fpm</div></div>
    </div>`;

    c.innerHTML = achHead + statsHtml + entries.map((e) => {
      const badge = (e.rating || "").replace("?", "");
      return `<div class="log-entry">
        <div class="log-head">
          <b>${esc(e.origin?.ident || "????")} → ${esc(e.destination?.ident || "????")}</b>
          <span class="log-badge lb-${badge}">${esc(e.rating)}</span>
          <span class="log-date">${esc(e.date)}</span>
        </div>
        <div class="log-detail">
          ${e.duration_min} min · ${e.dist_nm} NM ·
          touchdown ${e.touchdown_fpm} fpm · ${e.g_force} G · ${e.ias_kt} kt
        </div>
        ${e.track_id ? `<div class="log-actions">
          <button class="log-replay" data-fid="${esc(e.track_id)}">▶ Replay</button>
          <a class="log-gpx" href="/api/flight/${esc(e.track_id)}/gpx" download>GPX</a>
          <a class="log-gpx" href="/api/flight/${esc(e.track_id)}/card.png" download>Card</a>
        </div>` : ""}
      </div>`;
    }).join("");
  } catch {
    c.innerHTML = `<p class="metar-error">Logbook unavailable.</p>`;
  }
}
function openAchievements(open) {
  if (open) {
    closePanels("ach-panel");
    achPanel.classList.remove("hidden");
    // Achievements is the logbook's sub-panel: keep 📓 lit so an open
    // panel always has its toolbar button highlighted.
    btnLogbook.classList.add("active");
    loadAchievements();
  } else {
    achPanel.classList.add("hidden");
    btnLogbook.classList.remove("active");
  }
}

async function loadAchievements() {
  const box = document.getElementById("ach-content");
  box.innerHTML = `<p class="hint">Loading…</p>`;
  try {
    const r = await fetch("/api/achievements");
    const { unlocked, total, badges } = await r.json();
    document.getElementById("ach-count").textContent =
      `— ${unlocked} / ${total} unlocked`;
    const sorted = badges.slice().sort((a, b) => {
      if (a.unlocked !== b.unlocked) return b.unlocked - a.unlocked;
      const pa = a.progress / a.target, pb = b.progress / b.target;
      return pb - pa;                                // closest first
    });
    box.innerHTML = sorted.map((b) => {
      const pct = Math.min(100, 100 * b.progress / b.target);
      return `<div class="ach-row tier-${b.tier} ${b.unlocked ? "on" : "off"}">
        <div class="ach-emoji">${b.emoji}</div>
        <div class="ach-body">
          <div class="ach-name">${esc(b.name)}${b.unlocked_on
            ? ` <span class="ach-date">${esc(b.unlocked_on)}</span>` : ""}</div>
          <div class="ach-desc">${esc(b.description)}</div>
          <div class="ach-bar"><span style="width:${pct}%"></span></div>
          <div class="ach-progress">${b.progress} / ${b.target}</div>
        </div>
      </div>`;
    }).join("");
  } catch {
    box.innerHTML = `<p class="hint">Could not load achievements.</p>`;
  }
}

// ✕ goes BACK to the logbook (you drilled in from there), mirroring the
// "View all" step; tap-outside still drops straight back to the map.
document.getElementById("ach-close").addEventListener("click", () => {
  openAchievements(false);
  openLogbook(true);
});
document.getElementById("ach-refresh").addEventListener("click", loadAchievements);
// The "View all" button is inside dynamic logbook content: event delegation
document.getElementById("logbook-content").addEventListener("click", (ev) => {
  if (ev.target.id === "ach-open") openAchievements(true);
});

function openLogbook(open) {
  if (open) closePanels("logbook-panel");
  logbookPanel.classList.toggle("hidden", !open);
  btnLogbook.classList.toggle("active", open);
  if (open) loadLogbook();
}
btnLogbook.addEventListener("click", () =>
  openLogbook(logbookPanel.classList.contains("hidden")));
document.getElementById("logbook-close").addEventListener("click", () => openLogbook(false));
document.getElementById("logbook-refresh").addEventListener("click", loadLogbook);

/* Replay of an archived flight from the logbook */
document.getElementById("logbook-content").addEventListener("click", async (ev) => {
  const btn = ev.target.closest(".log-replay");
  if (!btn) return;
  btn.disabled = true;
  try {
    const r = await fetch(`/api/flight/${btn.dataset.fid}/track.json`);
    if (!r.ok) throw new Error();
    const { points, plan } = await r.json();
    openLogbook(false);            // close the logbook, hand over to replay
    // Self-contained replay: use the plan archived WITH the flight
    // (route + profile), and restore the session plan afterwards.
    if (plan) {
      if (!rpPlanSwapped) { rpSessionPlan = currentPlan; rpPlanSwapped = true; }
      drawFlightPlan(plan);
    }
    startReplay(points);
  } catch {
    showToast("Track for this flight not found.", "error");
    btn.disabled = false;
  }
});

/* ================================ REPLAY ==================================
   Replays the server-recorded track: time slider + accelerated playback
   (×30). The orange "ghost" aircraft travels along the flight.            */
let replayMode = false, rpPoints = [], rpIdx = 0, rpTimer = null;
/* Playback position as a shared float: the play loop advances it, and a
   manual seek (slider) re-syncs it, so playback continues seamlessly
   from the new spot instead of fighting a stale local counter. */
let rpPos = 0;
/* Plan swap while replaying an archived flight: the flight's own plan
   snapshot replaces the session plan for the duration of the replay,
   then the session configuration is restored on exit. */
let rpPlanSwapped = false, rpSessionPlan = null;

function clearPlanDisplay() {
  currentPlan = null; planCum = []; planTotal = 0;
  planLayer.clearLayers();
  approachLayer.clearLayers();
  document.getElementById("plan-bar").classList.add("hidden");
  document.body.classList.remove("has-plan");
  rebuildProfileX();
  drawProfile();
}
const replayBar = document.getElementById("replay-bar");
const rpSlider = document.getElementById("rp-slider");
const btnReplay = document.getElementById("btn-replay");
const ghost = L.circleMarker([0, 0], {
  radius: 8, color: "#1b1b1b", fillColor: "#ff9500", fillOpacity: 1, weight: 2,
});

let rpPath = null;   // path of the replayed flight

function startReplay(points) {
  if (!points || points.length < 2) {
    showToast("Not enough recorded track for a replay.", "error");
    return;
  }
  if (rpTimer) { clearInterval(rpTimer); rpTimer = null; }
  document.getElementById("rp-play").textContent = "▶";
  rpPoints = points;
  // Exact recorded route distances when present (flights archived from
  // v1.2.1); projection fallback for older archives.
  rpProfileX = points.some((p) => p.x != null)
    ? points.map((p) => p.x ?? null)
    : projectTrackXs(points);
  replayMode = true;
  document.body.classList.add("replaying");
  btnReplay.classList.add("active");
  replayBar.classList.remove("hidden");
  rpSlider.max = points.length - 1;
  rpSlider.value = 0;
  rpPos = 0;
  // Full path of the replayed flight (under the ghost aircraft)
  if (rpPath) map.removeLayer(rpPath);
  rpPath = L.polyline(points.map((p) => [p.lat, p.lon]),
    { color: "#ff9500", weight: 3, opacity: 0.55, dashArray: "4 5" }).addTo(map);
  ghost.addTo(map);
  setReplayIndex(0);
  map.fitBounds(rpPath.getBounds(), { padding: [40, 40] });
}

/* Toolbar ▶ button: replay of the current session */
async function enterReplay() {
  try {
    const r = await fetch("/api/track.json");
    const { points } = await r.json();
    startReplay(points);
  } catch { showToast("Track unavailable.", "error"); }
}

function exitReplay() {
  replayMode = false;
  clearInterval(rpTimer); rpTimer = null;
  document.getElementById("rp-play").textContent = "▶";
  btnReplay.classList.remove("active");
  replayBar.classList.add("hidden");
  document.body.classList.remove("replaying");
  rpProfileXY = null;
  rpProfileX = [];
  if (rpPlanSwapped) {
    rpPlanSwapped = false;
    const back = rpSessionPlan;
    rpSessionPlan = null;
    if (back) drawFlightPlan(back);
    else clearPlanDisplay();
  }
  drawProfile();
  map.removeLayer(ghost);
  if (rpPath) { map.removeLayer(rpPath); rpPath = null; }
  if (lastState) {
    aircraftMarker.setLatLng([lastState.lat, lastState.lon]);
    followAircraft(lastState);
  }
}

let rpProfileXY = null;   // [distance NM, altitude ft] of the replay cursor
let rpProfileX = [];      // per-point route distance, precomputed monotonic

function setReplayIndex(i) {
  rpIdx = Math.max(0, Math.min(rpPoints.length - 1, Math.round(i)));
  const p = rpPoints[rpIdx];
  ghost.setLatLng([p.lat, p.lon]);
  const fx = rpProfileX[rpIdx];
  rpProfileXY = fx != null ? [fx, p.alt_m / 0.3048] : null;
  drawProfile();
  const t = new Date(p.ts * 1000);
  document.getElementById("rp-info").textContent =
    t.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) +
    ` · ${Math.round(p.alt_m / 0.3048)} ft`;
  rpSlider.value = rpIdx;
}

btnReplay.addEventListener("click", () => replayMode ? exitReplay() : enterReplay());
document.getElementById("rp-exit").addEventListener("click", exitReplay);
rpSlider.addEventListener("input", () => {
  setReplayIndex(+rpSlider.value);
  rpPos = rpIdx;          // playback (if running) resumes from here
});
document.getElementById("rp-play").addEventListener("click", () => {
  const btn = document.getElementById("rp-play");
  if (rpTimer) {                       // pause
    clearInterval(rpTimer); rpTimer = null; btn.textContent = "▶"; return;
  }
  btn.textContent = "⏸";
  // Points are ~2 s apart → ×30 ≈ 1.5 points / 100 ms.
  // Restart from the beginning when play is pressed at the very end.
  rpPos = rpIdx >= rpPoints.length - 1 ? 0 : rpIdx;
  rpTimer = setInterval(() => {
    rpPos += 1.5;
    if (rpPos >= rpPoints.length - 1) {
      rpPos = rpPoints.length - 1;
      clearInterval(rpTimer); rpTimer = null; btn.textContent = "▶";
    }
    setReplayIndex(rpPos);
  }, 100);
});

/* ============================ Tools / buttons ============================= */
const btnNight = document.getElementById("btn-night");
function applyNight(on) {
  document.body.classList.toggle("night", on);
  btnNight.classList.toggle("active", on);
  // ALWAYS repaint the compass: it was only redrawn when sim data was
  // present, so toggling the theme with MSFS closed kept the previous
  // theme's colors on the canvas — the compass looked broken in
  // whichever mode wasn't active at page load.
  drawHSI(lastState ? lastState.hdg_deg : 0, null);
  drawProfile();
}
/* Dark by default; the ☾ toggle persists the user's choice per device */
const savedNight = prefs.get("night", null);
applyNight(savedNight === null ? true : savedNight === "1");
btnNight.addEventListener("click", () => {
  const on = !document.body.classList.contains("night");
  applyNight(on);
  prefs.set("night", on ? "1" : "0");
});

document.getElementById("btn-fullscreen").addEventListener("click", () => {
  if (!document.fullscreenElement) document.documentElement.requestFullscreen?.();
  else document.exitFullscreen?.();
});
// Reflect the real state (like every other mode button), including exits
// via Esc or a system gesture.
document.addEventListener("fullscreenchange", () => {
  document.getElementById("btn-fullscreen")
    .classList.toggle("active", !!document.fullscreenElement);
});

const settings = document.getElementById("settings");
function openSettings(open) {
  if (open) closePanels("settings");
  settings.classList.toggle("hidden", !open);
  document.getElementById("btn-settings").classList.toggle("active", open);
}
document.getElementById("btn-settings").addEventListener("click", () =>
  openSettings(settings.classList.contains("hidden")));
document.getElementById("settings-close").addEventListener("click", () =>
  openSettings(false));
document.getElementById("settings-x").addEventListener("click", () =>
  openSettings(false));

/* Clean-map mode: one toolbar toggle hides every UI chrome for a
   distraction-free map. The toggle itself stays in place and turns blue
   (.active) like the other mode buttons; press it again (or Escape) to
   bring everything back. Session-only (not persisted) so a page reload
   never leaves the pilot staring at a blank map with no way back. */
const btnHideUI = document.getElementById("btn-hideui");
function setHideUI(on) {
  if (on) closePanels();          // don't strand a panel under the hidden UI
  document.body.classList.toggle("hide-ui", on);
  btnHideUI.classList.toggle("active", on);
}
btnHideUI.addEventListener("click", () =>
  setHideUI(!document.body.classList.contains("hide-ui")));

/* Escape closes whatever panel is open, or exits clean-map mode first
   (desktop / second-screen use; tablets keep the tap-outside gesture). */
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  if (document.body.classList.contains("hide-ui")) setHideUI(false);
  else closePanels();
});

/* SimBrief — the ID is stored server-side (.env): pre-filled from
   /api/config, and any change made on one device is persisted and
   shared across the whole installation. */
const sbInput = document.getElementById("sb-id");

async function loadConfig() {
  try {
    const r = await fetch("/api/config");
    const cfg = await r.json();
    if (cfg.simbrief_id) sbInput.value = cfg.simbrief_id;
    if (cfg.openaip_key && cfg.openaip_key !== prefs.get("oaipKey", "")) {
      // Server is the source of truth: sync locally (the tile layer is
      // built client-side, so the key must also live in localStorage)
      prefs.set("oaipKey", cfg.openaip_key);
      oaipInput.value = cfg.openaip_key;
      if (oaipLayer) { map.removeLayer(oaipLayer); layerControl.removeLayer(oaipLayer); }
      oaipLayer = buildOpenAipLayer();
      if (oaipLayer) {
        layerControl.addOverlay(oaipLayer, "VFR chart (OpenAIP)");
        if (overlayWanted("VFR chart (OpenAIP)")) oaipLayer.addTo(map);
      }
      refreshAirspaces();
    }
    // OpenAIP key: server-side value wins and is mirrored locally so the
    // tile layer and airspaces work immediately on every device
    document.getElementById("cfg-url").textContent = cfg.url;
    document.getElementById("cfg-version").textContent = "v" + cfg.version;
    document.getElementById("cfg-hz").textContent = cfg.update_hz;
  } catch { /* server not ready yet: defaults apply */ }
}
loadConfig();

document.getElementById("sb-load").addEventListener("click", async () => {
  const id = sbInput.value.trim();
  if (!id) return;
  document.getElementById("sb-status").textContent = "Loading plan…";
  try {
    const r = await fetch("/api/simbrief", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error();
    if (data.plan_loaded) {
      showToast("SimBrief plan loaded.", "success");
    } else {
      showToast("ID saved, but no plan retrieved: generate a plan on " +
                "simbrief.com and check the PC's Internet access.", "error");
      document.getElementById("sb-status").textContent =
        "ID saved — no plan found for now.";
    }
  } catch {
    showToast("Failed to save the SimBrief ID.", "error");
    document.getElementById("sb-status").textContent =
      "Failed: server unreachable.";
  }
});

/* Auto-zoom checkbox: centering is kept either way (see followAircraft) */
const chkAutoZoom = document.getElementById("chk-autozoom");
chkAutoZoom.checked = prefs.get("autoZoom", "1") === "1";
autoZoom = chkAutoZoom.checked;
chkAutoZoom.addEventListener("change", () => {
  autoZoom = chkAutoZoom.checked;
  prefs.set("autoZoom", autoZoom ? "1" : "0");
  if (autoZoom && lastState) followAircraft(lastState);   // re-apply now
});

/* Free flight: unload the SimBrief plan (server-side, syncs all screens) */
document.getElementById("btn-freeflight").addEventListener("click", async () => {
  try {
    const r = await fetch("/api/plan/clear", { method: "POST" });
    if (!r.ok) throw new Error();
    showToast("Free flight — plan unloaded.", "success");
  } catch {
    showToast("Could not unload the plan.", "error");
  }
});

/* Interface scale slider: live preview, persisted per device */
const scaleInput = document.getElementById("ui-scale");
function applyScale(pct) {
  document.documentElement.style.setProperty("--ui-scale", pct / 100);
  document.getElementById("ui-scale-val").textContent = pct + "%";
}
scaleInput.value = prefs.get("uiScale", "100");
applyScale(+scaleInput.value);
scaleInput.addEventListener("input", () => {
  applyScale(+scaleInput.value);
  prefs.set("uiScale", scaleInput.value);
});

/* ------------------- Keep screen awake (anti-sleep) ----------------------
   The Wake Lock API needs a secure context (HTTPS); this app usually runs
   over plain HTTP on the LAN, so a NoSleep-style fallback is included:
   an invisible 2×2 looping video keeps mobile screens on. */
const NOSLEEP_MP4 = "data:video/mp4;base64,AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAOzbW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAA+gAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAt10cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAA+gAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAIAAAACAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAAPoAAAIAAABAAAAAAJVbWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAAAoAAAAKABVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAACAG1pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAAcBzdGJsAAAAwHN0c2QAAAAAAAAAAQAAALBhdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAIAAgBIAAAASAAAAAAAAAABFUxhdmM2MC4zMS4xMDIgbGlieDI2NAAAAAAAAAAAAAAAGP//AAAANmF2Y0MBZAAK/+EAGWdkAAqs2V+IiMBEAAADAAQAAAMAUDxIllgBAAZo6+PLIsD9+PgAAAAAEHBhc3AAAAABAAAAAQAAABRidHJ0AAAAAAAAGfgAABn4AAAAGHN0dHMAAAAAAAAAAQAAAAoAAAQAAAAAFHN0c3MAAAAAAAAAAQAAAAEAAABgY3R0cwAAAAAAAAAKAAAAAQAACAAAAAABAAAUAAAAAAEAAAgAAAAAAQAAAAAAAAABAAAEAAAAAAEAABQAAAAAAQAACAAAAAABAAAAAAAAAAEAAAQAAAAAAQAACAAAAAAcc3RzYwAAAAAAAAABAAAAAQAAAAoAAAABAAAAPHN0c3oAAAAAAAAAAAAAAAoAAALFAAAADAAAAAwAAAAMAAAADAAAABIAAAAOAAAADAAAAAwAAAASAAAAFHN0Y28AAAAAAAAAAQAAA+MAAABidWR0YQAAAFptZXRhAAAAAAAAACFoZGxyAAAAAAAAAABtZGlyYXBwbAAAAAAAAAAAAAAAAC1pbHN0AAAAJal0b28AAAAdZGF0YQAAAAEAAAAATGF2ZjYwLjE2LjEwMAAAAAhmcmVlAAADR21kYXQAAAKuBgX//6rcRem95tlIt5Ys2CDZI+7veDI2NCAtIGNvcmUgMTY0IHIzMTA4IDMxZTE5ZjkgLSBILjI2NC9NUEVHLTQgQVZDIGNvZGVjIC0gQ29weWxlZnQgMjAwMy0yMDIzIC0gaHR0cDovL3d3dy52aWRlb2xhbi5vcmcveDI2NC5odG1sIC0gb3B0aW9uczogY2FiYWM9MSByZWY9MyBkZWJsb2NrPTE6MDowIGFuYWx5c2U9MHgzOjB4MTEzIG1lPWhleCBzdWJtZT03IHBzeT0xIHBzeV9yZD0xLjAwOjAuMDAgbWl4ZWRfcmVmPTEgbWVfcmFuZ2U9MTYgY2hyb21hX21lPTEgdHJlbGxpcz0xIDh4OGRjdD0xIGNxbT0wIGRlYWR6b25lPTIxLDExIGZhc3RfcHNraXA9MSBjaHJvbWFfcXBfb2Zmc2V0PS0yIHRocmVhZHM9MSBsb29rYWhlYWRfdGhyZWFkcz0xIHNsaWNlZF90aHJlYWRzPTAgbnI9MCBkZWNpbWF0ZT0xIGludGVybGFjZWQ9MCBibHVyYXlfY29tcGF0PTAgY29uc3RyYWluZWRfaW50cmE9MCBiZnJhbWVzPTMgYl9weXJhbWlkPTIgYl9hZGFwdD0xIGJfYmlhcz0wIGRpcmVjdD0xIHdlaWdodGI9MSBvcGVuX2dvcD0wIHdlaWdodHA9MiBrZXlpbnQ9MjUwIGtleWludF9taW49MTAgc2NlbmVjdXQ9NDAgaW50cmFfcmVmcmVzaD0wIHJjX2xvb2thaGVhZD00MCByYz1jcmYgbWJ0cmVlPTEgY3JmPTIzLjAgcWNvbXA9MC42MCBxcG1pbj0wIHFwbWF4PTY5IHFwc3RlcD00IGlwX3JhdGlvPTEuNDAgYXE9MToxLjAwAIAAAAAPZYiEABH//veIHzLLb5x5AAAACEGaJGxBD/7gAAAACEGeQniHf7eBAAAACAGeYXRDf7qAAAAACAGeY2pDf7qBAAAADkGaaEmoQWiZTAh3//7hAAAACkGehkURLDv/t4EAAAAIAZ6ldEN/uoEAAAAIAZ6nakN/uoAAAAAOQZqpSahBbJlMCG///uA=";
let wakeLock = null, noSleepVideo = null;

async function setKeepAwake(on) {
  prefs.set("keepAwake", on ? "1" : "0");
  if (!on) {
    try { await wakeLock?.release(); } catch (_) {}
    wakeLock = null;
    if (noSleepVideo) { noSleepVideo.pause(); noSleepVideo.remove(); noSleepVideo = null; }
    return;
  }
  let native = false;
  if ("wakeLock" in navigator) {
    try {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeLock.addEventListener("release", () => { wakeLock = null; });
      native = true;
    } catch (_) { /* refused or insecure context → fallback below */ }
  }
  if (!native) {
    noSleepVideo = document.createElement("video");
    noSleepVideo.setAttribute("playsinline", "");
    noSleepVideo.muted = true;
    noSleepVideo.loop = true;
    noSleepVideo.src = NOSLEEP_MP4;
    noSleepVideo.style.cssText =
      "position:absolute;width:1px;height:1px;opacity:0;pointer-events:none";
    document.body.appendChild(noSleepVideo);
    // Some browsers stop looping tiny videos: nudge it back
    noSleepVideo.addEventListener("timeupdate", () => {
      if (noSleepVideo && noSleepVideo.currentTime > 0.6)
        noSleepVideo.currentTime = 0.05;
    });
    try { await noSleepVideo.play(); } catch (_) {
      showToast("Tap the screen once to enable anti-sleep.", "error");
    }
  }
  showToast("Screen will stay awake" + (native ? "." : " (fallback mode)."),
            "success");
}

const chkAwake = document.getElementById("chk-awake");
chkAwake.checked = prefs.get("keepAwake", "0") === "1";
if (chkAwake.checked) setKeepAwake(true);
chkAwake.addEventListener("change", () => setKeepAwake(chkAwake.checked));

/* The native lock is released when the tab goes to background:
   re-acquire it on return */
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" &&
      prefs.get("keepAwake", "0") === "1" && !wakeLock && !noSleepVideo)
    setKeepAwake(true);
});

/* Online network checkboxes (VATSIM and/or IVAO) */
const old = prefs.get("network", null);          // migrate the old selector
if (old === "vatsim") prefs.set("netVatsim", "1");
if (old === "ivao") prefs.set("netIvao", "1");
const cbV = document.getElementById("net-vatsim");
const cbI = document.getElementById("net-ivao");
cbV.checked = prefs.get("netVatsim", "0") === "1";
cbI.checked = prefs.get("netIvao", "0") === "1";
cbV.addEventListener("change", () => {
  prefs.set("netVatsim", cbV.checked ? "1" : "0"); restartTraffic();
});
cbI.addEventListener("change", () => {
  prefs.set("netIvao", cbI.checked ? "1" : "0"); restartTraffic();
});
restartTraffic();

/* OpenAIP key */
const oaipInput = document.getElementById("oaip-key");
oaipInput.value = prefs.get("oaipKey", "");
oaipInput.addEventListener("change", () => {
  prefs.set("oaipKey", oaipInput.value.trim());
  // Persist server-side so every device shares the key (like SimBrief)
  fetch("/api/openaip", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key: oaipInput.value.trim() }),
  }).then((r) => r.ok && showToast("OpenAIP key saved for all devices.", "success"))
    .catch(() => {});
  if (oaipLayer) { map.removeLayer(oaipLayer); layerControl.removeLayer(oaipLayer); }
  oaipLayer = buildOpenAipLayer();
  if (oaipLayer) {
    layerControl.addOverlay(oaipLayer, "VFR chart (OpenAIP)");
    if (overlayWanted("VFR chart (OpenAIP)")) oaipLayer.addTo(map);
  }
  airspaceData = [];
  refreshAirspaces();          // airspaces use the same key
});
map.on("overlayadd", (e) => { if (e.name === "Airspaces") refreshAirspaces(); });

/* Clean map / restore default overlays (Settings → Map overlays) */
document.getElementById("btn-clean-map").addEventListener("click", () => {
  hideAllOverlays();
  showToast("Clean map: all overlays hidden.", "success");
});
document.getElementById("btn-default-map").addEventListener("click", () => {
  restoreDefaultOverlays();
  showToast("Default overlays restored.", "success");
});

/* Track clearing — wipe everything locally right away (map trail, profile
   data) without waiting for the server round-trip, then tell the server so
   every other connected screen clears too. */
document.getElementById("btn-clear-track").addEventListener("click", () => {
  trail.setLatLngs([]);
  trailData = [];
  profileX = [];
  drawProfile();
  ws?.readyState === WebSocket.OPEN &&
    ws.send(JSON.stringify({ type: "clear_track" }));
  showToast("Session track cleared.", "success");
});

/* ------------------------------- Geometry -------------------------------- */
function distNM(lat1, lon1, lat2, lon2) {
  const R = 3440.065, toRad = Math.PI / 180;
  const dLat = (lat2 - lat1) * toRad, dLon = (lon2 - lon1) * toRad;
  const a = Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1 * toRad) * Math.cos(lat2 * toRad) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}
function bearing(lat1, lon1, lat2, lon2) {
  const toRad = Math.PI / 180;
  const y = Math.sin((lon2 - lon1) * toRad) * Math.cos(lat2 * toRad);
  const x = Math.cos(lat1 * toRad) * Math.sin(lat2 * toRad) -
    Math.sin(lat1 * toRad) * Math.cos(lat2 * toRad) * Math.cos((lon2 - lon1) * toRad);
  return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
}
/* Destination point from (lat,lon), bearing brg°, distance in NM */
function destPoint(lat, lon, brg, distNm) {
  const R = 3440.065, toRad = Math.PI / 180, toDeg = 180 / Math.PI;
  const d = distNm / R, b = brg * toRad;
  const p1 = lat * toRad, l1 = lon * toRad;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d) +
    Math.cos(p1) * Math.sin(d) * Math.cos(b));
  const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1),
    Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  return [p2 * toDeg, ((l2 * toDeg + 540) % 360) - 180];
}

/* --------------------------------- Go! ----------------------------------- */
window.addEventListener("resize", drawProfile);
drawHSI(0, null);
connect();
refreshViewport();   // initial airport load for the starting view
