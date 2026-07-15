# MSFS Tablet Tracker (v2.1.0)

Real-time tracking of your Microsoft Flight Simulator aircraft on a tablet, phone or second screen, over your local network. The PC running MSFS hosts a small Python server; any tablet on the same Wi-Fi displays an interactive map that follows the aircraft.

Compatible with MSFS 2020 and MSFS 2024 (both use the same SimConnect API). Free and open source (MIT license).

## Getting started

1. Copy `MSFS-Tablet-Tracker.exe` anywhere you like on your PC.
2. Start a flight in MSFS, double-click the exe.
3. Allow it through the Windows firewall (private network) at the first prompt.
4. Scan the QR code shown in the console with your tablet — or type the URL displayed underneath. The QR is also available in the Settings panel of the web interface at any time.

That's it: no Python or dependency to install on the pilot's PC (the exe embeds everything). Only the developer needs Python to rebuild the exe.

## Features

The server continuously reads position, altitude (MSL and AGL), heading, ground and indicated speed, vertical speed, on-ground/airborne status, stall and overspeed warnings, aircraft type, fuel, plus in-sim weather (wind, temperature, visibility) via SimConnect. Data is broadcast at 1–5 Hz over WebSocket to an unlimited number of simultaneous clients (pilot, copilot, wall display…).

### Map & navigation

- Heading-oriented aircraft icon, flight trail recorded server-side (survives page reloads and reconnections), and a 1/2/5-minute prediction vector ahead of the aircraft
- Smart auto-zoom that adapts to the flight phase and disengages on the first manual map gesture (◎ button to resume tracking); the auto-zoom itself can be turned off in Settings to keep centering while controlling the zoom level manually
- Worldwide airport coverage with viewport-based loading: major airports at low zoom, every airfield at high zoom, runways from zoom 10; ICAO labels appear at closer zoom levels for a cleaner overview
- Named gates and parking stands (OpenStreetMap/Overpass, loaded at zoom ≥ 14, names shown at zoom ≥ 16, 30-day disk cache)
- Base map selector: OSM, Esri satellite, topographic, optional OpenAIP VFR chart layer
- Live online traffic: VATSIM and/or IVAO pilots around you (orange for VATSIM, cyan for IVAO), refreshed every 20 s; both networks can be enabled simultaneously and labels auto-hide when zoomed out
- Vector airspaces with "entering airspace" alerts (requires the free OpenAIP key, drawn from zoom 8)
- Extended runway centerlines at the destination: dashed 10 NM approach axes with 3/5/10 NM distance dots, drawn automatically when a plan loads
- "All my flights" heatmap overlay: every archived flight drawn as a translucent orange line — your personal routes light up over time
- Every map overlay (airports, gates, traffic, airspaces, plan, approach axes, VFR chart, prediction, all my flights) has its on/off state saved per device

### SimBrief integration

- One-tap plan loading (free flight by default at startup — no stale plan resurrected); route drawn in magenta with waypoint names always visible (auto-hidden at world-level zooms to keep the map readable)
- Live remaining distance, ETE and next waypoint in the flight plan bar, computed with an exact segment-projection algorithm that stays smooth even in turns
- Fuel-on-board at destination (FOB@dest) predicted from the current fuel, live fuel flow and remaining flight time, turning red if it drops below the SimBrief reserve
- Vertical profile (📈 button) with TOC/TOD markers, "TOD in X NM" alert, the actual flown altitude drawn dashed over the theoretical profile, and a replay cursor synchronized with the ghost aircraft on the map
- Frequencies panel (📻 button): ATIS/Delivery/Ground/Tower/Approach for the departure and arrival airports (OurAirports database), plus the sequence of FIRs/centers along the route — tap any frequency to set it as the sim's COM1 standby, with the live COM1 active/standby shown at the top. Each airport gets a "🗺 Charts" shortcut opening ChartFox for that ICAO
- "Free flight" button that unloads the plan server-side and syncs every connected screen; reload a plan whenever you like

