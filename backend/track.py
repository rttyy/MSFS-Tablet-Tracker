"""
Flight history: records the trajectory server-side and exports it
as GPX or KML on demand (/api/track.gpx, /api/track.kml).

Sampling: at most one point every TRACK_INTERVAL seconds, and only when
the aircraft has moved, keeping files light even on long flights.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from geo import haversine_nm

TRACK_INTERVAL = 2.0      # minimum seconds between two points
MIN_MOVE_DEG = 0.00005    # ~5 m: below this the aircraft is considered still
# A jump larger than this between two recorded points can only be a
# relocation, not real flight: even at high sim-rate an aircraft covers
# far less than this in one ~2 s sample. Used to auto-drop the bogus line
# MSFS draws from the loading position to the real departure.
TELEPORT_NM = 50.0


class FlightTrack:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.points: list[dict] = []
        self._last_ts = 0.0

    def add_point(self, state: dict) -> bool:
        """Record one track point. Returns True when a teleport was detected
        and the trail was reset, so the caller can re-sync connected screens
        (the bogus line to the loading position disappears on its own)."""
        now = state.get("ts", time.time())
        if now - self._last_ts < TRACK_INTERVAL:
            return False
        teleported = False
        if self.points:
            last = self.points[-1]
            # MSFS relocates the aircraft when a flight finishes loading (and
            # on slew / "set position"). The straight line from that loading
            # spot to the real departure is the classic "huge useless trace":
            # a jump this large can't be real flight, so drop the trail so far
            # and restart cleanly from here — no manual "clear track" needed.
            if haversine_nm(last["lat"], last["lon"],
                            state["lat"], state["lon"]) > TELEPORT_NM:
                self.points.clear()
                teleported = True
                print("[Track] Teleport detected (loading/slew) — trail reset.")
            elif (abs(last["lat"] - state["lat"]) < MIN_MOVE_DEG
                    and abs(last["lon"] - state["lon"]) < MIN_MOVE_DEG):
                return False  # stationary (e.g. parked) → no point
        self._last_ts = now
        pp = state.get("plan_progress") or {}
        self.points.append({
            "lat": state["lat"], "lon": state["lon"],
            "alt_m": state.get("alt_ft", 0) * 0.3048,
            # exact along-route distance (NM) when a plan is loaded:
            # consumed by the vertical profile and the replay cursor
            "x": pp.get("dist_flown_nm"),
            # fuel on board (kg): lets the flight card recompute the fuel
            # used from the archived track when the takeoff capture failed
            "fuel": state.get("fuel_kg"),
            # sim-reported ground speed (kt): the card's "max GS" must come
            # from the sim, not from positions/wall-clock (sim-rate or
            # pauses turn position-derived speeds into nonsense)
            "gs": round(state["gs_kt"]) if state.get("gs_kt") is not None else None,
            "ts": now,
        })
        # Safety cap (~27 h at one point per 2 s): the live track must not
        # grow without bound if the server runs for days.
        if len(self.points) > 50000:
            del self.points[0]
        return teleported

    def slice(self, t_from: float, t_to: float) -> list:
        """Track points within a time window
        (used to extract a full flight at landing)."""
        return [p for p in self.points if t_from <= p["ts"] <= t_to]

    def reset(self):
        self.points.clear()
        self._last_ts = 0.0
        print("[Track] Track cleared.")

    # ------------------------------ Exports ----------------------------- #
    def to_gpx(self, points: list = None, name: str = None) -> str:
        """GPX of the full track, or of a provided point list
        (individual flight archiving)."""
        pts_src = self.points if points is None else points
        pts = "\n".join(
            f'      <trkpt lat="{p["lat"]:.6f}" lon="{p["lon"]:.6f}">'
            f'<ele>{p["alt_m"]:.1f}</ele>'
            f'<time>{_iso(p["ts"])}</time></trkpt>'
            for p in pts_src
        )
        title = name or ("MSFS flight " + _iso(time.time())[:10])
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="MSFS Tablet Tracker" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>{escape(title)}</name>
    <trkseg>
{pts}
    </trkseg>
  </trk>
</gpx>
"""

    def to_kml(self) -> str:
        coords = "\n".join(
            f'          {p["lon"]:.6f},{p["lat"]:.6f},{p["alt_m"]:.1f}'
            for p in self.points
        )
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>MSFS flight</name>
    <Style id="track"><LineStyle><color>ff00d5ff</color><width>3</width></LineStyle></Style>
    <Placemark>
      <name>Trajectory</name>
      <styleUrl>#track</styleUrl>
      <LineString>
        <altitudeMode>absolute</altitudeMode>
        <coordinates>
{coords}
        </coordinates>
      </LineString>
    </Placemark>
  </Document>
</kml>
"""


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
