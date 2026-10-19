"""Pure-Python client for the SLF IMIS snow-station data (ADR-0009).

Imports nothing from Home Assistant so it can move to PyPI unchanged:
an ``aiohttp.ClientSession`` is passed in. This module re-exports the
public surface; submodules hold the implementation.
"""

from __future__ import annotations

from .client import (
    fetch_imis_daily_snow,
    fetch_imis_latest,
    fetch_imis_stations,
    nearest_snow_stations,
)
from .models import (
    ImisDailySnow,
    ImisMeasurement,
    ImisStation,
    SlfConnectionError,
    SlfError,
    SlfParseError,
)

__all__ = [
    "ImisDailySnow",
    "ImisMeasurement",
    "ImisStation",
    "SlfConnectionError",
    "SlfError",
    "SlfParseError",
    "fetch_imis_daily_snow",
    "fetch_imis_latest",
    "fetch_imis_stations",
    "nearest_snow_stations",
]
