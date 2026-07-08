"""
Achievements — computed from the logbook entries.

Every badge is a small pure function of the entries list: no state, no
cache, cheap to recompute at every /api/achievements request. That way
adding a new badge only means adding a row to ACHIEVEMENTS below.
"""

from __future__ import annotations

# ICAO prefix → country. Two-letter prefixes are looked up first, then the
# single letter as a fallback (only used where the whole letter block IS a
# single country: K = USA, C = Canada, Y = Australia, Z = China…).
# See https://en.wikipedia.org/wiki/ICAO_airport_code
_COUNTRY_PREFIX = {
    # ---- Europe (E, L, B) ----
    "LF": "France", "LE": "Spain", "LI": "Italy", "LP": "Portugal",
    "EG": "United Kingdom", "EI": "Ireland", "ED": "Germany", "ET": "Germany",
    "EB": "Belgium", "EH": "Netherlands", "EL": "Luxembourg",
    "LS": "Switzerland", "LO": "Austria", "LK": "Czechia", "LZ": "Slovakia",
    "LH": "Hungary", "LR": "Romania", "LB": "Bulgaria", "LG": "Greece",
    "LT": "Turkey", "LC": "Cyprus", "LM": "Malta",
    "LD": "Croatia", "LJ": "Slovenia", "LQ": "Bosnia and Herzegovina",
    "LY": "Serbia/Montenegro", "LW": "North Macedonia", "LA": "Albania",
    "LX": "Gibraltar", "LN": "Monaco",
    "EK": "Denmark", "EN": "Norway", "ES": "Sweden", "EF": "Finland",
    "EP": "Poland", "EE": "Estonia", "EV": "Latvia", "EY": "Lithuania",
    "BI": "Iceland", "BG": "Greenland", "BK": "Kosovo",
    # ---- North America ----
    "K": "United States", "PA": "United States", "PH": "United States",
    "PG": "United States", "TJ": "United States",
    "C": "Canada",
    # ---- Mexico, Central America & Caribbean (M, T) ----
    "MM": "Mexico", "MU": "Cuba", "MG": "Guatemala", "MH": "Honduras",
    "MK": "Jamaica", "MP": "Panama", "MZ": "Belize", "MS": "El Salvador",
    "MR": "Costa Rica", "MN": "Nicaragua", "MD": "Dominican Republic",
    "MT": "Haiti", "MY": "Bahamas",
    "TB": "Barbados", "TT": "Trinidad and Tobago", "TF": "French Antilles",
    # ---- South America (S) ----
    "SA": "Argentina", "SC": "Chile", "SP": "Peru", "SK": "Colombia",
    "SE": "Ecuador", "SV": "Venezuela", "SU": "Uruguay", "SG": "Paraguay",
    "SL": "Bolivia", "SM": "Suriname", "SO": "French Guiana", "SY": "Guyana",
    "SB": "Brazil", "SD": "Brazil", "SI": "Brazil", "SJ": "Brazil",
    "SN": "Brazil", "SS": "Brazil", "SW": "Brazil",
    # ---- Asia (R, V, W, Z, U, O) ----
    "RJ": "Japan", "RO": "Japan", "RK": "South Korea",
    "RC": "Taiwan", "RP": "Philippines",
    "VT": "Thailand", "VH": "Hong Kong", "VM": "Macau", "VV": "Vietnam",
    "VD": "Cambodia", "VL": "Laos", "VY": "Myanmar", "VN": "Nepal",
    "VG": "Bangladesh", "VC": "Sri Lanka", "VR": "Maldives", "VQ": "Bhutan",
    "VA": "India", "VE": "India", "VI": "India", "VO": "India",
    "WS": "Singapore", "WM": "Malaysia", "WB": "Malaysia/Brunei",
    "WA": "Indonesia", "WI": "Indonesia", "WQ": "Indonesia",
    "WR": "Indonesia", "WP": "Timor-Leste",
    "ZK": "North Korea", "ZM": "Mongolia", "Z": "China",
    "UK": "Ukraine", "UM": "Belarus", "UB": "Azerbaijan", "UD": "Armenia",
    "UG": "Georgia", "UT": "Central Asia", "UA": "Kazakhstan",
    "U": "Russia",
    "OE": "Saudi Arabia", "OM": "United Arab Emirates", "OB": "Bahrain",
    "OK": "Kuwait", "OT": "Qatar", "OO": "Oman", "OY": "Yemen",
    "OI": "Iran", "OR": "Iraq", "OJ": "Jordan", "OL": "Lebanon",
    "OS": "Syria", "OP": "Pakistan", "OA": "Afghanistan",
    # ---- Africa (H, D, G, F) ----
    "HE": "Egypt", "HL": "Libya", "HA": "Ethiopia", "HK": "Kenya",
    "HT": "Tanzania", "HU": "Uganda", "HS": "Sudan", "HC": "Somalia",
    "HR": "Rwanda", "HB": "Burundi", "HD": "Djibouti",
    "DA": "Algeria", "DT": "Tunisia", "GM": "Morocco", "GC": "Spain",
    "DN": "Nigeria", "DG": "Ghana", "DI": "Ivory Coast", "DR": "Niger",
    "DF": "Burkina Faso", "DB": "Benin", "DX": "Togo",
    "GA": "Mali", "GO": "Senegal", "GU": "Guinea", "GB": "Gambia",
    "GF": "Sierra Leone", "GL": "Liberia", "GV": "Cape Verde",
    "FA": "South Africa", "FQ": "Mozambique", "FV": "Zimbabwe",
    "FB": "Botswana", "FY": "Namibia", "FZ": "DR Congo", "FL": "Zambia",
    "FW": "Malawi", "FM": "Madagascar/Comoros", "FI": "Mauritius",
    # ---- Oceania (Y, N, A, P) ----
    "Y": "Australia", "NZ": "New Zealand", "NF": "Fiji",
    "NT": "French Polynesia", "NW": "New Caledonia", "NV": "Vanuatu",
    "NC": "Cook Islands", "AY": "Papua New Guinea", "AG": "Solomon Islands",
}