### Instruments & weather

- Simplified PFD strip: IAS, GS, heading, altitude, V/S, wind, temperature
- G1000-style HSI rose with a magenta needle to the next waypoint, contrasted for both day and night themes
- System bar: Zulu clock, tap-to-cycle chrono (grey → running orange → paused green), and GEAR / FLAPS status chips, plus fuel with derived flow and endurance
- METAR panel (☁ button): live observations for departure and arrival — raw METAR, quick decode (wind, visibility, temperature, QNH), color-coded VFR/MVFR/IFR/LIFR category and observation age. Auto-refreshes every 5 minutes (with cache); the ⟳ button forces a fresh upstream fetch even within the cache window
- Visual stall and overspeed alerts

### Landing, logbook & replay

- Landing monitor: BUTTER→HARD rating based on the touchdown rate captured just before contact (accurate on all aircraft), with G-force and speed
- Automatic logbook (📓 button, persisted in `data/logbook.json`) with a statistics summary at the top: total flights, hours, distance, airports visited, average and best touchdown rates
- Every flight is archived automatically at touchdown in `data/flights/` (a GPX file importable anywhere, plus the raw track, plus a snapshot of the SimBrief plan when applicable, plus a shareable flight card). Each logbook entry offers ▶ Replay, GPX download and Card download
- Achievements: 21 badges spanning flight milestones, distance, destinations, landings and specialties (tiers bronze/silver/gold/platinum, progress bars, unlock dates). Visible as a summary card at the top of the logbook, as a dedicated panel via "View all", and as a celebratory banner on the landing rating card when a flight unlocks new ones
- **Flight card**: a shareable 1200×630 PNG generated at the end of each flight, with a dark basemap behind the flown track, aircraft type, callsign, duration, distance, max altitude, max ground speed, average block speed, touchdown rate, G-force and fuel used. Auto-download offered right at landing; regenerable on demand for any archived flight
- Replay mode (▶ button) for the current session or for any archived flight, with a time slider and ×30 playback. When replaying an archived flight, its own SimBrief plan snapshot is loaded automatically (route, waypoints and profile), and the session's plan is restored on exit — no need to reload anything on SimBrief
- GPX/KML export of the current session track

### Comfort & reliability

- Interface scale slider (80–170 %) — live preview, saved per device, resizes PFD, panels, buttons and map labels
- Adaptive toolbar: buttons wrap into a second column at high scales or short screens
- Panels close on tap outside, with fade/slide animations
- Dark mode by default (day/night toggle, remembered per device); fullscreen mode; multi-device (pilot + copilot see the same flight simultaneously)
- "Keep screen awake" option in Settings (Wake Lock API when available, video-based no-sleep fallback for plain HTTP)
- Dismissible connection banner (auto-reappears if the issue persists after 60 s)
- Live COM1 line in the Frequencies panel refreshes on every SimConnect frame, so tapping a frequency shows the change instantly
- The SimConnect link and the WebSocket both reconnect automatically if MSFS or the network restarts
- Robust HTTPS to external services: bundled `certifi` CA plus system-store fallback covers both outdated Windows certificate stores and corporate/antivirus HTTPS interception

## Configuration

`.env` file next to the exe (created on first launch):

```env
# Broadcast rate in Hz (1 to 5). Higher = smoother, more CPU.
UPDATE_HZ=3

# Bind address and port (default 8765). Leave HOST empty for all interfaces.
HOST=
PORT=8765

# Your SimBrief pilot ID (also settable from the Settings panel — the ID is
# stored server-side, so setting it once from any device applies everywhere).
SIMBRIEF_ID=

# Your free openaip.net API key (optional): enables the VFR tile layer and
# the vector airspaces. Also settable from the Settings panel.
OPENAIP_KEY=
```

## Moving to a new version or PC

