"""
MSFS Tablet Tracker — Main server
=================================
- Connects to MSFS 2020/2024 via SimConnect (with automatic reconnection)
- Serves the web interface (frontend/) on the local network
- Broadcasts the aircraft state in real time over WebSocket (multi-client)
- Exposes APIs: SimBrief flight plan, worldwide airports, gates, METARs,
  frequencies, logbook, per-flight archives, GPX/KML exports
- Prints the local IP + a QR code in the console for quick connection

Run:  python backend/server.py   (from the project root)
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import socket
import sys
from pathlib import Path

from aiohttp import web, WSMsgType
from dotenv import load_dotenv

# Console prints contain arrows ("LFLL → LFPG"): when stdout is a pipe
# (launcher, redirected logs) Windows falls back to cp1252, which can't
# encode them — and an un-encodable print must never crash the server.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass

# Local imports (backend/ is added to the path when run from the root)
sys.path.insert(0, str(Path(__file__).parent))
from sim_source import SimSource          # noqa: E402
from airports import AirportDB            # noqa: E402
from simbrief import fetch_simbrief_plan  # noqa: E402
from track import FlightTrack             # noqa: E402
from metar import fetch_metars            # noqa: E402
from gates import GateDB                  # noqa: E402
from flightlog import FlightMonitor       # noqa: E402
from online_traffic import fetch_traffic, filter_radius  # noqa: E402
from airspaces import AirspaceDB           # noqa: E402
from geo import haversine_nm               # noqa: E402
# Flight cards need Pillow; the tracker must keep working without it
# (a missing optional dependency should never take the whole server down)
try:
    from flightcard import render_card      # noqa: E402
except ImportError:
    render_card = None
    print("[Flights] Pillow not installed - flight cards disabled. "
          "Run: pip install pillow")
import achievements                        # noqa: E402

# ---------------------------------------------------------------------------
# Configuration (.env at the project root)
# Two path-resolution modes:
# - Python script: root = project folder
# - PyInstaller executable: the frontend is embedded inside the exe
#   (sys._MEIPASS), while .env and data/ live NEXT TO the exe so the
#   user can still edit them.
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).parent          # folder of the exe
    BUNDLE = Path(sys._MEIPASS)                 # embedded resources
else:
    ROOT = Path(__file__).parent.parent
    BUNDLE = ROOT
load_dotenv(ROOT / ".env")

VERSION = "2.0.0"

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8765"))
UPDATE_HZ = max(1, min(5, int(os.getenv("UPDATE_HZ", "3"))))  # 1 to 5 Hz
SIMBRIEF_ID = os.getenv("SIMBRIEF_ID", "").strip()
OPENAIP_KEY = os.getenv("OPENAIP_KEY", "").strip()

FRONTEND_DIR = BUNDLE / "frontend"
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
VOLS_DIR = DATA_DIR / "flights"          # one GPX + one JSON track per flight
VOLS_DIR.mkdir(exist_ok=True)


def persist_env(key: str, value: str):
    """Writes/replaces a key in the .env file (created if missing):
    a SimBrief ID entered on any tablet becomes the default for the
    whole installation, including after restarts."""
    env_path = ROOT / ".env"
    lines = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    done = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            done = True
            break
    if not done:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------
def get_local_ip() -> str:
    """Determines the machine's LAN IP without sending any real traffic
    (UDP connect() only selects a route). A public address is used so it
    works on any LAN scheme (192.168.x, 10.x, 172.16.x…); a private-range
    fallback covers machines with no default route to the Internet."""
    for probe in ("8.8.8.8", "192.168.255.255"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe, 1))          # no packet is actually sent
            return s.getsockname()[0]
        except OSError:
            continue
        finally:
            s.close()
    return "127.0.0.1"


def print_banner(url: str) -> None:
    """Prints the URL + an ASCII QR code in the console."""
    print("\n" + "=" * 60)
    print("  MSFS Tablet Tracker")
    print("=" * 60)
    print(f"  Version                  :  {VERSION}")
    print(f"  On your tablet, open     :  {url}")
    print(f"  Broadcast rate           :  {UPDATE_HZ} Hz")
    print(f"  SimBrief ID              :  {SIMBRIEF_ID or '(not set)'}")
    print("=" * 60)
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make()
        # ASCII QR code straight in the terminal
        qr.print_ascii(invert=True)
    except Exception:
        print("  (install 'qrcode' to display the QR code: pip install qrcode)")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
class TrackerApp:
    def __init__(self):
        self.sim = SimSource()
        self.airport_db = AirportDB(DATA_DIR)
        self.gate_db = GateDB(DATA_DIR)
        self.monitor = FlightMonitor(DATA_DIR)
        self.monitor.sync_from_archives(VOLS_DIR)
        self._refresh_card_cache()
        self.airspace_db = AirspaceDB(DATA_DIR)
        self.track = FlightTrack(DATA_DIR)
        self.clients: set[web.WebSocketResponse] = set()
        self.flightplan: dict | None = None
        self.simbrief_id = SIMBRIEF_ID
        # Plan geometry (cumulative leg distances) + continuity of the
        # along-route position between two frames
        self._plan_cum: list = []
        self._plan_total = 0.0
        self._route_x = None
        self._route_prev = None
        self.openaip_key = OPENAIP_KEY

    # ------------------------- Broadcast loop ------------------------------
    async def broadcast_loop(self):
        """Reads the SimConnect state at UPDATE_HZ and pushes it to clients.
        Every iteration is guarded: one bad frame (plan progress, logbook,
        track…) must never kill the loop — a dead loop would leave the
        server answering HTTP while silently broadcasting nothing."""
        interval = 1.0 / UPDATE_HZ
        while True:
            try:
                await self._broadcast_once()
            except Exception:
                import traceback
                print("[Server] Broadcast frame failed (loop continues):")
                traceback.print_exc()
            await asyncio.sleep(interval)

    async def _broadcast_once(self):
        state = await asyncio.to_thread(self.sim.read_state)
        if state:
            if self.flightplan:
                state["plan_progress"] = self._plan_progress(state)
            # Track recording (GPX/KML exports, replay)
            if self.track.add_point(state):
                # A relocation (flight finished loading, slew) reset the
                # trail: re-sync every connected screen so the bogus line
                # from the loading spot to the real start vanishes without
                # the pilot having to hit "clear track".
                await self._send_all(json.dumps({"type": "trail", "data": {
                    "points": [{"lat": p["lat"], "lon": p["lon"],
                                "alt": round(p["alt_m"] / 0.3048),
                                "x": p.get("x")}
                               for p in self.track.points],
                }}))
            # Takeoff / landing detection (logbook)
            event = self.monitor.update(state, self.airport_db)
            if event:
                if event["type"] == "landing":
                    # Achievements newly unlocked by THIS flight:
                    # compare before adding it to the running list
                    # (the entry has just been appended by update()).
                    before = self.monitor.entries[:-1]
                    after = self.monitor.entries
                    try:
                        event["data"]["new_badges"] = (
                            achievements.newly_unlocked(before, after))
                    except Exception as e:
                        print(f"[Achievements] check failed: {e}")
                        event["data"]["new_badges"] = []
                    # Automatic flight archiving (GPX + replay track)
                    fid = self._save_flight(event["data"], state["ts"])
                    if fid:
                        event["data"]["track_id"] = fid
                        self.monitor._save()   # persists the link
                        # The archive was cut at touchdown: extend it in
                        # ~40 s so the rollout makes it into the files.
                        asyncio.create_task(self._extend_archive(
                            fid, event["data"],
                            self.monitor.t_takeoff, state["ts"]))
                await self._send_all(json.dumps(event))
            msg = json.dumps({"type": "state", "data": state})
            await self._send_all(msg)
        else:
            # No sim connection: inform the clients (status)
            msg = json.dumps({
                "type": "status",
                "data": {"connected": False, "detail": self.sim.status},
            })
            await self._send_all(msg)

    async def _send_all(self, msg: str):
        dead = []
        for ws in self.clients:
            try:
                await ws.send_str(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    # ------------------------------ WebSocket ------------------------------
    async def ws_handler(self, request: web.Request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self.clients.add(ws)
        print(f"[WS] Client connected ({len(self.clients)} total)")

        # On connect: send the already-loaded flight plan, if any
        if self.flightplan:
            await ws.send_str(json.dumps({"type": "flightplan", "data": self.flightplan}))
        # …and the trail recorded so far, so a reconnecting tablet
        # (page refresh, Wi-Fi drop) gets the full flight path back.
        await ws.send_str(json.dumps({"type": "trail", "data": {
            "points": [{"lat": p["lat"], "lon": p["lon"],
                        "alt": round(p["alt_m"] / 0.3048),
                        "x": p.get("x")}
                       for p in self.track.points[-4000:]],
        }}))

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._handle_client_msg(ws, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.clients.discard(ws)
            print(f"[WS] Client disconnected ({len(self.clients)} left)")
        return ws

    async def _handle_client_msg(self, ws, raw: str):
        """Incoming client messages: SimBrief configuration, track reset…"""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if msg.get("type") == "set_simbrief":
            new_id = str(msg.get("id", "")).strip()
            if new_id:
                self.simbrief_id = new_id
                persist_env("SIMBRIEF_ID", new_id)
                # Free flight by default: the ID is stored but the plan is
                # only loaded explicitly (Settings → Load plan).
                print("[SimBrief] ID configured "
                      " Import your flight plan : Use Settings → Load plan.")
        elif msg.get("type") == "clear_track":
            self.track.reset()
            # Sync every connected screen (pilot AND copilot)
            await self._send_all(json.dumps(
                {"type": "trail", "data": {"points": []}}))

    # ------------------------------- SimBrief ------------------------------
    async def load_simbrief(self, broadcast: bool = False):
        if not self.simbrief_id:
            return
        plan = await asyncio.to_thread(fetch_simbrief_plan, self.simbrief_id)
        if plan:
            self.flightplan = plan
            self._build_plan_geometry()
            print(f"[SimBrief] Plan loaded: {plan['origin']['icao']} → "
                  f"{plan['destination']['icao']} ({len(plan['waypoints'])} waypoints)")
            if broadcast:
                await self._send_all(json.dumps({"type": "flightplan", "data": plan}))
        else:
            print("[SimBrief] Failed to fetch the flight plan.")
            if broadcast:
                await self._send_all(json.dumps({
                    "type": "flightplan_error",
                    "data": {"detail": "Could not fetch the SimBrief plan."},
                }))

    # -------------------------------- HTTP API -----------------------------
    async def api_airports(self, request: web.Request):
        """GET /api/airports?lat=..&lon=..&radius=.. → nearby airports + runways."""
        try:
            lat = float(request.query["lat"])
            lon = float(request.query["lon"])
            radius = float(request.query.get("radius", "60"))  # NM
        except (KeyError, ValueError):
            return web.json_response({"error": "lat/lon parameters required"}, status=400)
        airports = await asyncio.to_thread(self.airport_db.nearby, lat, lon, radius)
        return web.json_response({"airports": airports})

    async def api_simbrief(self, request: web.Request):
        """
        POST /api/simbrief {"id": "..."} → stores the ID (persisted in .env:
        it becomes the installation-wide default) then tries to load the
        latest flight plan.
        """
        body = await request.json()
        new_id = str(body.get("id", "")).strip()
        if new_id:
            self.simbrief_id = new_id
            persist_env("SIMBRIEF_ID", new_id)
        await self.load_simbrief(broadcast=True)
        return web.json_response({
            "ok": True,
            "plan_loaded": self.flightplan is not None,
        })

    async def api_config(self, request: web.Request):
        """GET /api/config → current configuration (UI pre-filling)."""
        return web.json_response({
            "version": VERSION,
            "simbrief_id": self.simbrief_id,
            "openaip_key": self.openaip_key,
            "update_hz": UPDATE_HZ,
            "url": f"http://{get_local_ip()}:{PORT}",
        })

    async def api_plan_clear(self, request: web.Request):
        """
        POST /api/plan/clear → "free flight": unloads the SimBrief plan
        server-side and syncs every connected screen. Progress, FOB and
        plan snapshots stop until the next 'Load plan'.
        """
        self.flightplan = None
        self._plan_cum = []
        self._plan_total = 0.0
        self._route_x = None
        self._route_prev = None
        await self._send_all(json.dumps({"type": "flightplan", "data": None}))
        print("[SimBrief] Plan unloaded — free flight.")
        return web.json_response({"ok": True})

    async def api_openaip(self, request: web.Request):
        """
        POST /api/openaip {"key": "..."} → stores the OpenAIP API key
        server-side (.env), like the SimBrief ID: set it once from any
        device, every tablet gets the VFR layer and the airspaces.
        """
        body = await request.json()
        self.openaip_key = str(body.get("key", "")).strip()
        persist_env("OPENAIP_KEY", self.openaip_key)
        return web.json_response({"ok": True})

    async def api_qr(self, request: web.Request):
        """GET /api/qr.svg → QR code of the server URL (connect a second
        device straight from the settings panel)."""
        import io
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(f"http://{get_local_ip()}:{PORT}",
                          image_factory=qrcode.image.svg.SvgPathImage)
        buf = io.BytesIO()
        img.save(buf)
        return web.Response(body=buf.getvalue(), content_type="image/svg+xml")


    async def api_metar(self, request: web.Request):
        """
        GET /api/metar?ids=LFLL,LFPG → up-to-date METARs for these airports.
        Without the ids parameter: automatically uses the departure and
        arrival of the loaded SimBrief plan.
        """
        ids = request.query.get("ids", "")
        icaos = [s for s in ids.split(",") if s.strip()]
        if not icaos and self.flightplan:
            icaos = [self.flightplan["origin"]["icao"],
                     self.flightplan["destination"]["icao"]]
        if not icaos:
            return web.json_response(
                {"error": "No airports: load a SimBrief plan or pass ?ids=XXXX,YYYY"},
                status=400)
        try:
            force = request.query.get("force") == "1"
            metars = await asyncio.to_thread(fetch_metars, icaos, force)
            return web.json_response({"metars": metars})
        except Exception as e:
            return web.json_response(
                {"error": f"METAR unavailable (Internet required on the PC): {e}"},
                status=502)

    # ------------------- Flight card cache freshness -------------------
    def _refresh_card_cache(self):
        """Cards are cached as PNG next to each flight. When the card
        design changes with a new version, stale caches must not be
        served forever: if the recorded generator version differs, wipe
        the cached PNGs — they regenerate on demand (or at landing)."""
        try:
            VOLS_DIR.mkdir(parents=True, exist_ok=True)
            verfile = VOLS_DIR / "cards.ver"
            old = (verfile.read_text(encoding="utf-8").strip()
                   if verfile.exists() else "")
            if old != VERSION:
                n = 0
                for p in VOLS_DIR.glob("*.png"):
                    p.unlink()
                    n += 1
                verfile.write_text(VERSION, encoding="utf-8")
                if n:
                    print(f"[Flights] {n} cached card(s) invalidated "
                          f"(new design in v{VERSION}).")
        except OSError as e:
            print(f"[Flights] Card cache check failed: {e}")

    # ---------------- Plan geometry & progress (exact) ----------------
    def _build_plan_geometry(self):
        """Cumulative leg distances, computed once per plan load."""
        wps = self.flightplan["waypoints"]
        cum = [0.0]
        for i in range(1, len(wps)):
            cum.append(cum[-1] + haversine_nm(
                wps[i - 1]["lat"], wps[i - 1]["lon"],
                wps[i]["lat"], wps[i]["lon"]))
        self._plan_cum = cum
        self._plan_total = cum[-1] if cum else 0.0
        self._route_x = None
        self._route_prev = None

    def _plan_progress(self, state: dict) -> dict:
        """
        Along-route position by TRUE segment projection (closest point on
        the route line), with a windowed advance: between two frames the
        position can progress at most by the ground distance actually
        flown, and never regress. This one exact number feeds the plan
        bar (remaining NM, ETE), the FOB estimate, the vertical profile
        and the replay cursor.
        """
        wps = self.flightplan["waypoints"]
        if len(wps) < 2:
            return {}
        lat, lon = state["lat"], state["lon"]

        # Closest point on any leg (flat-earth per leg: fine at leg scale)
        cos_lat = math.cos(math.radians(lat))
        best_x, best_d = 0.0, float("inf")
        for i in range(len(wps) - 1):
            a, b = wps[i], wps[i + 1]
            ax = (a["lon"] - lon) * 60 * cos_lat
            ay = (a["lat"] - lat) * 60
            bx = (b["lon"] - lon) * 60 * cos_lat
            by = (b["lat"] - lat) * 60
            dx, dy = bx - ax, by - ay
            len2 = dx * dx + dy * dy
            t = max(0.0, min(1.0, -(ax * dx + ay * dy) / len2)) if len2 > 0 else 0.0
            d = math.hypot(ax + t * dx, ay + t * dy)
            if d < best_d:
                best_d = d
                best_x = self._plan_cum[i] + t * (self._plan_cum[i + 1] - self._plan_cum[i])

        x = max(0.0, min(self._plan_total, best_x))
        # Windowed continuity (reset after a jump/slew > 50 NM)
        if self._route_x is not None and self._route_prev is not None:
            moved = haversine_nm(self._route_prev[0], self._route_prev[1], lat, lon)
            if moved < 50:
                x = min(max(x, self._route_x), self._route_x + moved * 1.5 + 0.3)
        self._route_x = x
        self._route_prev = (lat, lon)

        remaining = self._plan_total - x
        gs = state.get("gs_kt", 0)
        ete_min = (remaining / gs * 60) if gs > 30 else None
        # Next waypoint: the first whose cumulative distance exceeds x
        next_wp = wps[-1]["ident"]
        for i, c in enumerate(self._plan_cum):
            if c > x + 0.05:
                next_wp = wps[i]["ident"]
                break
        return {
            "dist_flown_nm": round(x, 2),
            "dist_remaining_nm": round(remaining, 1),
            "ete_min": round(ete_min) if ete_min else None,
            "next_wp": next_wp,
        }

    # ------------------- Automatic flight archiving -------------------
    def _save_flight(self, entry: dict, t_land: float):
        """
        At landing: extracts the flight's portion of the track (takeoff
        roll included) and writes it to data/flights/ in two forms:
        - <id>.gpx  : importable anywhere (Google Earth, LittleNavMap…)
        - <id>.json : raw points for the logbook replay
        Returns the flight id, or None if the track is too short.
        """
        pts = self.track.slice(self.monitor.t_takeoff - 90, t_land + 30)
        if len(pts) < 2:
            return None
        o = (entry.get("origin") or {}).get("ident", "XXXX")
        d = (entry.get("destination") or {}).get("ident", "XXXX")
        from datetime import datetime
        fid = f"{datetime.now().strftime('%Y%m%d_%H%M')}_{o}-{d}"
        # Touch-and-goes can land twice within the same minute: never
        # overwrite an existing archive, suffix with seconds instead.
        if (VOLS_DIR / f"{fid}.json").exists():
            fid = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{o}-{d}"
        try:
            gpx = self.track.to_gpx(points=pts, name=f"{o} → {d} ({entry['date']})")
            (VOLS_DIR / f"{fid}.gpx").write_text(gpx, encoding="utf-8")
            data = {"entry": entry, "points": pts}
            # Snapshot the loaded SimBrief plan with the flight (when it
            # plausibly matches) so replays are self-contained: the route
            # and the vertical profile no longer depend on whichever plan
            # happens to be loaded later.
            if self.flightplan:
                po = self.flightplan["origin"]["icao"]
                pd = self.flightplan["destination"]["icao"]
                if o == po or d == pd:
                    data["plan"] = self.flightplan
            (VOLS_DIR / f"{fid}.json").write_text(
                json.dumps(data), encoding="utf-8")
            # Shareable recap card (best-effort: a card failure must
            # never lose the flight itself)
            try:
                if render_card is None:
                    raise RuntimeError("Pillow not installed")
                png = render_card(entry, pts, VERSION,
                                  plan=data.get("plan"))
                (VOLS_DIR / f"{fid}.png").write_bytes(png)
            except Exception as e:
                print(f"[Flights] Card generation failed: {e}")
            print(f"[Flights] Flight saved: data/flights/{fid}.gpx")
            return fid
        except Exception as e:
            print(f"[Flights] Could not save: {e}")
            return None

    async def _extend_archive(self, fid: str, entry: dict,
                              t_takeoff: float, t_land: float):
        """The flight archive is written AT the touchdown frame, so the
        rollout that follows doesn't exist yet at that moment. 40 s later,
        re-slice the live track and rewrite the .json/.gpx (and refresh
        the card) so the archived flight includes the rollout."""
        await asyncio.sleep(40)
        try:
            pts = self.track.slice(t_takeoff - 90, t_land + 30)
            json_path = VOLS_DIR / f"{fid}.json"
            if len(pts) < 2 or not json_path.exists():
                return
            data = json.loads(json_path.read_text(encoding="utf-8"))
            if len(pts) <= len(data.get("points", [])):
                return   # nothing new (track cleared meanwhile, sim paused…)
            data["points"] = pts
            json_path.write_text(json.dumps(data), encoding="utf-8")
            o = (entry.get("origin") or {}).get("ident", "XXXX")
            d = (entry.get("destination") or {}).get("ident", "XXXX")
            gpx = self.track.to_gpx(points=pts,
                                    name=f"{o} → {d} ({entry['date']})")
            (VOLS_DIR / f"{fid}.gpx").write_text(gpx, encoding="utf-8")
            if render_card is not None:
                png = await asyncio.to_thread(
                    render_card, entry, pts, VERSION, data.get("plan"))
                (VOLS_DIR / f"{fid}.png").write_bytes(png)
        except Exception as e:
            print(f"[Flights] Rollout extension failed for {fid}: {e}")

    @staticmethod
    def _safe_fid(fid: str):
        """Validates a flight id (path-traversal protection)."""
        return fid if re.fullmatch(r"[\w-]+", fid) else None

    async def api_achievements(self, request: web.Request):
        """GET /api/achievements → state of every badge (unlocked flag,
        progress, unlock date). Re-scans data/flights/ first so brand-
        new flights count immediately (same policy as /api/logbook)."""
        await asyncio.to_thread(self.monitor.sync_from_archives, VOLS_DIR)
        return web.json_response(
            await asyncio.to_thread(achievements.compute,
                                    self.monitor.entries))

    async def api_all_tracks(self, request: web.Request):
        """
        GET /api/flights/tracks → every archived flight's track, decimated
        (≤ 250 points per flight) for the "All my flights" heatmap layer.
        """
        def collect():
            out = []
            for path in sorted(VOLS_DIR.glob("*.json")):
                try:
                    pts = json.loads(path.read_text(encoding="utf-8"))["points"]
                except Exception:
                    continue
                step = max(1, -(-len(pts) // 250))  # ceil
                out.append([[round(p["lat"], 4), round(p["lon"], 4)]
                            for p in pts[::step]
                            if p.get("lat") is not None])
            return out
        tracks = await asyncio.to_thread(collect)
        return web.json_response({"tracks": tracks})

    async def api_flight_card(self, request: web.Request):
        """
        GET /api/flight/{fid}/card.png → the flight's recap card.
        Generated at landing; built on demand for flights archived by
        older versions (then cached to disk). Served from memory: no
        FileResponse/sendfile machinery (source of ERR_INVALID_RESPONSE
        on some platforms), and any failure returns a clean JSON error
        with the full traceback printed to the console.
        """
        fid = self._safe_fid(request.match_info["fid"])
        if not fid:
            return web.json_response({"error": "invalid id"}, status=400)
        if render_card is None:
            return web.json_response(
                {"error": "flight cards need Pillow — run: pip install pillow"},
                status=501)
        try:
            png_path = VOLS_DIR / f"{fid}.png"
            if png_path.exists():
                png = png_path.read_bytes()
            else:
                json_path = VOLS_DIR / f"{fid}.json"
                if not json_path.exists():
                    return web.json_response({"error": "flight not found"},
                                             status=404)
                data = json.loads(json_path.read_text(encoding="utf-8"))
                png = await asyncio.to_thread(
                    render_card, data["entry"], data.get("points", []),
                    VERSION, data.get("plan"))
                try:
                    png_path.write_bytes(png)     # cache, best effort
                except OSError:
                    pass
            return web.Response(body=png, content_type="image/png", headers={
                "Content-Disposition": f'attachment; filename="{fid}.png"',
                "Cache-Control": "no-store",
            })
        except Exception as e:
            import traceback
            print(f"[Flights] Card endpoint failed for {fid}:")
            traceback.print_exc()
            return web.json_response({"error": f"card failed: {e}"}, status=500)

    async def api_flight_track(self, request: web.Request):
        """GET /api/flight/{fid}/track.json → archived flight track."""
        fid = self._safe_fid(request.match_info["fid"])
        path = VOLS_DIR / f"{fid}.json" if fid else None
        if not path or not path.exists():
            return web.json_response({"error": "flight not found"}, status=404)
        return web.json_response(json.loads(path.read_text(encoding="utf-8")))

    async def api_flight_gpx(self, request: web.Request):
        """GET /api/flight/{fid}/gpx → archived GPX download."""
        fid = self._safe_fid(request.match_info["fid"])
        path = VOLS_DIR / f"{fid}.gpx" if fid else None
        if not path or not path.exists():
            return web.json_response({"error": "flight not found"}, status=404)
        return web.FileResponse(path, headers={
            "Content-Disposition": f"attachment; filename={fid}.gpx"})

    async def api_airports_bbox(self, request: web.Request):
        """
        GET /api/airports_bbox?s=&w=&n=&e=&zoom= → worldwide airports
        visible in the map viewport (filtered by significance based on
        zoom). Runways are included from zoom 10.
        """
        try:
            q = request.query
            s_, w = float(q["s"]), float(q["w"])
            n, e = float(q["n"]), float(q["e"])
            zoom = int(q.get("zoom", 8))
        except (KeyError, ValueError):
            return web.json_response({"error": "s/w/n/e parameters required"}, status=400)
        airports = await asyncio.to_thread(
            self.airport_db.in_bbox, s_, w, n, e, zoom, zoom >= 10)
        return web.json_response({"airports": airports})

    async def api_gates(self, request: web.Request):
        """
        GET /api/gates?s=&w=&n=&e= → gates and parking stands (OSM) for the
        visible area. The area is deliberately limited: you must be zoomed
        onto an airport. Disk cache: at most 1 Internet request per area.
        """
        try:
            q = request.query
            s_, w = float(q["s"]), float(q["w"])
            n, e = float(q["n"]), float(q["e"])
        except (KeyError, ValueError):
            return web.json_response({"error": "s/w/n/e parameters required"}, status=400)
        try:
            gates = await asyncio.to_thread(self.gate_db.get_gates, s_, w, n, e)
            return web.json_response({"gates": gates})
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            return web.json_response({"error": f"OSM/Overpass unavailable: {e}"},
                                     status=502)

    async def api_freqs(self, request: web.Request):
        """
        GET /api/freqs[?ids=LFLL,LFPG] → frequencies (ATIS, DEL, GND, TWR,
        APP…) for the requested airports — by default the SimBrief plan's
        departure/arrival — plus the sequence of FIRs along the route.
        """
        ids = request.query.get("ids", "")
        icaos = [x.strip().upper() for x in ids.split(",") if x.strip()]
        if not icaos and self.flightplan:
            icaos = [self.flightplan["origin"]["icao"],
                     self.flightplan["destination"]["icao"]]
        if not icaos:
            return web.json_response(
                {"error": "No airports: load a SimBrief plan or pass ?ids="},
                status=400)
        result = []
        for icao in icaos:
            ap = await asyncio.to_thread(self.airport_db.get, icao)
            freqs = await asyncio.to_thread(self.airport_db.frequencies, icao)
            result.append({"icao": icao,
                           "name": ap["name"] if ap else "",
                           "freqs": freqs})
        return web.json_response({
            "airports": result,
            "firs": (self.flightplan or {}).get("firs", []),
        })

    async def api_traffic(self, request: web.Request):
        """
        GET /api/traffic?network=vatsim|ivao&lat=&lon=&radius= → live online
        traffic around a position. Server-side 20 s cache: every tablet
        shares a single upstream request.
        """
        network = request.query.get("network", "").lower()
        if network not in ("vatsim", "ivao"):
            return web.json_response({"error": "network=vatsim|ivao required"},
                                     status=400)
        try:
            lat = float(request.query["lat"])
            lon = float(request.query["lon"])
            radius = min(500.0, float(request.query.get("radius", "250")))
        except (KeyError, ValueError):
            return web.json_response({"error": "lat/lon parameters required"},
                                     status=400)
        try:
            traffic = await asyncio.to_thread(fetch_traffic, network)
            return web.json_response(
                {"traffic": filter_radius(traffic, lat, lon, radius)})
        except Exception as e:
            return web.json_response(
                {"error": f"{network.upper()} feed unavailable: {e}"}, status=502)

    async def api_airspaces(self, request: web.Request):
        """
        GET /api/airspaces?s=&w=&n=&e=&key= → vector airspaces (OpenAIP).
        The key is the user's free openaip.net API key, sent by the client
        (the same one used for the VFR tile layer). Disk-cached 30 days.
        """
        try:
            q = request.query
            s_, w = float(q["s"]), float(q["w"])
            n, e = float(q["n"]), float(q["e"])
            key = q.get("key", "").strip() or self.openaip_key
        except (KeyError, ValueError):
            return web.json_response({"error": "s/w/n/e parameters required"},
                                     status=400)
        try:
            spaces = await asyncio.to_thread(
                self.airspace_db.get_airspaces, s_, w, n, e, key)
            return web.json_response({"airspaces": spaces})
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            return web.json_response({"error": f"OpenAIP unavailable: {e}"},
                                     status=502)

    async def api_radio(self, request: web.Request):
        """
        POST /api/radio {"mhz": 121.95, "com": 1} → sets the sim's COM
        STANDBY frequency (tap a frequency in the Frequencies panel).
        """
        body = await request.json()
        try:
            mhz = float(body["mhz"])
            com = int(body.get("com", 1))
            # Airband COM range: 118.000 to 136.990 MHz (8.33 kHz plan)
            assert 118.0 <= mhz <= 136.99 and com in (1, 2)
        except (KeyError, ValueError, AssertionError):
            return web.json_response({"error": "invalid frequency"}, status=400)
        ok = await asyncio.to_thread(self.sim.set_com_standby, mhz, com)
        if ok:
            return web.json_response({"ok": True})
        return web.json_response(
            {"error": "sim not connected — frequency not set"}, status=502)

    async def api_airport(self, request: web.Request):
        """GET /api/airport/{icao} → one airport with its runways
        (used for the extended runway centerlines at the destination)."""
        icao = request.match_info["icao"].upper()
        ap = await asyncio.to_thread(self.airport_db.get, icao)
        if not ap:
            return web.json_response({"error": "airport not found"}, status=404)
        result = dict(ap)
        result["runways"] = [
            r for r in self.airport_db.runways_by_airport.get(icao, [])
            if not r["closed"]
        ]
        return web.json_response({"airport": result})

    async def api_track_json(self, request: web.Request):
        """GET /api/track.json → track points (for replay mode)."""
        return web.json_response({"points": self.track.points})

    async def api_logbook(self, request: web.Request):
        """GET /api/logbook → full logbook (most recent first).
        Re-scans data/flights/ first, so flights dropped into the folder
        WHILE the server runs appear on the next logbook refresh (only
        unknown files are parsed — a no-op when nothing changed)."""
        await asyncio.to_thread(self.monitor.sync_from_archives, VOLS_DIR)
        return web.json_response({"entries": list(reversed(self.monitor.entries))})

    async def api_track_gpx(self, request: web.Request):
        gpx = self.track.to_gpx()
        return web.Response(
            text=gpx, content_type="application/gpx+xml",
            headers={"Content-Disposition": "attachment; filename=flight.gpx"},
        )

    async def api_track_kml(self, request: web.Request):
        kml = self.track.to_kml()
        return web.Response(
            text=kml, content_type="application/vnd.google-earth.kml+xml",
            headers={"Content-Disposition": "attachment; filename=flight.kml"},
        )

    async def index(self, request: web.Request):
        return web.FileResponse(FRONTEND_DIR / "index.html")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
async def main():
    tracker = TrackerApp()

    app = web.Application()
    app.router.add_get("/", tracker.index)
    app.router.add_get("/ws", tracker.ws_handler)
    app.router.add_get("/api/airports", tracker.api_airports)
    app.router.add_get("/api/airports_bbox", tracker.api_airports_bbox)
    app.router.add_get("/api/gates", tracker.api_gates)
    app.router.add_get("/api/freqs", tracker.api_freqs)
    app.router.add_get("/api/traffic", tracker.api_traffic)
    app.router.add_get("/api/airspaces", tracker.api_airspaces)
    app.router.add_post("/api/radio", tracker.api_radio)
    app.router.add_get("/api/airport/{icao}", tracker.api_airport)
    app.router.add_get("/api/achievements", tracker.api_achievements)
    app.router.add_get("/api/flights/tracks", tracker.api_all_tracks)
    app.router.add_get("/api/flight/{fid}/card.png", tracker.api_flight_card)
    app.router.add_get("/api/track.json", tracker.api_track_json)
    app.router.add_get("/api/logbook", tracker.api_logbook)
    app.router.add_get("/api/flight/{fid}/track.json", tracker.api_flight_track)
    app.router.add_get("/api/flight/{fid}/gpx", tracker.api_flight_gpx)
    app.router.add_post("/api/simbrief", tracker.api_simbrief)
    app.router.add_get("/api/config", tracker.api_config)
    app.router.add_post("/api/openaip", tracker.api_openaip)
    app.router.add_post("/api/plan/clear", tracker.api_plan_clear)
    app.router.add_get("/api/qr.svg", tracker.api_qr)
    app.router.add_get("/api/metar", tracker.api_metar)
    app.router.add_get("/api/track.gpx", tracker.api_track_gpx)
    app.router.add_get("/api/track.kml", tracker.api_track_kml)
    app.router.add_static("/", FRONTEND_DIR)  # static files (js/css)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()

    url = f"http://{get_local_ip()}:{PORT}"
    print_banner(url)

    # Airport database download/loading in the background
    asyncio.create_task(asyncio.to_thread(tracker.airport_db.ensure_loaded))
    # Free flight by default at startup: no automatic plan loading —
    # a stored OFP would often be stale from a previous session. The
    # pilot loads a plan explicitly (Settings → Load plan).
    if tracker.simbrief_id:
        print("[SimBrief] ID configured. "
              "Import your flight plan : Use Settings → Load plan.")

    await tracker.broadcast_loop()  # infinite loop

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer stopped.")
