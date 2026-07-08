"""
Live online traffic from the VATSIM and IVAO networks.

Both networks publish free, public data feeds (no key required):
- VATSIM: https://data.vatsim.net/v3/vatsim-data.json (refreshed ~15 s)
- IVAO:   https://api.ivao.aero/v2/tracker/whazzup

A short server-side cache (20 s) means multiple tablets share a single
upstream request, respecting both networks' polling guidelines.
"""

from __future__ import annotations

import json
import time
import urllib.request

from net import urlopen
from geo import haversine_nm as _haversine_nm

VATSIM_URL = "https://data.vatsim.net/v3/vatsim-data.json"
IVAO_URL = "https://api.ivao.aero/v2/tracker/whazzup"
CACHE_TTL = 20  # seconds

_cache: dict = {}  # network → (timestamp, [aircraft])


def fetch_traffic(network: str) -> list:
    """Returns [{callsign, lat, lon, alt_ft, gs_kt, hdg}] for the network."""
    network = network.lower()
    now = time.time()
    hit = _cache.get(network)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]

    url = VATSIM_URL if network == "vatsim" else IVAO_URL
    req = urllib.request.Request(
        url, headers={"User-Agent": "MSFS-Tablet-Tracker/1.1"})
    with urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    out = []
    if network == "vatsim":
        for p in data.get("pilots", []):
            try:
                out.append({
                    "callsign": p["callsign"],
                    "lat": float(p["latitude"]),
                    "lon": float(p["longitude"]),
                    "alt_ft": int(p.get("altitude", 0)),
                    "gs_kt": int(p.get("groundspeed", 0)),
                    "hdg": int(p.get("heading", 0)),
                })
            except (KeyError, TypeError, ValueError):
                continue
    else:  # ivao
        for p in (data.get("clients", {}) or {}).get("pilots", []):
            t = p.get("lastTrack") or {}
            try:
                out.append({
                    "callsign": p.get("callsign", "?"),
                    "lat": float(t["latitude"]),
                    "lon": float(t["longitude"]),
                    "alt_ft": int(t.get("altitude", 0)),
                    "gs_kt": int(t.get("groundSpeed", 0)),
                    "hdg": int(t.get("heading", 0)),
                })
            except (KeyError, TypeError, ValueError):
                continue

    _cache[network] = (now, out)
    return out


def filter_radius(traffic: list, lat: float, lon: float, radius_nm: float) -> list:
    """Keeps only aircraft within radius_nm of (lat, lon), closest first."""
    out = []
    for t in traffic:
        d = _haversine_nm(lat, lon, t["lat"], t["lon"])
        if d <= radius_nm:
            t = dict(t)
            t["dist_nm"] = round(d, 1)
            out.append(t)
    out.sort(key=lambda x: x["dist_nm"])
    return out[:150]  # cap the payload
