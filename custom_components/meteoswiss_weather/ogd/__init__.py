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
from .history import fetch_station_history, select_history_files
from .hourly import (
    AiohttpRangeReader,
    classify_layout,
    fetch_hourly_file,
    horizon_end_utc,
)
from .http import CachedResponse, get_text
from .models import (
    DailyForecast,
    FileLayout,
    ForecastPoint,
    HourlyForecast,
    HourlyHistoryRow,
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
    "AiohttpRangeReader",
    "BulkCsvBackend",
    "CachedResponse",
    "DailyForecast",
    "FileLayout",
    "ForecastBackend",
    "ForecastPoint",
    "HourlyForecast",
    "HourlyHistoryRow",
    "Observation",
    "OgdConnectionError",
    "OgdError",
    "OgdParseError",
    "Run",
    "Station",
    "classify_layout",
    "fetch_current",
    "fetch_datainventory",
    "fetch_hourly_file",
    "fetch_points",
    "fetch_station_history",
    "fetch_stations",
    "get_text",
    "horizon_end_utc",
    "latest_run",
    "nearest_point",
    "nearest_stations",
    "parse_daily",
    "parse_hourly",
    "points_for_postal_code",
    "select_history_files",
]
