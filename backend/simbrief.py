"""
SimBrief integration.

Fetches the user's latest generated flight plan via the public API:
    https://www.simbrief.com/api/xml.fetcher.php?userid=<ID>&json=1

This is the ONLY runtime Internet call of the application
(besides the initial airport database download).
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from net import urlopen

SIMBRIEF_URL = "https://www.simbrief.com/api/xml.fetcher.php?{}&json=1"


def fetch_simbrief_plan(user_id: str) -> dict | None:
    """
    Returns a normalized flight plan:
    {
      origin/destination: {icao, name, lat, lon},
      route: "DCT XYZ ...",
      waypoints: [{ident, lat, lon, alt_ft, fir}, ...],  # incl. dep & arr
      distance_nm, cruise_alt_ft, callsign, firs
    }
    or None on error.
    """
    if not user_id:
        return None
    # The API accepts userid (numeric) or username (SimBrief alias)
    param = "userid" if user_id.isdigit() else "username"
    url = SIMBRIEF_URL.format(urllib.parse.urlencode({param: user_id}))
    try:
        with urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[SimBrief] Network/parsing error: {e}")
        return None

    if "origin" not in data or "navlog" not in data:
        return None

    def airport(node):
        return {
            "icao": node.get("icao_code", "????"),
            "name": node.get("name", ""),
            "lat": float(node.get("pos_lat", 0)),
            "lon": float(node.get("pos_long", 0)),
        }

    origin = airport(data["origin"])
    destination = airport(data["destination"])

    # Navlog waypoints (entries without valid coordinates are skipped;
    # computed points like TOC/TOD are kept — useful for progress).
    fixes = data["navlog"].get("fix", [])
    if isinstance(fixes, dict):  # single fix → the API returns an object
        fixes = [fixes]

    waypoints = [{
        "ident": origin["icao"], "lat": origin["lat"], "lon": origin["lon"],
        "alt_ft": 0,
    }]
    for fx in fixes:
        try:
            waypoints.append({
                "ident": fx.get("ident", ""),
                "lat": float(fx["pos_lat"]),
                "lon": float(fx["pos_long"]),
                "alt_ft": int(float(fx.get("altitude_feet", 0))),
                # FIR (flight information region) crossed at this point
                "fir": (fx.get("fir") or "").strip().upper(),
            })
        except (KeyError, ValueError, TypeError):
            continue
    waypoints.append({
        "ident": destination["icao"], "lat": destination["lat"],
        "lon": destination["lon"], "alt_ft": 0,
    })

    # Ordered sequence of FIRs along the route, no consecutive duplicates
    firs = []
    for w in waypoints:
        f = w.get("fir", "")
        if f and (not firs or firs[-1] != f):
            firs.append(f)

    # Planned fuel (for the live "fuel at destination vs reserve" check).
    # SimBrief reports fuel in the OFP units (kgs or lbs) → normalize to kg.
    fuel_node = data.get("fuel", {})
    units = (data.get("params", {}) or {}).get("units", "kgs")
    to_kg = 0.453592 if str(units).lower().startswith("lb") else 1.0
    try:
        plan_fuel = {
            "block_kg": round(float(fuel_node.get("plan_ramp", 0)) * to_kg),
            "reserve_kg": round(float(fuel_node.get("reserve", 0)) * to_kg),
        }
    except (TypeError, ValueError):
        plan_fuel = {"block_kg": 0, "reserve_kg": 0}

    general = data.get("general", {})
    return {
        "origin": origin,
        "destination": destination,
        "route": general.get("route", ""),
        "waypoints": waypoints,
        "distance_nm": int(float(general.get("route_distance", 0) or 0)),
        "cruise_alt_ft": int(float(general.get("initial_altitude", 0) or 0)),
        "callsign": data.get("atc", {}).get("callsign", ""),
        "firs": firs,
        "fuel": plan_fuel,
    }
