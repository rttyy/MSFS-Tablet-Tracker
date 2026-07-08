"""
Vector airspaces from the OpenAIP Core API (free key from openaip.net —
the same key used for the VFR tile layer).

Airspaces are fetched per map area, normalized (limits converted to feet)
and cached on disk for 30 days: each area costs a single Internet request.
The client draws the polygons and raises "entering <airspace>" alerts by
point-in-polygon + altitude checks.
"""

from __future__ import annotations

import json
import time
import urllib.request

from net import urlopen
from pathlib import Path

API_URL = "https://api.core.openaip.net/api/airspaces?bbox={w},{s},{e},{n}&limit=1000"
MAX_SPAN_DEG = 4.0
CACHE_TTL = 30 * 24 * 3600

# OpenAIP icaoClass enum → letter
ICAO_CLASS = {0: "A", 1: "B", 2: "C", 3: "D", 4: "E", 5: "F", 6: "G", 8: "SUA"}


class AirspaceDB:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "airspaces_cache.json"
        try:
            self.cache = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self.cache = {}

    def get_airspaces(self, south, west, north, east, api_key: str) -> list:
        if not api_key:
            raise ValueError("OpenAIP API key required (Settings panel)")
        if north - south > MAX_SPAN_DEG or east - west > MAX_SPAN_DEG:
            raise ValueError("area too large — zoom in")

        # Coarse cache key so pans within the same area reuse the entry
        key = f"{round(south)},{round(west)},{round(north)},{round(east)}"
        hit = self.cache.get(key)
        if hit and time.time() - hit["t"] < CACHE_TTL:
            return hit["a"]

        url = API_URL.format(w=west, s=south, e=east, n=north)
        req = urllib.request.Request(url, headers={
            "User-Agent": "MSFS-Tablet-Tracker/1.1",
            "x-openaip-api-key": api_key,
        })
        with urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        out = []
        for item in data.get("items", []):
            geom = item.get("geometry") or {}
            if geom.get("type") != "Polygon":
                continue  # keep it simple: polygons cover CTR/TMA/D/P/R
            out.append({
                "name": item.get("name", "?"),
                "class": ICAO_CLASS.get(item.get("icaoClass"), "?"),
                "type": item.get("type"),          # raw OpenAIP type id
                "lower_ft": _limit_ft(item.get("lowerLimit")),
                "upper_ft": _limit_ft(item.get("upperLimit")),
                # reference datum of each limit (GND/MSL/STD): a floor of
                # "1000 ft AGL" must be compared against the AGL altitude,
                # not MSL — the client picks the right altitude source.
                "lower_ref": _limit_ref(item.get("lowerLimit")),
                "upper_ref": _limit_ref(item.get("upperLimit")),
                # ring of [lat, lon] pairs (GeoJSON is lon/lat → swapped)
                "ring": [[c[1], c[0]] for c in geom["coordinates"][0]],
            })

        self.cache[key] = {"t": time.time(), "a": out}
        try:
            self.path.write_text(json.dumps(self.cache), encoding="utf-8")
        except Exception:
            pass
        return out


def _limit_ref(limit: dict | None) -> str:
    """OpenAIP referenceDatum enum → 'GND' | 'MSL' | 'STD'."""
    if not limit:
        return "GND"
    return {0: "GND", 1: "MSL", 2: "STD"}.get(limit.get("referenceDatum"), "MSL")


def _limit_ft(limit: dict | None) -> int:
    """Converts an OpenAIP vertical limit to feet (unit 1=ft, 2=m, 6=FL)."""
    if not limit:
        return 0
    v = float(limit.get("value", 0))
    unit = limit.get("unit")
    if unit == 6:      # flight level
        return int(v * 100)
    if unit == 2:      # meters
        return int(v * 3.28084)
    return int(v)      # feet