def _country_of(icao: str) -> str | None:
    if not icao or len(icao) < 2:
        return None
    return _COUNTRY_PREFIX.get(icao[:2]) or _COUNTRY_PREFIX.get(icao[:1])


def _stats(entries: list) -> dict:
    """One pass over the entries, everything the badges may need."""
    origins = set()
    dests = set()
    airports = set()
    countries = set()
    total_min = 0.0
    total_nm = 0.0
    tds = []                          # touchdown_fpm values (absolute)
    butter = smooth = crash = 0
    longest_nm = 0.0
    highest_ft = 0
    night_flights = 0                 # a landing between 20:00 and 06:00
    dawn_flights = 0                  # takeoff (proxy: date hour) 04-06
    for e in entries:
        o = (e.get("origin") or {}).get("ident")
        d = (e.get("destination") or {}).get("ident")
        if o: origins.add(o); airports.add(o); countries.add(_country_of(o))
        if d: dests.add(d); airports.add(d); countries.add(_country_of(d))
        total_min += e.get("duration_min", 0) or 0
        nm = e.get("dist_nm", 0) or 0
        total_nm += nm
        longest_nm = max(longest_nm, nm)
        td = e.get("touchdown_fpm")
        if td is not None:
            tds.append(abs(td))
        rating = e.get("rating", "")
        if rating == "BUTTER": butter += 1
        if rating in ("BUTTER", "SMOOTH"): smooth += 1
        if rating == "CRASH?": crash += 1
        # Day/night from the LOCAL solar time at the destination: the
        # logbook date is UTC, so shift by longitude (15°/h). Entries
        # without coordinates (older versions) fall back to UTC.
        date = str(e.get("date", ""))
        try:
            hh = float(int(date[11:13]))
            lon = (e.get("destination") or {}).get("lon")
            if lon is not None:
                hh = (hh + lon / 15.0) % 24
            if hh >= 20 or hh < 6: night_flights += 1
            if 4 <= hh < 6: dawn_flights += 1
        except (ValueError, IndexError, TypeError):
            pass
    countries.discard(None)
    return {
        "count": len(entries),
        "origins": origins, "dests": dests,
        "airports": airports, "countries": countries,
        "total_hours": total_min / 60.0,
        "total_nm": total_nm,
        "longest_nm": longest_nm,
        "butter": butter, "smooth": smooth, "crash": crash,
        "night_flights": night_flights,
        "dawn_flights": dawn_flights,
        # earliest-first date lookup, for "unlocked on" stamps
        "entries_sorted": sorted(entries, key=lambda x: str(x.get("date", ""))),
    }


# --------------------------- Achievement list --------------------------- #
# Each row: (id, emoji, name, description, tier, unlock(stats)->bool,
#            progress(stats)->(current, target))
# Progress fields power the % bars; unlock() gets the final say.

def _unlock_at(n):     return lambda s: s["count"] >= n
def _progress_count(n): return lambda s: (min(s["count"], n), n)


