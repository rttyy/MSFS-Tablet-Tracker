"""
Flight card generator — a shareable PNG recap produced at the end of
each flight (1200×630, Discord/social-friendly ratio).

The route panel shows the flown track over a dark basemap (CARTO tiles,
stitched server-side in proper Web-Mercator projection, with the required
attribution). If the tiles can't be fetched (offline, blocked), the card
degrades gracefully to a plain panel — a card must never fail for a
network reason.
"""

from __future__ import annotations

import math
import urllib.request
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from net import urlopen

W, H = 1200, 630
BG = "#0b0f14"
PANEL = "#111a24"
TEXT = "#d7e6f2"
MUTED = "#7b8fa1"
ACCENT = "#35c8ff"
TRAIL = "#ff9500"

# Dark basemap (free with attribution). © OpenStreetMap contributors © CARTO
TILE_URL = "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"
TILE_ATTRIB = "© OpenStreetMap · © CARTO"

RATING_COLORS = {
    "BUTTER": "#38d97a", "SMOOTH": "#5cd6a0", "GOOD": "#35c8ff",
    "FIRM": "#ffb340", "HARD": "#ff4d5e", "CRASH?": "#d1273a",
}

_FONT_DIRS = [
    "C:/Windows/Fonts",                                # Windows (production)
    "/usr/share/fonts/truetype/dejavu",                # Linux (dev/test)
]
_FONT_FILES = {
    True: ["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"],   # bold
    False: ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"],
}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for d in _FONT_DIRS:
        for name in _FONT_FILES[bold]:
            p = Path(d) / name
            if p.exists():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


# ------------------------- Web-Mercator helpers ------------------------- #
def _merc(lat: float, lon: float, z: int) -> tuple:
    """lat/lon → global pixel coordinates at zoom z (256 px tiles)."""
    lat = max(-85.05, min(85.05, lat))
    n = 256 * (2 ** z)
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def _pick_zoom(lats, lons, box_w, box_h) -> int:
    """Largest zoom where the track bbox fits in ~80% of the panel."""
    for z in range(12, 2, -1):
        x0, y0 = _merc(max(lats), min(lons), z)
        x1, y1 = _merc(min(lats), max(lons), z)
        if abs(x1 - x0) <= box_w * 0.8 and abs(y1 - y0) <= box_h * 0.8:
            return z
    return 3


