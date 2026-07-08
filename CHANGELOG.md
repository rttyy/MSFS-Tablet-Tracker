# Changelog

## 2.0.0 — 2026-07-07

Massive release building on the 1.0 foundation: online traffic, airspaces,
fuel tracking, flight cards, achievements — plus a deep accuracy and
reliability pass to make the tracker trustworthy as a distributable app.

### New features

- **Online traffic**: VATSIM and IVAO pilots around you (orange / cyan, both
  networks at once if you want), refreshed every 20 s, labels auto-hidden at
  low zoom.
- **Vector airspaces** (OpenAIP, free key): CTR/TMA/D/P/R polygons from
  zoom 8 with "entering airspace" alerts by point-in-polygon + altitude.
- **Fuel tracking**: live quantity, derived fuel flow and endurance in the
  system bar, and FOB@destination in the plan bar — red when it drops below
  the SimBrief reserve.
- **Tap-to-tune radios**: tap any frequency in the Frequencies panel to set
  the sim's COM1 standby, with the live COM1 active/standby shown at the top
  and refreshed on every frame.
- **Flight cards**: a shareable 1200×630 PNG generated at every landing —
  dark basemap of the flown route, aircraft, callsign, duration, distance,
  max altitude, max ground speed, average block speed, touchdown rate,
  G-force and fuel used. Auto-offered at landing, regenerable for any
  archived flight.
- **21 achievements** across milestones, time/distance, destinations,
  landings and specialties (bronze → platinum tiers, progress bars, unlock
  dates) — summary card at the top of the logbook, full panel, and a
  celebration banner on the landing card when a flight unlocks new ones.
- **"All my flights" heatmap**: every archived flight drawn as a translucent
  orange line — your personal atlas grows with every landing.
- **Extended runway centerlines** at the destination: dashed 10 NM approach
  axes with 3/5/10 NM distance dots, drawn automatically when a plan loads.
- **ChartFox shortcut** per airport in the Frequencies panel.
- **Free flight by default**: no stale SimBrief plan resurrected at startup;
  a "Free flight" button unloads the plan server-side and syncs every screen.
- **Logbook statistics**: total flights, hours, distance, airports visited,
  average and best touchdown rates.
- **Server-side flight trail**: survives page reloads and reconnections; the
  actual flown altitude is drawn dashed over the theoretical vertical
  profile, with a replay cursor synchronized with the ghost aircraft.
- **Self-contained replays**: each archive snapshots its own SimBrief plan;
  replaying an old flight loads its route and profile, and the session plan
  is restored on exit.
- **Interface scale slider** (80–170 %) with live preview, saved per device;
  adaptive toolbar and panels at every scale.
- **Dark mode by default**, keep-screen-awake option (Wake Lock + video
  fallback), per-device persistence of every map overlay's on/off state.
- **GEAR / FLAPS chips, Zulu clock and tap-cycle chrono** in a new system bar.
- **Logbook auto-sync**: drop a flight `.json` into `data/flights/` and it
  appears on the next refresh — moving to a new PC is just copying a folder.

### Flight data accuracy

- **HSI needle corrected to magnetic**: the course to the next waypoint is
  now converted with the sim's own magnetic variation — it was previously a
  true bearing on a magnetic rose (up to ~20° off in North America).
- **Prediction vector follows the ground track** (course over ground), not
  the heading: with crosswind the 1/2/5-minute line now points where the
  aircraft actually goes.
- **Airspace altitude datums honored**: a floor of "1000 ft AGL" is compared
  against AGL altitude instead of MSL; tooltips show GND/AGL limits.
- **Accurate touchdown rate**: measured from the raw vertical speed at
  contact (previous builds over-reported by up to 300 fpm).
- **Touchdown G-force is the real peak**: captured over the final 1.5 s
  window instead of a single sample at detection time.
- **Max ground speed on flight cards is sim-rate-proof**: read from the
  sim's reported speed recorded in the track; for older archives an
  implausible position-derived figure is hidden rather than shown wrong.
- **Fuel on study-level addons**: automatic fallback to the fuel-weight
  variable for aircraft that manage fuel outside the standard SimVars
  (Fenix, PMDG…); the card can also recompute fuel used from the archived
  track.
- **The landing rollout is archived**: the flight files are extended shortly
  after touchdown instead of being cut at the contact point.
- **GEAR chip shows the actual gear position**, not the handle — no more
  "down" while the gear is still in transit.
- **Night owl / Early bird badges use local solar time** at the destination,
  not UTC — a 2 pm landing in Los Angeles is no longer a "night flight".
- **Country badges use a proper ICAO prefix table** (~130 entries): Melbourne
  and Perth count for Australia, Panama is no longer Mexico, Japan and Korea
  are different countries.

### Fixes

- **"Clear track" fixed**: the command was ignored whenever a SimBrief ID
  was configured; it now clears the trail on every connected screen instantly.
- **The tracker no longer freezes mid-flight**: a single bad data frame is
  skipped instead of silently stopping all live updates.
- **No more "connection lost" spiral**: a transient SimConnect read glitch
  (an isolated `None` frame — sim paused a split second, aircraft change,
  cache not yet refreshed) no longer tears the connection down. Only reads
  that keep failing for several seconds (a real MSFS shutdown) trigger a
  reconnect, so the live feed no longer gets stuck reconnecting forever
  after a harmless hiccup.
- **A stuck SimConnect close can't freeze the feed**: closing a dead
  connection now runs with a timeout and is abandoned if it hangs, instead
  of blocking all live updates while reconnecting.
- **No more bogus trace from the loading position**: when MSFS relocates the
  aircraft as a flight finishes loading (or on slew/reposition), the long
  straight line to the real departure is dropped automatically and the trail
  restarts from the new spot — no need to hit "clear track" every flight.
- **Fewer "certificate expired" errors** on VATSIM, METAR and SimBrief:
  automatic fallback covers outdated Windows certificate stores and
  corporate/antivirus HTTPS filtering.

## 1.0.0 — initial public release

Real-time moving map for MSFS 2020/2024 on any device of your home Wi-Fi:
run the exe, scan the QR code, you're flying.

- Heading-oriented aircraft icon, flight trail, smart auto-zoom adapting to
  the flight phase (disengages on manual pan/zoom, one-tap recenter).
- Worldwide airports with runways, named gates and parking stands; base map
  selector (OSM, satellite, topographic, optional OpenAIP VFR layer).
- 1/2/5-minute prediction vector, G1000-style HSI rose.
- SimBrief integration: latest plan auto-loaded (magenta route, waypoint
  names always visible), live remaining distance/ETE and next waypoint,
  vertical profile with TOC/TOD markers and "TOD in X NM" alert,
  departure/arrival frequencies plus FIRs along the route; the SimBrief ID
  is stored server-side and shared by every device.
- Overlay PFD (IAS, GS, heading, altitude, V/S, wind, OAT), live METARs
  with color-coded flight categories, stall and overspeed warnings.
- Automatic landing rate monitor (touchdown fpm, G-force, speed — from
  BUTTER to HARD) and automatic logbook with per-flight GPX archive and
  replayable track; replay mode with time slider and ×30 playback.
- Night mode, fullscreen, multi-device (pilot + copilot), connection status
  pill; standalone single-exe distribution.

Data sources: OurAirports (public domain), OpenStreetMap/Overpass,
aviationweather.gov (NOAA), SimBrief API. Free and open source (MIT).
