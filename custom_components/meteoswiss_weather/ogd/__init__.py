"""Pure-Python client for the MeteoSwiss open data (ADR-0001).

Imports nothing from Home Assistant so it can move to PyPI unchanged once
the interface has settled: an ``aiohttp.ClientSession`` is passed in. This
module re-exports the public surface; submodules hold the implementation.
"""

from __future__ import annotations

from .http import CachedResponse, get_text
from .models import (
    Observation,
    OgdConnectionError,
    OgdError,
    OgdParseError,
    Station,
)
from .stations import fetch_current, fetch_stations, nearest_stations

__all__ = [
    "CachedResponse",
    "Observation",
    "OgdConnectionError",
    "OgdError",
    "OgdParseError",
    "Station",
    "fetch_current",
    "fetch_stations",
    "get_text",
    "nearest_stations",
]