ACHIEVEMENTS = [
    # ---- Milestones ----
    ("first_flight", "🛫", "First flight",
     "Complete your very first archived flight.",
     "bronze", _unlock_at(1), _progress_count(1)),
    ("ten_flights", "✈️", "Frequent flyer",
     "Complete 10 flights.",
     "bronze", _unlock_at(10), _progress_count(10)),
    ("fifty_flights", "🛬", "Regular",
     "Complete 50 flights.",
     "silver", _unlock_at(50), _progress_count(50)),
    ("hundred_flights", "🏆", "Centurion",
     "Complete 100 flights.",
     "gold", _unlock_at(100), _progress_count(100)),
    ("five_hundred", "💎", "Airline material",
     "Complete 500 flights.",
     "platinum", _unlock_at(500), _progress_count(500)),

    # ---- Time / distance ----
    ("ten_hours", "⏱️", "Ten hours in the air",
     "Log 10 hours of total flight time.",
     "bronze",
     lambda s: s["total_hours"] >= 10,
     lambda s: (round(min(s["total_hours"], 10), 1), 10)),
    ("hundred_hours", "🕐", "Iron pilot",
     "Log 100 hours of total flight time.",
     "gold",
     lambda s: s["total_hours"] >= 100,
     lambda s: (round(min(s["total_hours"], 100), 1), 100)),
    ("long_haul", "🌍", "Long haul",
     "Complete a single flight of at least 2,000 NM.",
     "gold",
     lambda s: s["longest_nm"] >= 2000,
     lambda s: (round(min(s["longest_nm"], 2000)), 2000)),
    ("marathon", "📏", "Marathon",
     "Fly 10,000 NM in total.",
     "silver",
     lambda s: s["total_nm"] >= 10000,
     lambda s: (round(min(s["total_nm"], 10000)), 10000)),

    # ---- Destinations ----
    ("world_tour_5", "🗺️", "Wanderer",
     "Visit 5 different airports.",
     "bronze",
     lambda s: len(s["airports"]) >= 5,
     lambda s: (min(len(s["airports"]), 5), 5)),
    ("world_tour_25", "🧭", "Runway roulette",
     "Visit 25 different airports.",
     "silver",
     lambda s: len(s["airports"]) >= 25,
     lambda s: (min(len(s["airports"]), 25), 25)),
    ("world_tour_100", "🌐", "Grand tour",
     "Visit 100 different airports.",
     "gold",
     lambda s: len(s["airports"]) >= 100,
     lambda s: (min(len(s["airports"]), 100), 100)),
    ("countries_5", "🚩", "Passport stamps",
     "Fly to 5 different countries.",
     "silver",
     lambda s: len(s["countries"]) >= 5,
     lambda s: (min(len(s["countries"]), 5), 5)),
    ("countries_15", "🎌", "Globetrotter",
     "Fly to 15 different countries.",
     "gold",
     lambda s: len(s["countries"]) >= 15,
     lambda s: (min(len(s["countries"]), 15), 15)),

    # ---- Landings ----
    ("first_butter", "🧈", "First butter",
     "Land under 60 fpm at touchdown.",
     "bronze",
     lambda s: s["butter"] >= 1,
     lambda s: (min(s["butter"], 1), 1)),
    ("butter_master", "🎯", "Butter master",
     "Rack up 10 BUTTER landings.",
     "gold",
     lambda s: s["butter"] >= 10,
     lambda s: (min(s["butter"], 10), 10)),
    ("smooth_operator", "👌", "Smooth operator",
     "Complete 25 flights ending SMOOTH or better.",
     "silver",
     lambda s: s["smooth"] >= 25,
     lambda s: (min(s["smooth"], 25), 25)),

    # ---- Specialties ----
    ("night_owl", "🌙", "Night owl",
     "Complete 5 flights ending between 20:00 and 06:00.",
     "silver",
     lambda s: s["night_flights"] >= 5,
     lambda s: (min(s["night_flights"], 5), 5)),
    ("early_bird", "🌅", "Early bird",
     "Complete 3 flights that finish before 06:00.",
     "bronze",
     lambda s: s["dawn_flights"] >= 3,
     lambda s: (min(s["dawn_flights"], 3), 3)),
    ("round_trip", "🔁", "Round trip",
     "Depart from and return to the same airport in different flights.",
     "bronze",
     lambda s: bool(s["origins"] & s["dests"]),
     lambda s: (1 if s["origins"] & s["dests"] else 0, 1)),
    ("survivor", "🚑", "Walk-away landing",
     "Rated CRASH? but the logbook lived to tell the tale.",
     "bronze",
     lambda s: s["crash"] >= 1,
     lambda s: (min(s["crash"], 1), 1)),
]


def compute(entries: list) -> dict:
    """Returns {unlocked_count, total, badges: [{...}]}."""
    stats = _stats(entries)
    badges = []
    for aid, emoji, name, desc, tier, is_unlocked, progress in ACHIEVEMENTS:
        cur, tgt = progress(stats)
        unlocked = is_unlocked(stats)
        badges.append({
            "id": aid, "emoji": emoji, "name": name, "description": desc,
            "tier": tier, "unlocked": unlocked,
            "progress": cur, "target": tgt,
            "unlocked_on": _unlock_date(aid, stats) if unlocked else None,
        })
    return {"unlocked": sum(1 for b in badges if b["unlocked"]),
            "total": len(badges), "badges": badges}


def _unlock_date(aid: str, stats: dict) -> str | None:
    """
    Best-effort earliest date at which this achievement crossed its
    threshold. Approximate: walks entries from oldest and returns the
    first date at which unlock() would have fired.
    """
    ordered = stats["entries_sorted"]
    for i in range(1, len(ordered) + 1):
        try:
            sub = _stats(ordered[:i])
            row = next(r for r in ACHIEVEMENTS if r[0] == aid)
            if row[5](sub):
                return str(ordered[i - 1].get("date", ""))
        except Exception:
            return None
    return None


def newly_unlocked(before: list, after: list) -> list:
    """Returns badges present in `after` but not in `before` — used to
    surface newcomers on the landing card."""
    b = {x["id"] for x in compute(before)["badges"] if x["unlocked"]}
    return [x for x in compute(after)["badges"]
            if x["unlocked"] and x["id"] not in b]
