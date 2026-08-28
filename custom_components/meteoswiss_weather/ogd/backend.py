"""Forecast backend seam (ADR-0002).

The daily forecast is assembled from the bulk CSV files today; MeteoSwiss has
announced a per-point OGC Features API for the end of 2026. Both live behind
:class:`ForecastBackend`, so swapping to the point API when it ships is a
contained change that never reaches the coordinator or the entities.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Protocol

import aiohttp

from .const import (
    COLLECTION_FORECAST,
    DAILY_REQUIRED_PARAMS,
    FORECAST_ENCODING,
    HOURLY_HORIZON_FULL_RUN,
    HOURLY_REQUIRED_PARAMS,
)
from .forecast import parse_daily, parse_hourly
from .hourly import fetch_hourly_file, horizon_end_utc
from .http import get_text
from .models import DailyForecast, ForecastPoint, HourlyForecast
from .stac import latest_run

_LOGGER = logging.getLogger(__name__)


class ForecastBackend(Protocol):
    """A source of daily and hourly forecasts for a resolved point."""

    async def fetch_daily(self, point: ForecastPoint) -> list[DailyForecast]: ...

    async def fetch_hourly(
        self, point: ForecastPoint, *, horizon_days: int = HOURLY_HORIZON_FULL_RUN
    ) -> list[HourlyForecast]: ...


class BulkCsvBackend:
    """Assembles the forecast from the bulk per-parameter CSV files.

    Discovers the newest complete run (STAC), downloads its small daily files
    and parses them off the event loop (ADR-0002). Holds no cross-poll state:
    the coordinator compares the run timestamp before asking for a refresh, so
    an unchanged run never reaches this backend.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        # Byte offset of the point's block in each point-major hourly file,
        # remembered across runs so the next fetch verifies it with one probe
        # instead of a fresh binary search (issue #50). Keyed by parameter code.
        self._block_starts: dict[str, int] = {}

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

    async def fetch_hourly(
        self, point: ForecastPoint, *, horizon_days: int = HOURLY_HORIZON_FULL_RUN
    ) -> list[HourlyForecast]:
        # The bulk hourly files are the whole traffic budget (~30 MB each), so
        # this path only runs behind the opt-in option and the 3 h throttle the
        # coordinator enforces (ADR-0002). Each file is fetched with the cheapest
        # Range strategy for its layout (issue #50): a horizon prefix for the
        # date-major files, the point's contiguous block for the point-major
        # ones, and the full file only as a fallback.
        run = await latest_run(
            self._session, COLLECTION_FORECAST, HOURLY_REQUIRED_PARAMS
        )
        horizon_end = horizon_end_utc(horizon_days, datetime.now(UTC))

        results = await asyncio.gather(
            *(
                fetch_hourly_file(
                    self._session,
                    run.asset_url(param),
                    point,
                    horizon_end=horizon_end,
                    cached_start=self._block_starts.get(param),
                )
                for param in HOURLY_REQUIRED_PARAMS
            )
        )
        text_by_param: dict[str, str] = {}
        for param, result in zip(HOURLY_REQUIRED_PARAMS, results, strict=True):
            text_by_param[param] = result.text
            if result.block_start is not None:
                self._block_starts[param] = result.block_start

        # The download is the cost this option pays for; record it so a user can
        # see what enabling the hourly forecast actually spends (ADR-0002).
        total_bytes = sum(len(text.encode(FORECAST_ENCODING)) for text in
                          text_by_param.values())
        _LOGGER.debug(
            "hourly forecast run %s (horizon_days=%s): fetched %d bytes across "
            "%d files (%s)",
            run.timestamp.isoformat(),
            horizon_days,
            total_bytes,
            len(text_by_param),
            ", ".join(
                f"{param}={results[i].layout.value}:"
                f"{len(text_by_param[param].encode(FORECAST_ENCODING))}"
                for i, param in enumerate(HOURLY_REQUIRED_PARAMS)
            ),
        )
        # Parsing keeps only the point's rows; keep it off the event loop.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, parse_hourly, text_by_param, point, horizon_end
        )
