"""Geographic helpers shared by the station and forecast clients.

Kept HA-free (ADR-0001): plain stdlib maths so the ``ogd`` package can move
to PyPI unchanged.
"""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

# Mean Earth radius (km). Haversine is accurate enough to rank nearby points.
_EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres between two WGS84 points."""
    p1, p2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * asin(sqrt(a))
