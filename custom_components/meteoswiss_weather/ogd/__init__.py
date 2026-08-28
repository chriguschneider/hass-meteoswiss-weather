"""Pure-Python client for the MeteoSwiss open data (ADR-0001).

Imports nothing from Home Assistant so it can move to PyPI unchanged once
the interface has settled: an ``aiohttp.ClientSession`` is passed in. This
module re-exports the public surface; submodules hold the implementation.
"""

from __future__ import annotations

from .backend import BulkCsvBackend, ForecastBackend
from .const import POINT_TYPE_MOUNTAIN
from .forecast import (
    aggregate_daily_wind,
    fetch_points,
    mountain_points,
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
    fetch_wind_block,
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
    PollenObservation,
    PollenStation,
    Station,
)
from .pollen import (
    fetch_pollen_current,
    fetch_pollen_datainventory,
    fetch_pollen_parameters,
    fetch_pollen_stations,
    nearest_pollen_station,
    nearest_pollen_stations,
)
from .stac import Run, latest_run
from .stations import (
    fetch_current,
    fetch_datainventory,
    fetch_precip_current,
    fetch_precip_datainventory,
    fetch_precip_stations,
    fetch_stations,
    nearest_stations,
)

__all__ = [
    "AiohttpRangeReader",
    "BulkCsvBackend",
    "POINT_TYPE_MOUNTAIN",
    "CachedResponse",
    "DailyForecast",
    "aggregate_daily_wind",
    "FileLayout",
    "ForecastBackend",
    "ForecastPoint",
    "HourlyForecast",
    "HourlyHistoryRow",
    "Observation",
    "OgdConnectionError",
    "OgdError",
    "OgdParseError",
    "PollenObservation",
    "PollenStation",
    "Run",
    "Station",
    "classify_layout",
    "fetch_current",
    "fetch_datainventory",
    "fetch_hourly_file",
    "fetch_wind_block",
    "fetch_points",
    "mountain_points",
    "fetch_pollen_current",
    "fetch_pollen_datainventory",
    "fetch_pollen_parameters",
    "fetch_pollen_stations",
    "fetch_precip_current",
    "fetch_precip_datainventory",
    "fetch_precip_stations",
    "fetch_station_history",
    "fetch_stations",
    "get_text",
    "horizon_end_utc",
    "latest_run",
    "nearest_point",
    "nearest_pollen_station",
    "nearest_pollen_stations",
    "nearest_stations",
    "parse_daily",
    "parse_hourly",
    "points_for_postal_code",
    "select_history_files",
]
