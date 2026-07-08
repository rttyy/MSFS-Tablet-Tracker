"""
Airport, runway and frequency database — source: OurAirports (public domain).

On first launch the CSVs are downloaded then cached in data/.
Everything works offline afterwards (LAN constraint respected: a single
one-time download, like SimBrief).

Note on taxiways: OurAirports does not provide taxiway geometry.
They are visible client-side through the OpenStreetMap tiles at high zoom
(taxiways are mapped in OSM for virtually every major airport); named
gates and parking stands come from the gates.py module (Overpass).
"""

from __future__ import annotations

import csv
import math
import urllib.request

from net import urlopen
from geo import haversine_nm as _haversine_nm
from pathlib import Path

# Primary URLs + fallback mirrors (tried in order)
AIRPORTS_URLS = [
    "https://davidmegginson.github.io/ourairports-data/airports.csv",
    "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/airports.csv",
]
RUNWAYS_URLS = [
    "https://davidmegginson.github.io/ourairports-data/runways.csv",
    "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/runways.csv",
]
FREQS_URLS = [
    "https://davidmegginson.github.io/ourairports-data/airport-frequencies.csv",
    "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/airport-frequencies.csv",
]

# Logical display order for frequencies (ground to en-route)
FREQ_ORDER = {"ATIS": 0, "AWOS": 0, "ASOS": 0, "DEL": 1, "CLD": 1, "GND": 2,
              "TWR": 3, "CTAF": 3, "UNIC": 4, "APP": 5, "A/D": 5, "DEP": 6,
              "CTR": 7, "RDO": 8, "FSS": 8}

# Airport types kept (heliports, closed seaplane bases etc. are ignored)
KEEP_TYPES = {"large_airport", "medium_airport", "small_airport"}