def _basemap(center_px, z, box_w, box_h) -> Image.Image | None:
    """Stitches tiles into a box_w×box_h image centered on center_px.
    Returns None on any failure (offline, HTTP error…)."""
    px0 = center_px[0] - box_w / 2
    py0 = center_px[1] - box_h / 2
    tx0, ty0 = int(px0 // 256), int(py0 // 256)
    tx1, ty1 = int((px0 + box_w) // 256), int((py0 + box_h) // 256)
    n_tiles = 2 ** z
    img = Image.new("RGB", (box_w, box_h), PANEL)
    try:
        for tx in range(tx0, tx1 + 1):
            for ty in range(max(0, ty0), min(n_tiles - 1, ty1) + 1):
                url = TILE_URL.format(z=z, x=tx % n_tiles, y=ty)
                req = urllib.request.Request(
                    url, headers={"User-Agent": "MSFS-Tablet-Tracker"})
                with urlopen(req, timeout=10) as resp:
                    tile = Image.open(BytesIO(resp.read())).convert("RGB")
                img.paste(tile, (int(tx * 256 - px0), int(ty * 256 - py0)))
        return img
    except Exception:
        return None


def _pctl(values, q: float):
    """Small percentile helper (spike-resistant maximum)."""
    if not values:
        return None
    vs = sorted(values)
    return vs[min(len(vs) - 1, int(q * len(vs)))]


def render_card(entry: dict, points: list, version: str = "",
                plan: dict | None = None) -> bytes:
    """Renders the flight card and returns PNG bytes."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Top accent line + header
    d.rectangle([0, 0, W, 6], fill=ACCENT)
    d.text((60, 30), "FLIGHT CARD", font=_font(22, True), fill=MUTED)
    date = str(entry.get("date", ""))
    f22 = _font(22)
    d.text((W - 60 - d.textlength(date, font=f22), 30), date,
           font=f22, fill=MUTED)

    # Route (idents + names + aircraft/callsign line)
    o = entry.get("origin") or {}
    dst = entry.get("destination") or {}
    oi = o.get("ident") or "????"
    di = dst.get("ident") or "????"
    d.text((60, 66), f"{oi}  →  {di}", font=_font(56, True), fill=TEXT)
    names = f"{o.get('name', 'Unknown')}  →  {dst.get('name', 'Unknown')}"
    if len(names) > 66:
        names = names[:63] + "…"
    d.text((60, 136), names, font=_font(20), fill=MUTED)
    craft_bits = []
    if entry.get("aircraft"):
        craft_bits.append(str(entry["aircraft"])[:48])
    if plan and plan.get("callsign"):
        craft_bits.append(str(plan["callsign"]))
    if craft_bits:
        d.text((60, 166), "  ·  ".join(craft_bits), font=_font(20, True),
               fill=ACCENT)

    # ---- Route panel: dark basemap + flown track (left) ----
    mx0, my0, mx1, my1 = 60, 214, 640, 566
    bw, bh = mx1 - mx0, my1 - my0
    pts = [(p["lat"], p["lon"]) for p in points
           if p.get("lat") is not None and p.get("lon") is not None]

    panel = Image.new("RGB", (bw, bh), PANEL)
    if len(pts) >= 2:
        lats = [p[0] for p in pts]
        lons = [p[1] for p in pts]
        z = _pick_zoom(lats, lons, bw, bh)
        cx0, cy0 = _merc(max(lats), min(lons), z)
        cx1, cy1 = _merc(min(lats), max(lons), z)
        center = ((cx0 + cx1) / 2, (cy0 + cy1) / 2)

        base = _basemap(center, z, bw, bh)
        if base is not None:
            # slight darkening so the orange trail pops
            panel = Image.blend(base, Image.new("RGB", base.size, BG), 0.22)
        pd = ImageDraw.Draw(panel)

        def xy(lat, lon):
            gx, gy = _merc(lat, lon, z)
            return (gx - (center[0] - bw / 2), gy - (center[1] - bh / 2))

        pd.line([xy(*p) for p in pts], fill=TRAIL, width=4, joint="curve")
        x0, y0 = xy(*pts[0])
        x1, y1 = xy(*pts[-1])
        pd.ellipse([x0 - 7, y0 - 7, x0 + 7, y0 + 7], fill="#38d97a",
                   outline=BG, width=2)
        end_col = RATING_COLORS.get(entry.get("rating", ""), ACCENT)
        pd.ellipse([x1 - 7, y1 - 7, x1 + 7, y1 + 7], fill=end_col,
                   outline=BG, width=2)
        if base is not None:
            f12 = _font(14)
            tw = pd.textlength(TILE_ATTRIB, font=f12)
            pd.text((bw - tw - 10, bh - 22), TILE_ATTRIB, font=f12,
                    fill="#9db0c0")
    else:
        pd = ImageDraw.Draw(panel)
        f = _font(22)
        t = "No track"
        pd.text(((bw - pd.textlength(t, font=f)) / 2, bh / 2 - 12),
                t, font=f, fill=MUTED)

    # rounded-corner paste
    mask = Image.new("L", (bw, bh), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, bw - 1, bh - 1],
                                           radius=12, fill=255)
    img.paste(panel, (mx0, my0), mask)
    d.rounded_rectangle([mx0, my0, mx1, my1], radius=12,
                        outline="#22303e", width=2)

    # ---- Derived stats from the track ----
    alts = [p.get("alt_m", 0) for p in points if p.get("alt_m") is not None]
    max_alt_ft = round(max(alts) / 0.3048) if alts else None
    # Max ground speed: prefer the sim-reported GS recorded in the track —
    # immune to sim-rate acceleration and pauses. Position/wall-clock
    # derivation is kept only for archives that predate the "gs" field,
    # with a median-based outlier filter to reject pause/teleport spikes.
    gss = [p.get("gs") for p in points if p.get("gs")]
    if gss:
        max_gs = _pctl(gss, 0.98)
    else:
        speeds = []
        for a, b in zip(points, points[1:]):
            try:
                dt = b["ts"] - a["ts"]
                if not 0.5 < dt < 30:
                    continue
                dlat = math.radians(b["lat"] - a["lat"])
                dlon = math.radians(b["lon"] - a["lon"])
                p1 = math.radians(a["lat"])
                h = (math.sin(dlat / 2) ** 2 +
                     math.cos(p1) * math.cos(math.radians(b["lat"])) *
                     math.sin(dlon / 2) ** 2)
                d_nm = 2 * 3440.065 * math.asin(math.sqrt(h))
                speeds.append(d_nm / (dt / 3600.0))
            except (KeyError, TypeError):
                continue
        med = _pctl(speeds, 0.5)
        if med:
            speeds = [v for v in speeds if v <= med * 3]
        max_gs = _pctl(speeds, 0.98)      # spike-resistant maximum
    dur = entry.get("duration_min") or 0
    avg_gs = round((entry.get("dist_nm") or 0) / (dur / 60)) if dur >= 5 else None
    # Legacy archives (no sim-recorded GS): position/wall-clock speeds are
    # inflated by sim-rate acceleration, which timestamps alone can't
    # reveal. When the figure is implausible against the block average,
    # show nothing rather than a wrong number.
    if not gss and max_gs and avg_gs and max_gs > avg_gs * 3:
        max_gs = None

    # Fuel used: from the logbook entry, or derived from the archived
    # track's fuel-on-board samples when the entry predates the feature
    # or the takeoff capture failed.
    fuel_used = entry.get("fuel_used_kg")
    if fuel_used is None:
        fuels = [p.get("fuel") for p in points if p.get("fuel")]
        if len(fuels) >= 2 and fuels[0] >= fuels[-1]:
            fuel_used = round(fuels[0] - fuels[-1])

    # ---- Stats grid (right side, 2 × 4) ----
    dur_s = f"{dur // 60}h{dur % 60:02d}" if dur >= 60 else f"{dur} min"
    stats = [
        ("DURATION", dur_s),
        ("DISTANCE", f"{round(entry.get('dist_nm') or 0)} NM"),
        ("MAX ALTITUDE", f"{max_alt_ft:,} ft".replace(",", " ")
         if max_alt_ft else "--"),
        ("MAX GROUND SPEED", f"{round(max_gs)} kt" if max_gs else "--"),
        ("TOUCHDOWN", f"{round(entry.get('touchdown_fpm') or 0)} fpm"),
        ("G-FORCE / SPEED",
         f"{entry.get('g_force') or '--'} G · {round(entry.get('ias_kt') or 0)} kt"),
        ("AVG BLOCK SPEED", f"{avg_gs} kt" if avg_gs else "--"),
        ("FUEL USED", f"{round(fuel_used):,} kg".replace(",", " ")
         if fuel_used is not None else "--"),
    ]
    gx0, gx1 = 690, 1140
    rows = [214, 302, 390, 478]
    cols = [gx0, (gx0 + gx1) // 2 + 16]
    for i, (label, value) in enumerate(stats):
        x = cols[i % 2]
        y = rows[i // 2]
        d.text((x, y), label, font=_font(16, True), fill=MUTED)
        d.text((x, y + 24), value, font=_font(33, True), fill=TEXT)

    # Rating badge (bottom of the stats column)
    rating = entry.get("rating", "")
    if rating:
        col = RATING_COLORS.get(rating, ACCENT)
        f = _font(24, True)
        tw = d.textlength(rating, font=f)
        bx, by = gx0, 556
        d.rounded_rectangle([bx, by, bx + tw + 36, by + 42], radius=21, fill=col)
        d.text((bx + 18, by + 8), rating, font=f, fill="#0b0f14")

    # Footer
    footer = f"MSFS Tablet Tracker {('v' + version) if version else ''}"
    f18 = _font(18)
    d.text((W - 60 - d.textlength(footer, font=f18), H - 38), footer,
           font=f18, fill=MUTED)

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
