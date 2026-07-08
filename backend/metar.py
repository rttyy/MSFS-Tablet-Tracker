"""
Up-to-date METARs via the public aviationweather.gov API
(NOAA — free, no API key).

A 5-minute cache avoids hitting the API every time the panel opens on a
tablet: METARs are only published about every 30 minutes anyway.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from net import urlopen

API_URL = "https://aviationweather.gov/api/data/metar?{}"
CACHE_TTL = 300  # seconds

_cache: dict = {}  # key = "LFLL,LFPG" → (timestamp, result)


def fetch_metars(icaos: list, force: bool = False) -> list:
    """
    Returns a list of normalized METARs for the requested ICAO codes:
    [{icao, name, raw, time, flt_cat, temp_c, dewp_c,
      wind_dir, wind_kt, gust_kt, visib, altim}, ...]
    Raises on network errors (handled by the caller).
    force=True bypasses the 5-minute cache (manual refresh button):
    the user's tap always yields a genuine upstream fetch.
    """
    ids = ",".join(sorted({i.strip().upper() for i in icaos
                           if i and len(i.strip()) == 4}))
    if not ids:
        return []

    now = time.time()
    hit = _cache.get(ids)
    if hit and now - hit[0] < CACHE_TTL and not force:
        return hit[1]

    url = API_URL.format(urllib.parse.urlencode({"ids": ids, "format": "json"}))
    req = urllib.request.Request(
        url, headers={"User-Agent": "MSFS-Tablet-Tracker/1.0"})
    with urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    out = []
    for m in data:
        out.append({
            "icao": m.get("icaoId"),
            "name": m.get("name"),
            "raw": m.get("rawOb"),
            "time": m.get("reportTime") or m.get("obsTime"),
            "flt_cat": m.get("fltCat"),        # VFR / MVFR / IFR / LIFR
            "temp_c": m.get("temp"),
            "dewp_c": m.get("dewp"),
            "wind_dir": m.get("wdir"),         # degrees or "VRB"
            "wind_kt": m.get("wspd"),
            "gust_kt": m.get("wgst"),
            "visib": m.get("visib"),           # statute miles, e.g. 10 or "6+"
            "altim": m.get("altim"),           # QNH in hPa
        })
    _cache[ids] = (now, out)
    return out