class AirportDB:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.airports: list[dict] = []
        self.runways_by_airport: dict[str, list] = {}
        self.freqs_by_airport: dict[str, list] = {}
        self.loaded = False

    # ------------------------------------------------------------------ #
    def ensure_loaded(self):
        """Downloads (if needed) then loads the CSVs into memory."""
        if self.loaded:
            return
        try:
            ap_file = self._get_file("airports.csv", AIRPORTS_URLS)
            rw_file = self._get_file("runways.csv", RUNWAYS_URLS)
            fq_file = self._get_file("airport-frequencies.csv", FREQS_URLS)
            self._load(ap_file, rw_file, fq_file)
            self.loaded = True
            print(f"[Airports] {len(self.airports)} airports loaded.")
        except Exception as e:
            print(f"[Airports] Could not load the database: {e}")
            print("[Airports] Airports won't be displayed (everything else works).")

    def _get_file(self, name: str, urls: list[str]) -> Path:
        """Downloads the file from the first URL that responds (with UA)."""
        path = self.data_dir / name
        if path.exists():
            return path
        print(f"[Airports] Downloading {name} (one time only)…")
        last_err = None
        for url in urls:
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "MSFS-Tablet-Tracker/1.0"})
                with urlopen(req, timeout=60) as resp:
                    path.write_bytes(resp.read())
                return path
            except Exception as e:
                last_err = e
        raise RuntimeError(f"failed to download {name}: {last_err}")

    def _load(self, ap_file: Path, rw_file: Path, fq_file: Path):
        with open(ap_file, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["type"] not in KEEP_TYPES:
                    continue
                try:
                    self.airports.append({
                        "ident": row["ident"],
                        "name": row["name"],
                        "lat": float(row["latitude_deg"]),
                        "lon": float(row["longitude_deg"]),
                        "type": row["type"],
                    })
                except ValueError:
                    continue
        # Sort by significance: when an area holds too many airports, the
        # cap keeps large airports first, then medium ones.
        _prio = {"large_airport": 0, "medium_airport": 1, "small_airport": 2}
        self.airports.sort(key=lambda a: _prio[a["type"]])

        with open(rw_file, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    rw = {
                        "le_ident": row["le_ident"], "he_ident": row["he_ident"],
                        "le_lat": float(row["le_latitude_deg"]),
                        "le_lon": float(row["le_longitude_deg"]),
                        "he_lat": float(row["he_latitude_deg"]),
                        "he_lon": float(row["he_longitude_deg"]),
                        "length_ft": int(float(row["length_ft"] or 0)),
                        "surface": row["surface"],
                        "closed": row["closed"] == "1",
                    }
                except ValueError:
                    continue  # runway ends without coordinates
                self.runways_by_airport.setdefault(row["airport_ident"], []).append(rw)

        with open(fq_file, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    self.freqs_by_airport.setdefault(row["airport_ident"], []).append({
                        "type": row["type"].upper(),
                        "desc": row["description"],
                        "mhz": float(row["frequency_mhz"]),
                    })
                except (ValueError, KeyError):
                    continue

    # ------------------------------------------------------------------ #
    def nearby(self, lat: float, lon: float, radius_nm: float = 60) -> list[dict]:
        """Airports (with their runways) within a given radius in NM."""
        if not self.loaded:
            return []
        out = []
        # Fast rectangular pre-filter before the exact haversine
        dlat = radius_nm / 60.0
        dlon = dlat / max(0.2, math.cos(math.radians(lat)))
        for ap in self.airports:
            if abs(ap["lat"] - lat) > dlat or abs(ap["lon"] - lon) > dlon:
                continue
            d = _haversine_nm(lat, lon, ap["lat"], ap["lon"])
            if d <= radius_nm:
                entry = dict(ap)
                entry["dist_nm"] = round(d, 1)
                entry["runways"] = [
                    r for r in self.runways_by_airport.get(ap["ident"], [])
                    if not r["closed"]
                ]
                out.append(entry)
        # Closest first, capped so the map never gets overloaded
        out.sort(key=lambda a: a["dist_nm"])
        return out[:40]

    # ------------------------------------------------------------------ #
    def in_bbox(self, south, west, north, east, zoom: int,
                include_runways: bool, limit: int = 400) -> list:
        """
        Airports visible in the map viewport, filtered by significance
        based on zoom (worldwide coverage without flooding the tablet):
        - zoom < 5  : large airports only
        - zoom 5-7  : large + medium
        - zoom >= 8 : everything, small airfields included
        Since the list is sorted large→small at load time, the `limit`
        cap always keeps the most significant ones.
        """
        if not self.loaded:
            return []
        if zoom < 5:
            types = {"large_airport"}
        elif zoom < 8:
            types = {"large_airport", "medium_airport"}
        else:
            types = KEEP_TYPES
        out = []
        for ap in self.airports:
            if ap["type"] not in types:
                continue
            if not (south <= ap["lat"] <= north and west <= ap["lon"] <= east):
                continue
            entry = dict(ap)
            if include_runways:
                entry["runways"] = [
                    r for r in self.runways_by_airport.get(ap["ident"], [])
                    if not r["closed"]
                ]
            out.append(entry)
            if len(out) >= limit:
                break
        return out

    def frequencies(self, icao: str) -> list:
        """Airport frequencies, sorted ATIS → DEL → GND → TWR → APP…"""
        freqs = list(self.freqs_by_airport.get(icao.upper(), []))
        freqs.sort(key=lambda f: (FREQ_ORDER.get(f["type"], 9), f["mhz"]))
        return freqs

    def get(self, icao: str):
        """Airport record by ICAO code, or None."""
        icao = icao.upper()
        for ap in self.airports:
            if ap["ident"] == icao:
                return ap
        return None

    def nearest(self, lat: float, lon: float):
        """Closest airport (used by the logbook)."""
        if not self.loaded:
            return None
        best, best_d = None, 1e9
        for ap in self.airports:
            if abs(ap["lat"] - lat) > 1.5 or abs(ap["lon"] - lon) > 1.5:
                continue
            d = _haversine_nm(lat, lon, ap["lat"], ap["lon"])
            if d < best_d:
                best, best_d = ap, d
        return best
