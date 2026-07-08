"""
Aircraft data source.

Live reading from MSFS 2020/2024 (Python-SimConnect library), with
automatic reconnection if the simulator restarts.

All reads are non-blocking for MSFS: Python-SimConnect maintains a cache
refreshed by the sim itself; we only read from it.
"""

from __future__ import annotations

import math
import threading
import time


class SimSource:
    RECONNECT_DELAY = 5.0  # seconds between connection attempts
    # A single failed frame is NOT a lost connection: python-SimConnect
    # routinely returns None for a SimVar on an isolated frame (cache not
    # yet refreshed, sim paused a split second, aircraft change). Tearing
    # the SimConnect object down and recreating it over such a glitch is
    # exactly what sends it into an unrecoverable "connection lost" spiral
    # (it reconnects badly while its previous background thread is still
    # alive). Only declare the connection lost after reads keep failing for
    # this long — a real MSFS shutdown fails continuously, a glitch doesn't.
    READ_GRACE_S = 8.0

    def __init__(self):
        self.sm = None            # SimConnect instance
        self.aq = None            # AircraftRequests
        self.status = "starting"
        self._last_attempt = 0.0
        self._first_fail = None   # time of the first read failure in a streak
        # Vertical speed computed as an altitude derivative: the
        # VERTICAL_SPEED SimVar is returned either in ft/s or ft/min
        # depending on the Python-SimConnect version (leading to values
        # 60x too large). Deriving it ourselves is always correct.
        self._vs_fpm = 0.0
        self._vs_raw_fpm = 0.0   # unsmoothed, for the touchdown rate
        self._prev_alt = None  # (timestamp, altitude_ft)
        # Fuel flow, also derived (kg/h) — works on every aircraft without
        # relying on per-engine fuel-flow SimVars.
        self._fuel_flow_kgh = 0.0
        self._prev_fuel = None  # (timestamp, fuel_kg)
        self._events = None     # AircraftEvents (lazy, for radio tuning)

    # ------------------------------------------------------------------ #
    #  Connection / reconnection                                         #
    # ------------------------------------------------------------------ #
    def _try_connect(self) -> bool:
        """Attempts to open SimConnect. Returns True when connected."""
        now = time.time()
        if now - self._last_attempt < self.RECONNECT_DELAY:
            return False
        self._last_attempt = now
        try:
            from SimConnect import SimConnect, AircraftRequests
            self.sm = SimConnect()
            # _time=200 ms: internal cache refresh rate.
            # Plenty for 5 Hz without loading MSFS.
            self.aq = AircraftRequests(self.sm, _time=200)
            self.status = "connected to MSFS"
            print("[SimConnect] Connected to MSFS.")
            return True
        except Exception as e:
            self.sm = None
            self.aq = None
            self.status = f"MSFS not found ({type(e).__name__}) — retrying…"
            return False

    def _disconnect(self):
        # Drop our references first so read_state reconnects cleanly even if
        # the close below never returns.
        sm = self.sm
        self.sm = None
        self.aq = None
        self._events = None
        if sm is None:
            return
        # sm.exit() joins python-SimConnect's internal receiver thread, which
        # can itself hang when the sim died mid-message — exactly the moment
        # _disconnect runs. read_state executes in the broadcast loop's worker
        # thread, so a stuck exit() would freeze the whole live feed. Close in
        # a throwaway daemon thread and stop waiting after a short timeout; an
        # abandoned close thread is harmless (daemon → dies with the process).
        def _close():
            try:
                sm.exit()
            except Exception:
                pass
        t = threading.Thread(target=_close, daemon=True)
        t.start()
        t.join(timeout=3.0)
        if t.is_alive():
            print("[SimConnect] Old connection won't close — abandoning it "
                  "and reconnecting anyway.")

    # ------------------------------------------------------------------ #
    #  State reading                                                     #
    # ------------------------------------------------------------------ #
    def read_state(self) -> dict | None:
        """Returns an aircraft state dict, or None if no data is available."""
        if self.aq is None and not self._try_connect():
            return None

        try:
            g = self.aq.get  # shortcut

            lat = g("PLANE_LATITUDE")
            lon = g("PLANE_LONGITUDE")
            if lat is None or lon is None:
                raise ValueError("position data unavailable")

            alt_ft = _f(g("PLANE_ALTITUDE"))
            state = {
                "connected": True,
                "lat": float(lat),
                "lon": float(lon),
                # Altitudes in feet
                "alt_ft": alt_ft,
                "alt_agl_ft": _f(g("PLANE_ALT_ABOVE_GROUND")),
                # Magnetic heading in degrees (SimConnect returns radians)
                "hdg_deg": math.degrees(_f(g("PLANE_HEADING_DEGREES_MAGNETIC"))) % 360,
                "hdg_true_deg": math.degrees(_f(g("PLANE_HEADING_DEGREES_TRUE"))) % 360,
                # True ground TRACK (course over ground): with crosswind it
                # differs from the heading — it's the honest direction for
                # the prediction vector. None when the SimVar is missing so
                # the client can fall back to the heading.
                "trk_true_deg": _track_deg(g("GPS_GROUND_TRUE_TRACK")),
                # Speeds
                "gs_kt": _f(g("GROUND_VELOCITY")),          # knots
                "ias_kt": _f(g("AIRSPEED_INDICATED")),      # knots
                "vs_fpm": self._compute_vs(alt_ft),         # smoothed derivative
                # Raw instantaneous V/S: noisier, but it's the honest value
                # at touchdown (the smoothed one lags behind the flare and
                # over-reports the descent rate — the -560 vs -200 fpm bug)
                "vs_raw_fpm": round(self._vs_raw_fpm, 1),
                # Load factor (for the landing rating)
                "g_force": _f(g("G_FORCE")) or 1.0,
                # Ground / airborne status
                "on_ground": bool(g("SIM_ON_GROUND")),
                # Warnings (0/1 sim-side)
                "stall": bool(g("STALL_WARNING")),
                "overspeed": bool(g("OVERSPEED_WARNING")),
                # In-sim weather
                "wind_dir_deg": _f(g("AMBIENT_WIND_DIRECTION")),
                "wind_kt": _f(g("AMBIENT_WIND_VELOCITY")),
                "oat_c": _f(g("AMBIENT_TEMPERATURE")),
                "visibility_m": _f(g("AMBIENT_VISIBILITY")),
                "ts": time.time(),
            }

            # ---- Fuel (quantity in gallons → kg, derived flow, endurance)
            gal = _f(g("FUEL_TOTAL_QUANTITY"))
            wpg = _f(g("FUEL_WEIGHT_PER_GALLON")) or 6.7   # lbs/gal fallback
            fuel_kg = gal * wpg * 0.453592
            if fuel_kg <= 0.5:
                # Some study-level addons (Fenix, PMDG…) manage fuel in
                # their own systems and leave the gallons SimVar at 0;
                # the total-weight SimVar (lbs) is often maintained anyway.
                fuel_kg = _f(g("FUEL_TOTAL_QUANTITY_WEIGHT")) * 0.453592
            state["fuel_kg"] = round(fuel_kg, 1)
            state["fuel_flow_kgh"] = self._compute_fuel_flow(fuel_kg)
            state["endurance_min"] = (
                round(fuel_kg / state["fuel_flow_kgh"] * 60)
                if state["fuel_flow_kgh"] > 1 else None)

            # ---- Systems (PFD chips)
            # Actual gear position, not the handle: the GEAR chip must not
            # say "down" while the gear is still in transit. Falls back to
            # the handle when the position SimVar is unavailable.
            gp = g("GEAR_TOTAL_PCT_EXTENDED")
            if gp is not None:
                gpf = _f(gp)
                if gpf > 1.001:          # some lib versions return 0–100
                    gpf /= 100.0
                state["gear_down"] = gpf > 0.99
            else:
                state["gear_down"] = _f(g("GEAR_HANDLE_POSITION")) > 0.5
            # FLAPS_HANDLE_PERCENT is returned as a 0–1 fraction ("percent
            # over 100") by the library → scale it; keep raw values > 1 as
            # already-percent for library versions that differ.
            fl = _f(g("FLAPS_HANDLE_PERCENT"))
            state["flaps_pct"] = round(fl * 100 if fl <= 1.001 else fl)

            # ---- Aircraft name (for the logbook & flight cards)
            try:
                t = g("TITLE")
                if isinstance(t, bytes):
                    t = t.decode("utf-8", "ignore")
                state["aircraft"] = (t or "").strip() or None
            except Exception:
                state["aircraft"] = None

            # ---- COM1 radios (shown in the Frequencies panel)
            state["com1_active"] = round(_f(g("COM_ACTIVE_FREQUENCY:1")), 3)
            state["com1_stby"] = round(_f(g("COM_STANDBY_FREQUENCY:1")), 3)

            # A good frame clears any pending failure streak.
            self._first_fail = None
            return state
        except Exception as e:
            # Distinguish a transient read glitch from a real disconnect:
            # keep the (still valid) SimConnect object during the grace
            # window and just skip this frame. Only after failures persist
            # do we tear down and reconnect — otherwise a one-frame None
            # would trigger an endless, unrecoverable reconnect spiral.
            now = time.time()
            if self._first_fail is None:
                self._first_fail = now
            if now - self._first_fail < self.READ_GRACE_S:
                self.status = "sim data glitch — holding connection…"
                return None
            print(f"[SimConnect] Connection lost after "
                  f"{now - self._first_fail:.0f}s of failed reads: {e}")
            self._disconnect()
            self._first_fail = None
            self.status = "connection lost — reconnecting automatically…"
            return None

    def _compute_fuel_flow(self, fuel_kg: float) -> float:
        """Fuel flow in kg/h derived from the quantity, heavily smoothed
        (consumption is slow, so a long time constant keeps it stable)."""
        now = time.time()
        if self._prev_fuel is not None:
            dt = now - self._prev_fuel[0]
            if 0.05 < dt < 10.0:
                burn = self._prev_fuel[1] - fuel_kg          # kg burned
                if burn >= 0:                                 # ignore refuels
                    raw = burn / dt * 3600.0
                    # dt-normalized EMA (τ ≈ 6.5 s): the smoothing behaves
                    # the same at every UPDATE_HZ setting.
                    alpha = 1.0 - math.exp(-dt / 6.5)
                    self._fuel_flow_kgh += alpha * (raw - self._fuel_flow_kgh)
        self._prev_fuel = (now, fuel_kg)
        return round(self._fuel_flow_kgh, 1)

    # ------------------------------------------------------------------ #
    #  Radio tuning (write path: tap a frequency on the tablet)          #
    # ------------------------------------------------------------------ #
    def set_com_standby(self, mhz: float, com: int = 1) -> bool:
        """Sets the COM1/COM2 STANDBY frequency in the sim (tap on the
        tablet, then swap from the cockpit when ready — the safe workflow).

        Python-SimConnect's built-in event catalog predates the *_HZ
        events (its find() returns None), so the event is created directly
        by name via the Event class. Falls back to the legacy BCD16 event
        for older sims (25 kHz spacing only).
        """
        if self.sm is None:
            return False
        from SimConnect import Event
        # Modern Hz event: exact value, native 8.33 kHz support
        name = b"COM_STBY_RADIO_SET_HZ" if com == 1 else b"COM2_STBY_RADIO_SET_HZ"
        try:
            Event(name, self.sm)(int(round(mhz * 1_000_000)))
            return True
        except Exception as e:
            print(f"[SimConnect] Hz radio event failed ({e}) — trying BCD fallback")
        # Legacy BCD16 event: encodes the 4 digits after the leading "1"
        # (121.950 → 0x2195)
        try:
            name = b"COM_STBY_RADIO_SET" if com == 1 else b"COM2_STBY_RADIO_SET"
            digits = f"{round(mhz * 1000):06d}"      # 121.950 → "121950"
            bcd = int(digits[1:5], 16)               # "2195" → 0x2195
            Event(name, self.sm)(bcd)
            return True
        except Exception as e:
            print(f"[SimConnect] Radio set failed: {e}")
            return False

    def _compute_vs(self, alt_ft: float) -> float:
        """V/S in ft/min derived from altitude, with exponential smoothing
        to absorb sampling noise (1–5 Hz)."""
        now = time.time()
        if self._prev_alt is not None:
            dt = now - self._prev_alt[0]
            if 0.05 < dt < 5.0:
                raw = (alt_ft - self._prev_alt[1]) / dt * 60.0
                self._vs_raw_fpm = raw
                # dt-normalized EMA (τ ≈ 0.8 s): same V/S responsiveness
                # whether the server runs at 1 Hz or 5 Hz.
                alpha = 1.0 - math.exp(-dt / 0.8)
                self._vs_fpm += alpha * (raw - self._vs_fpm)
        self._prev_alt = (now, alt_ft)
        return round(self._vs_fpm, 1)


def _f(v) -> float:
    """Converts a SimConnect value to a safe float (None → 0.0)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _track_deg(v) -> float | None:
    """Ground track in degrees, or None when the SimVar is unavailable
    (a missing value must NOT silently become a 0° track)."""
    if v is None:
        return None
    try:
        return math.degrees(float(v)) % 360
    except (TypeError, ValueError):
        return None
