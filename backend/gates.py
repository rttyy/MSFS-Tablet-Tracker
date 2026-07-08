"""
Boarding gates and aircraft parking stands fetched from OpenStreetMap
via the Overpass API (free, no key).

Major airports are very well mapped in OSM: every gate is an
`aeroway=gate` node with its name in the `ref` tag (e.g. "A12"),
and every stand an `aeroway=parking_position`.

Disk cache (data/gates_cache.json) with a 30-day TTL: each area is
requested from the Internet only once, then everything stays local.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from net import urlopen
from pathlib import Path

# Public Overpass servers (tried in order if one is down)
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
MAX_SPAN_DEG = 0.35        # max requested area (~20 NM): forces the client
                           # to be zoomed onto a specific airport
CACHE_TTL = 30 * 24 * 3600  # 30 days: gates rarely change


class GateDB:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "gates_cache.json"
        try:
            self.cache = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self.cache = {}

    def get_gates(self, south: float, west: float, north: float, east: float) -> list:
        """Returns [{name, lat, lon, kind}] for the requested area."""
        if north - south > MAX_SPAN_DEG or east - west > MAX_SPAN_DEG:
            raise ValueError("area too large — zoom onto an airport")

        # Rounded cache key: slight zoom/pan changes reuse the same entry
        # instead of hitting Overpass again.
        key = f"{south:.2f},{west:.2f},{north:.2f},{east:.2f}"
        hit = self.cache.get(key)
        if hit and time.time() - hit["t"] < CACHE_TTL:
            return hit["g"]

        bbox = f"{south},{west},{north},{east}"
        query = (
            "[out:json][timeout:25];("
            f'node["aeroway"="gate"]({bbox});'
            f'node["aeroway"="parking_position"]({bbox});'
            f'way["aeroway"="parking_position"]({bbox});'
            ");out center 1500;"
        )

        data, last_err = None, None
        for url in OVERPASS_URLS:
            try:
                req = urllib.request.Request(
                    url,
                    data=urllib.parse.urlencode({"data": query}).encode(),
                    headers={"User-Agent": "MSFS-Tablet-Tracker/1.0"},
                )
                with urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except Exception as e:
                last_err = e
        if data is None:
            raise RuntimeError(f"Overpass unreachable: {last_err}")

        gates = []
        for el in data.get("elements", []):
            lat = el.get("lat") or el.get("center", {}).get("lat")
            lon = el.get("lon") or el.get("center", {}).get("lon")
            if lat is None or lon is None:
                continue
            tags = el.get("tags", {})
            gates.append({
                # Gate name: ref tag ("A12"), else name, else "?"
                "name": tags.get("ref") or tags.get("name") or "?",
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                # gate = terminal gate, parking = aircraft stand
                "kind": "gate" if tags.get("aeroway") == "gate" else "parking",
            })

        self.cache[key] = {"t": time.time(), "g": gates}
        try:
            self.path.write_text(json.dumps(self.cache), encoding="utf-8")
        except Exception:
            pass  # disk cache is not critical
        return gates
