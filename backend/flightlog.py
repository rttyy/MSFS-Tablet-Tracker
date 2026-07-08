"""
Flight monitor and logbook.

State machine fed by every SimConnect frame:
- takeoff detected (ground → air): stores the time and departure airport;
- landing detected (air → ground): captures the touchdown rate (strongest
  V/S of the last ~3 airborne seconds), load factor and speed, computes
  a rating, and closes the logbook entry.

The logbook is persisted in data/logbook.json and served by /api/logbook.
The landing event is also broadcast over WebSocket so tablets can show
the "landing rating" card.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from geo import haversine_nm as _haversine_nm

MIN_AIRBORNE_S = 30   # minimum airborne time to count a takeoff/landing
RECENT_VS_S = 3.5     # V/S capture window before touchdown


def rate_landing(vs_fpm: float) -> str:
    """Classic sim-community scale."""
    v = abs(vs_fpm)
    if v < 60:
        return "BUTTER"
    if v < 150:
        return "SMOOTH"
    if v < 300:
        return "GOOD"
    if v < 500:
        return "FIRM"
    if v < 800:
        return "HARD"
    return "CRASH?"


class FlightMonitor:
    def __init__(self, data_dir: Path):
        self.log_path = data_dir / "logbook.json"
        try:
            self.entries = json.loads(self.log_path.read_text(encoding="utf-8"))
        except Exception:
            self.entries = []

        self.airborne = False
        self.t_takeoff = 0.0
        self.origin = None           # {ident, name} or None
        self.dist_nm = 0.0
        self._last_pos = None        # (lat, lon)
        self._recent_vs = []         # [(ts, vs_fpm)] while airborne
        self._recent_g = []          # [(ts, g)] while airborne (peak capture)
        # True once the aircraft has been seen ON GROUND this session:
        # a takeoff is only logged after that, so starting the server
        # mid-flight doesn't create a bogus "takeoff" from the nearest
        # airport at connection time.
        self._seen_ground = False
        self._fuel_takeoff = None   # fuel_kg at takeoff → fuel used

    # ------------------------------------------------------------------ #
    def sync_from_archives(self, flights_dir: Path):
        """
        Rebuilds missing logbook entries from the per-flight archives in
        data/flights/: each <id>.json embeds its full logbook entry, so
        copying flight files into a fresh install (new version, new PC)
        is enough — the logbook repopulates itself at startup.
        """
        if not flights_dir.exists():
            return
        known = {e.get("track_id") for e in self.entries if e.get("track_id")}
        imported = 0
        for path in sorted(flights_dir.glob("*.json")):
            fid = path.stem
            if fid in known:
                continue
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))["entry"]
            except Exception as e:
                print(f"[Logbook] Unreadable archive {path.name}: {e}")
                continue
            entry["track_id"] = fid   # the archived copy predates the link
            self.entries.append(entry)
            imported += 1
        if imported:
            self.entries.sort(key=lambda e: e.get("date", ""))
            self._save()
            print(f"[Logbook] {imported} flight(s) imported from archives.")

    # ------------------------------------------------------------------ #
    def update(self, state: dict, airport_db) -> dict | None:
        """Called on every frame. Returns an event to broadcast, or None."""
        ts = state.get("ts", time.time())
        lat, lon = state["lat"], state["lon"]

        # Distance flown (accumulated while airborne only)
        if self.airborne and self._last_pos:
            self.dist_nm += _haversine_nm(*self._last_pos, lat, lon)
        self._last_pos = (lat, lon)

        if state["on_ground"]:
            self._seen_ground = True

        # Server started (or sim reconnected) mid-flight: adopt the
        # in-flight state silently — unknown origin, no takeoff event,
        # the landing will still be rated and logged (partial flight).
        if not self.airborne and not state["on_ground"] and not self._seen_ground:
            self.airborne = True
            self.t_takeoff = ts
            self.origin = None
            self.dist_nm = 0.0
            self._fuel_takeoff = state.get("fuel_kg")
            self._recent_vs.clear()
            self._recent_g.clear()
            return None

        # ------------------------------ Takeoff --------------------------
        if (self._seen_ground and not self.airborne and not state["on_ground"]
                and state.get("alt_agl_ft", 0) > 50 and state.get("gs_kt", 0) > 40):
            self.airborne = True
            self.t_takeoff = ts
            self.dist_nm = 0.0
            self._fuel_takeoff = state.get("fuel_kg")
            self._recent_vs.clear()
            self._recent_g.clear()
            ap = airport_db.nearest(lat, lon) if airport_db.loaded else None
            self.origin = _ap_ref(ap)
            return {"type": "takeoff",
                    "data": {"origin": self.origin, "time": _iso(ts)}}

        # ------------------------------ Airborne -------------------------
        if self.airborne and not state["on_ground"]:
            # Prefer the raw V/S: the smoothed one lags behind the flare
            self._recent_vs.append(
                (ts, state.get("vs_raw_fpm", state.get("vs_fpm", 0))))
            self._recent_g.append((ts, state.get("g_force", 1.0)))
            # keep only the recent window
            self._recent_vs = [(t, v) for t, v in self._recent_vs
                               if ts - t <= RECENT_VS_S]
            self._recent_g = [(t, v) for t, v in self._recent_g
                              if ts - t <= RECENT_VS_S]

        # ------------------------------ Landing --------------------------
        if self.airborne and state["on_ground"]:
            self.airborne = False
            if ts - self.t_takeoff < MIN_AIRBORNE_S:
                return None  # bounce / false positive: ignored
            # Touchdown V/S = mean of the very last airborne samples
            # (≤ 0.8 s before contact). Taking the window minimum grabbed
            # the short-final descent rate instead of the post-flare one.
            last = [v for t, v in self._recent_vs if ts - t <= 0.8]
            if not last and self._recent_vs:
                last = [self._recent_vs[-1][1]]
            vs_td = sum(last) / len(last) if last else 0.0
            # Touchdown G: peak over the last airborne ~1.5 s plus the
            # contact frame itself — a single 3 Hz sample at detection
            # time usually misses the compression spike.
            g_win = [v for t, v in self._recent_g if ts - t <= 1.5]
            g_win.append(state.get("g_force", 1.0))
            ap = airport_db.nearest(lat, lon) if airport_db.loaded else None
            entry = {
                "date": _iso(ts),
                "origin": self.origin,
                "destination": _ap_ref(ap),
                "duration_min": round((ts - self.t_takeoff) / 60),
                "dist_nm": round(self.dist_nm, 1),
                "touchdown_fpm": round(vs_td),
                "g_force": round(max(g_win), 2),
                "ias_kt": round(state.get("ias_kt", 0)),
                "rating": rate_landing(vs_td),
                "aircraft": state.get("aircraft"),
                "fuel_used_kg": (
                    round(self._fuel_takeoff - state["fuel_kg"])
                    if self._fuel_takeoff and state.get("fuel_kg") is not None
                    and self._fuel_takeoff >= state["fuel_kg"] else None),
            }
            self.entries.append(entry)
            self._save()
            print(f"[Logbook] Landing: {entry['touchdown_fpm']} fpm "
                  f"({entry['rating']}) at "
                  f"{entry['destination']['ident'] if entry['destination'] else '????'}")
            return {"type": "landing", "data": entry}

        return None

    def _save(self):
        try:
            self.log_path.write_text(
                json.dumps(self.entries, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except Exception as e:
            print(f"[Logbook] Could not save: {e}")


def _ap_ref(ap: dict | None) -> dict | None:
    """Compact airport reference stored in logbook entries. Includes the
    coordinates so achievements can derive the local (solar) time of a
    landing — 'night owl' must not count a 14:00 Los Angeles landing as
    a night flight just because it's 22:00 UTC."""
    if not ap:
        return None
    return {"ident": ap["ident"], "name": ap["name"],
            "lat": round(ap["lat"], 4), "lon": round(ap["lon"], 4)}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
