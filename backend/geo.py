"""
Shared geodesy helpers — single source of truth for the great-circle
distance used by the plan progress, the logbook, the airport lookups
and the online-traffic radius filter.
"""

from __future__ import annotations

import math

EARTH_RADIUS_NM = 3440.065


def haversine_nm(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in nautical miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(math.sqrt(a))
