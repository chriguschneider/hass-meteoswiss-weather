"""Forecast backend seam (ADR-0002).

The daily forecast is assembled from the bulk CSV files today; MeteoSwiss has
announced a per-point OGC Features API for the end of 2026. Both live behind
:class:`ForecastBackend`, so swapping to the point API when it ships is a
contained change that never reaches the coordinator or the entities.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

import aiohttp

from .const import (
    COLLECTION_FORECAST,
    DAILY_REQUIRED_PARAMS,
    FORECAST_ENCODING,
)
from .forecast import parse_daily
from .http import get_text
from .models import DailyForecast, ForecastPoint, HourlyForecast
from .stac import latest_run


class ForecastBackend(Protocol):
    """A source of daily and hourly forecasts for a resolved point."""

    async def fetch_daily(self, point: ForecastPoint) -> list[DailyForecast]: ...

    async def fetch_hourly(self, point: ForecastPoint) -> list[HourlyForecast]: ...


class BulkCsvBackend:
    """Assembles the forecast from the bulk per-parameter CSV files.

    Discovers the newest complete run (STAC), downloads its small daily files
    and parses them off the event loop (ADR-0002). Holds no cross-poll state:
    the coordinator compares the run timestamp before asking for a refresh, so
    an unchanged run never reaches this backend.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def fetch_daily(self, point: ForecastPoint) -> list[DailyForecast]:
        run = await latest_run(
            self._session, COLLECTION_FORECAST, DAILY_REQUIRED_PARAMS
        )
        # Daily files are small; fetch them concurrently, one per parameter.
        bodies = await asyncio.gather(
            *(
                get_text(
                    self._session, run.asset_url(param), encoding=FORECAST_ENCODING
                )
                for param in DAILY_REQUIRED_PARAMS
            )
        )
        text_by_param = {
            param: response.body
            for param, response in zip(DAILY_REQUIRED_PARAMS, bodies, strict=True)
        }
        # Parsing scans several MB per file; keep it off the event loop.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, parse_daily, text_by_param, point)

    async def fetch_hourly(self, point: ForecastPoint) -> list[HourlyForecast]:
        # The hourly backend is issue #10; the bulk hourly files are the whole
        # traffic budget and gated behind an opt-in option (ADR-0002).
        raise NotImplementedError("hourly forecast is not implemented yet (#10)")
