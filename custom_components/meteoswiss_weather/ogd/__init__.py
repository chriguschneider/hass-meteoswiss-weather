"""Pure-Python client for the MeteoSwiss open data (ADR-0001).

Imports nothing from Home Assistant so it can move to PyPI unchanged once
the interface has settled: an ``aiohttp.ClientSession`` is passed in. This
module re-exports the public surface; submodules hold the implementation.
"""

from __future__ import annotations

from .backend import BulkCsvBackend, ForecastBackend
from .forecast import (
    fetch_points,
    nearest_point,
    parse_daily,
    parse_hourly,
    points_for_postal_code,
)
from .http import CachedResponse, get_text
from .models import (
    DailyForecast,
    ForecastPoint,
    HourlyForecast,
    Observation,
    OgdConnectionError,
    OgdError,
    OgdParseError,
    Station,
)
from .stac import Run, latest_run
from .stations import (
    fetch_current,
    fetch_datainventory,
    fetch_stations,
    nearest_stations,
)

__all__ = [
    "BulkCsvBackend",
    "CachedResponse",
    "DailyForecast",
    "ForecastBackend",
    "ForecastPoint",
    "HourlyForecast",
    "Observation",
    "OgdConnectionError",
    "OgdError",
    "OgdParseError",
    "Run",
    "Station",
    "fetch_current",
    "fetch_datainventory",
    "fetch_points",
    "fetch_stations",
    "get_text",
    "latest_run",
    "nearest_point",
    "nearest_stations",
    "parse_daily",
    "parse_hourly",
    "points_for_postal_code",
]