Copy the `data/` folder (or just `data/flights/`) into the new installation: the logbook rebuilds itself automatically at startup from the per-flight archives, so your history, ratings, replays, flight cards and achievements all follow you. The `.env` file carries your SimBrief ID and OpenAIP key.

The logbook also rescans `data/flights/` on every refresh: dropping a flight file into the folder while the server is running makes it appear immediately on the next `⟳` in the panel.

## Troubleshooting

- **Tablet says "connecting…"**: check that the tablet is on the same Wi-Fi as the PC, allow the exe through the Windows firewall, disable any VPN, and try the URL displayed in the console.
- **`ModuleNotFoundError: No module named 'PIL'`** (when running from source): `pip install pillow`. Flight cards need Pillow but the rest of the tracker keeps working without it — a 501 message is returned for card requests.
- **SimBrief plan won't load**: check the ID, that a plan was generated recently on simbrief.com, and that the server PC has Internet access at that moment.
- **VATSIM/METAR errors "certificate has expired"**: the server automatically falls back to the system certificate store; this typically appears on machines running a corporate/antivirus HTTPS filter and clears itself.
- **Radio tap doesn't tune the sim** on complex study-level aircraft: some addons (Fenix, PMDG…) manage their radios internally and ignore the standard SimConnect event. Report the aircraft and I'll consider a per-addon path.
- **Fuel shows 0 / "fuel used" missing on study-level addons** (Fenix, PMDG…): these manage fuel outside the standard gallons SimVar. The tracker falls back to the fuel-weight SimVar automatically; if an addon maintains neither, the fuel figures stay blank rather than showing wrong numbers.
- **Interface changes don't show up after an update**: the frontend files are versioned (`?v=…` in `index.html`) so browsers reload them automatically; if you edit the files yourself, bump that version number, and rebuild the exe (the frontend is embedded in it).
- **Flight card looks like an older design**: the server invalidates the PNG cache automatically when the card generator's version changes, so a mismatched design should never persist. If it does, delete the `.png` files in `data/flights/` (never the `.json`/`.gpx`) and reopen the card.

## Building the exe (developers only)

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
build_exe.bat
```

The resulting `dist\MSFS-Tablet-Tracker.exe` is self-contained (Python interpreter, all libraries, and the entire frontend are embedded). Rebuild after any frontend change.

## Data & attribution

- Airports and runways: OurAirports (public domain community database) — note that frequencies may differ slightly from your ATC software (BeyondATC, SayIntentions, VATSIM/IVAO…) or the sim's own navdata; when in doubt, the frequency shown by your ATC tool is the one to tune.
- Gates and parking stands: OpenStreetMap contributors (ODbL), via Overpass.
- Weather: aviationweather.gov (NOAA, free, no key).
- Flight plans: SimBrief public API.
- VFR chart tiles and vector airspaces: OpenAIP (free API key required).
- Live online traffic: VATSIM (data.vatsim.net) and IVAO (api.ivao.aero) public feeds.
- Flight card basemap: © OpenStreetMap · © CARTO (dark tiles).
- Charts shortcut: ChartFox (external link only, no data pulled).
- Map rendering: Leaflet (BSD).

## Changelog

**2.1.0** (2026-07-15): clean map mode. A toolbar button (👁) hides all the interface chrome for a distraction-free moving map, and brings it back with a second press or Escape.

**2.0.0** (2026-07-07) — massive release: VATSIM/IVAO traffic, vector airspaces with alerts, fuel tracking, tap-to-tune radios, flight cards, 21 achievements, "all my flights" heatmap, plus a deep accuracy pass (magnetic HSI, ground-track prediction, airspace altitude datums, sim-rate-proof speeds, real touchdown G peak) and reliability hardening for distribution.

**1.0.0** — initial public release: real-time moving map, worldwide airports, SimBrief integration, METARs, PFD/HSI, landing monitor, logbook and replay.

The detailed history lives in [CHANGELOG.md](CHANGELOG.md).
