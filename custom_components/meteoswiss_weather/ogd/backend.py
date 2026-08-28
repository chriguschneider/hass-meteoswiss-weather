"""Forecast backend seam (ADR-0002).

The daily forecast is assembled from the bulk CSV files today; MeteoSwiss has
announced a per-point OGC Features API for the end of 2026. Both live behind
:class:`ForecastBackend`, so swapping to the point API when it ships is a
contained change that never reaches the coordinator or the entities.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol

import aiohttp

from .const import (
    COLLECTION_FORECAST,
    DAILY_REQUIRED_PARAMS,
    DAILY_WIND_PARAMS,
    FORECAST_ENCODING,
    HOURLY_HORIZON_FULL_RUN,
    HOURLY_REQUIRED_PARAMS,
)
from .forecast import aggregate_daily_wind, parse_daily, parse_hourly
from .hourly import fetch_hourly_file, fetch_wind_block, horizon_end_utc
from .http import get_text
from .models import (
    DailyForecast,
    ForecastPoint,
    HourlyForecast,
    OgdConnectionError,
)
from .stac import Run, latest_run

_LOGGER = logging.getLogger(__name__)


class ForecastBackend(Protocol):
    """A source of daily and hourly forecasts for a resolved point."""

    async def fetch_daily(self, point: ForecastPoint) -> list[DailyForecast]: ...

    async def fetch_hourly(
        self,
        point: ForecastPoint,
        *,
        horizon_days: int = HOURLY_HORIZON_FULL_RUN,
        params: tuple[str, ...] = HOURLY_REQUIRED_PARAMS,
    ) -> list[HourlyForecast]: ...


class BulkCsvBackend:
    """Assembles the forecast from the bulk per-parameter CSV files.

    Discovers the newest complete run (STAC), downloads its small daily files
    and parses them off the event loop (ADR-0002). The three point-major wind
    files are also fetched with each daily refresh (~5 KB each via the #50
    block strategy) to populate daily wind fields; their blocks are cached by
    run stamp so the lazy hourly fetch reuses them without a second download
    (issue #60, ADR-0002 revision 3).
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        # Byte offset of the point's block in each point-major hourly file,
        # remembered across runs so the next fetch verifies it with one probe
        # instead of a fresh binary search (issue #50). Keyed by parameter code.
        # Shared between the daily wind fetch and the lazy hourly fetch.
        self._block_starts: dict[str, int] = {}
        # Cached wind block texts from the most recent successful fetch, keyed
        # by run stamp so the daily and hourly paths never download them twice
        # for the same run (issue #60).
        self._wind_texts: dict[str, str] | None = None
        self._wind_run: datetime | None = None

    async def _get_wind_texts(
        self, point: ForecastPoint, run: Run
    ) -> dict[str, str] | None:
        """Return the wind block texts for ``run``, fetching only if needed.

        Returns ``None`` when any wind file is not point-major (ADR-0002
        guardrail: the full 30 MB download is never triggered for a default
        feature). On success the texts are cached so the hourly path reuses
        them for the same run without a second download.
        """
        # _wind_run set means we already tried this run; _wind_texts is the result
        # (populated dict on success, empty dict when the guardrail fired).
        if self._wind_run == run.timestamp:
            return self._wind_texts or None

        # The daily run is selected on DAILY_REQUIRED_PARAMS alone, so it can be
        # complete for the small daily files while the ~30 MB wind files of the
        # same run have not landed yet (they publish last). A missing wind asset
        # must degrade to None like the point-major guardrail, never crash the
        # default daily refresh with a KeyError from asset_url() (issue #60).
        if any(param not in run.assets for param in DAILY_WIND_PARAMS):
            _LOGGER.warning(
                "daily wind skipped for run %s: one or more wind files are not "
                "published yet; wind fields will be None for all days",
                run.timestamp.isoformat(),
            )
            self._wind_texts = {}
            self._wind_run = run.timestamp
            return None

        # Wind is a best-effort bonus on the default daily refresh: a transient
        # connection error while probing/fetching a block must degrade wind to
        # None, never fail the whole daily update and lose the temperature,
        # precipitation and symbol that fetched fine (ADR-0002 revision 3, the
        # same "never crash the default daily refresh" contract as the missing-
        # asset and non-point-major guardrails above).
        try:
            results = await asyncio.gather(
                *(
                    fetch_wind_block(
                        self._session,
                        run.asset_url(param),
                        point,
                        cached_start=self._block_starts.get(param),
                    )
                    for param in DAILY_WIND_PARAMS
                )
            )
        except OgdConnectionError as err:
            _LOGGER.warning(
                "daily wind skipped for run %s: %s; wind fields will be None "
                "for all days",
                run.timestamp.isoformat(),
                err,
            )
            self._wind_texts = {}
            self._wind_run = run.timestamp
            return None

        if any(r is None for r in results):
            _LOGGER.warning(
                "daily wind skipped for run %s: one or more wind files are not "
                "point-major; wind fields will be None for all days",
                run.timestamp.isoformat(),
            )
            # Record the sentinel (empty dict) so repeated calls for this run
            # return None immediately without re-probing the files.
            self._wind_texts = {}
            self._wind_run = run.timestamp
            return None

        texts: dict[str, str] = {}
        for param, result in zip(DAILY_WIND_PARAMS, results, strict=True):
            texts[param] = result.text  # type: ignore[union-attr]
            if result.block_start is not None:  # type: ignore[union-attr]
                self._block_starts[param] = result.block_start  # type: ignore[union-attr]

        self._wind_texts = texts
        self._wind_run = run.timestamp
        return texts

    async def fetch_daily(self, point: ForecastPoint) -> list[DailyForecast]:
        run = await latest_run(
            self._session, COLLECTION_FORECAST, DAILY_REQUIRED_PARAMS
        )
        # Daily files are small; fetch them concurrently, one per parameter.
        # Fetch wind blocks concurrently with the daily files (each ~5 KB via
        # the point-major block strategy — well inside the daily budget).
        bodies, wind_texts = await asyncio.gather(
            asyncio.gather(
                *(
                    get_text(
                        self._session, run.asset_url(param), encoding=FORECAST_ENCODING
                    )
                    for param in DAILY_REQUIRED_PARAMS
                )
            ),
            self._get_wind_texts(point, run),
        )
        text_by_param = {
            param: response.body
            for param, response in zip(DAILY_REQUIRED_PARAMS, bodies, strict=True)
        }
        # Parsing scans several MB per file; keep it off the event loop.
        loop = asyncio.get_running_loop()
        daily = await loop.run_in_executor(None, parse_daily, text_by_param, point)

        if wind_texts:
            wind_by_day = await loop.run_in_executor(
                None, aggregate_daily_wind, wind_texts, point
            )
            daily = [
                replace(
                    d,
                    native_wind_speed=wind[0],
                    native_wind_gust_speed=wind[1],
                    wind_bearing=wind[2],
                )
                for d in daily
                for wind in [wind_by_day.get(d.date, (None, None, None))]
            ]

        return daily

    async def fetch_hourly(
        self,
        point: ForecastPoint,
        *,
        horizon_days: int = HOURLY_HORIZON_FULL_RUN,
        params: tuple[str, ...] = HOURLY_REQUIRED_PARAMS,
    ) -> list[HourlyForecast]:
        # The bulk hourly files are the whole traffic budget (~30 MB each), so
        # this path only runs behind the opt-in option and the tiered schedule
        # the provider enforces (ADR-0002 revision 2). Each file is fetched with
        # the cheapest Range strategy for its layout (issue #50): a horizon prefix
        # for the date-major files, the point's contiguous block for the
        # point-major ones, and the full file only as a fallback.
        #
        # ``params`` is the subset to fetch — the tiered provider (issue #68) asks
        # for the date-major temperature file (near/far horizon) and the
        # point-major group on independent schedules, so this fetches only what a
        # given tier needs rather than the whole set every time.
        #
        # When fetch_daily() has already fetched the three point-major wind blocks
        # for this run, reuse their cached texts without a second download
        # (issue #60, ADR-0002 revision 3). When the cache is absent (no prior
        # daily call, or a different run), the requested params are fetched the
        # normal way — the same as before issue #60.
        run = await latest_run(self._session, COLLECTION_FORECAST, params)
        horizon_end = horizon_end_utc(horizon_days, datetime.now(UTC))

        # Direct cache check (no re-probe): only hit if daily already ran.
        wind_cache = (
            self._wind_texts
            if (self._wind_run == run.timestamp and self._wind_texts)
            else None
        )
        params_to_fetch = (
            [p for p in params if p not in DAILY_WIND_PARAMS]
            if wind_cache is not None
            else list(params)
        )

        results = await asyncio.gather(
            *(
                fetch_hourly_file(
                    self._session,
                    run.asset_url(param),
                    point,
                    horizon_end=horizon_end,
                    cached_start=self._block_starts.get(param),
                )
                for param in params_to_fetch
            )
        )
        text_by_param: dict[str, str] = {}
        for param, result in zip(params_to_fetch, results, strict=True):
            text_by_param[param] = result.text
            if result.block_start is not None:
                self._block_starts[param] = result.block_start

        if wind_cache is not None:
            # Only fold in cached wind texts for wind params this call requested,
            # so a temperature-only (near/far) fetch stays temperature-only.
            text_by_param.update(
                {p: t for p, t in wind_cache.items() if p in params}
            )

        # The download is the cost this option pays for; record it so a user can
        # see what enabling the hourly forecast actually spends (ADR-0002).
        total_bytes = sum(len(text.encode(FORECAST_ENCODING)) for text in
                          text_by_param.values())
        _LOGGER.debug(
            "hourly forecast run %s (horizon_days=%s): fetched %d bytes across "
            "%d files",
            run.timestamp.isoformat(),
            horizon_days,
            total_bytes,
            len(text_by_param),
        )
        # Parsing keeps only the point's rows; keep it off the event loop.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, parse_hourly, text_by_param, point, horizon_end
        )
