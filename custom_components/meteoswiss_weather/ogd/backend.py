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
    DAILY_BLOCK_PARAMS,
    DAILY_REQUIRED_PARAMS,
    DAILY_WIND_PARAMS,
    FORECAST_ENCODING,
    HOURLY_HORIZON_FULL_RUN,
    HOURLY_PRECIP_PROBABILITY,
    HOURLY_REQUIRED_PARAMS,
    HOURLY_ZERO_DEGREE,
)
from .forecast import (
    aggregate_daily_precip_probability,
    aggregate_daily_wind,
    parse_daily,
    parse_hourly,
)
from .hourly import fetch_hourly_file, fetch_point_block, horizon_end_utc
from .http import get_text
from .models import (
    DailyBundle,
    ForecastPoint,
    HourlyForecast,
    OgdConnectionError,
)
from .stac import Run, latest_run

_LOGGER = logging.getLogger(__name__)


class ForecastBackend(Protocol):
    """A source of daily and hourly forecasts for a resolved point."""

    async def fetch_daily(self, point: ForecastPoint) -> DailyBundle: ...

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
    and parses them off the event loop (ADR-0002). The point-major blocks of
    :data:`DAILY_BLOCK_PARAMS` are also fetched with each daily refresh (~5 KB
    each via the #50 block strategy): the three wind files for the daily wind
    fields (issue #60, ADR-0002 revision 3), ``rp0003i0`` for the derived daily
    precipitation probability (issue #112, revision 5) and ``zprfr0hs`` for the
    hourly zero-degree levels (issue #107, revision 6). The blocks are cached by
    run stamp so the lazy hourly fetch reuses them without a second download.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        # Byte offset of the point's block in each point-major hourly file,
        # remembered across runs so the next fetch verifies it with one probe
        # instead of a fresh binary search (issue #50). Keyed by parameter code.
        # Shared between the daily block fetch and the lazy hourly fetch.
        self._block_starts: dict[str, int] = {}
        # Cached block texts from the most recent daily refresh, keyed by
        # parameter and remembered with the run stamp, so the daily and hourly
        # paths never download a block twice for the same run (issue #60). A
        # parameter that degraded (missing, not point-major, unreachable) is
        # simply absent from the dict.
        self._block_texts: dict[str, str] = {}
        self._block_run: datetime | None = None

    async def _get_block_texts(
        self, point: ForecastPoint, run: Run
    ) -> dict[str, str]:
        """Return the point-major block texts for ``run``, fetching only if needed.

        Every parameter in :data:`DAILY_BLOCK_PARAMS` is fetched independently
        and the result holds only the ones that came back; a file that is not
        point-major, not yet published for this run or unreachable is left out
        with a warning (ADR-0002 guardrail: the full 30 MB download is never
        triggered for a default feature, and one file's trouble never takes the
        others down with it). On success the texts are cached so the hourly
        path reuses them for the same run without a second download.
        """
        # _block_run set means we already tried this run; _block_texts is the
        # result (possibly empty when every guardrail fired).
        if self._block_run == run.timestamp:
            return self._block_texts

        # The daily run is selected on DAILY_REQUIRED_PARAMS alone, so it can be
        # complete for the small daily files while the ~30 MB hourly files of
        # the same run have not landed yet (they publish last). A missing asset
        # must degrade like the point-major guardrail, never crash the default
        # daily refresh with a KeyError from asset_url() (issue #60).
        present = [param for param in DAILY_BLOCK_PARAMS if param in run.assets]
        for param in DAILY_BLOCK_PARAMS:
            if param not in present:
                _LOGGER.warning(
                    "block %s skipped for run %s: file not published yet; its "
                    "fields will be None",
                    param,
                    run.timestamp.isoformat(),
                )

        # The blocks are a best-effort bonus on the default daily refresh: a
        # transient connection error while probing/fetching one must degrade
        # its fields to None, never fail the whole daily update and lose the
        # temperature, precipitation and symbol that fetched fine (ADR-0002
        # revision 3, the same "never crash the default daily refresh" contract
        # as the missing-asset and non-point-major guardrails).
        results = await asyncio.gather(
            *(
                fetch_point_block(
                    self._session,
                    run.asset_url(param),
                    point,
                    cached_start=self._block_starts.get(param),
                )
                for param in present
            ),
            return_exceptions=True,
        )

        texts: dict[str, str] = {}
        for param, result in zip(present, results, strict=True):
            if isinstance(result, OgdConnectionError):
                _LOGGER.warning(
                    "block %s skipped for run %s: %s; its fields will be None",
                    param,
                    run.timestamp.isoformat(),
                    result,
                )
                continue
            if isinstance(result, BaseException):
                raise result
            if result is None:
                _LOGGER.warning(
                    "block %s skipped for run %s: file is not point-major; its "
                    "fields will be None",
                    param,
                    run.timestamp.isoformat(),
                )
                continue
            texts[param] = result.text
            if result.block_start is not None:
                self._block_starts[param] = result.block_start

        self._block_texts = texts
        self._block_run = run.timestamp
        return texts

    async def fetch_daily(self, point: ForecastPoint) -> DailyBundle:
        run = await latest_run(
            self._session, COLLECTION_FORECAST, DAILY_REQUIRED_PARAMS
        )
        # Daily files are small; fetch them concurrently, one per parameter.
        # Fetch the point-major blocks concurrently with the daily files (each
        # ~5 KB via the block strategy — well inside the daily budget).
        bodies, block_texts = await asyncio.gather(
            asyncio.gather(
                *(
                    get_text(
                        self._session, run.asset_url(param), encoding=FORECAST_ENCODING
                    )
                    for param in DAILY_REQUIRED_PARAMS
                )
            ),
            self._get_block_texts(point, run),
        )
        text_by_param = {
            param: response.body
            for param, response in zip(DAILY_REQUIRED_PARAMS, bodies, strict=True)
        }
        # Parsing scans several MB per file; keep it off the event loop.
        loop = asyncio.get_running_loop()
        daily = await loop.run_in_executor(None, parse_daily, text_by_param, point)

        # Daily wind needs all three blocks: speed drives the aggregation and
        # gust/direction are looked up at its hour, so a partial set is as good
        # as none.
        wind_texts = {p: block_texts[p] for p in DAILY_WIND_PARAMS if p in block_texts}
        if len(wind_texts) == len(DAILY_WIND_PARAMS):
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

        # The daily probability is derived from the rp0003i0 block (issue #112):
        # the maximum 3-hour probability per local calendar day.
        if HOURLY_PRECIP_PROBABILITY in block_texts:
            prob_by_day = await loop.run_in_executor(
                None,
                aggregate_daily_precip_probability,
                block_texts[HOURLY_PRECIP_PROBABILITY],
                point,
            )
            daily = [
                replace(d, precipitation_probability=prob_by_day.get(d.date))
                for d in daily
            ]

        # The zero-degree block is the hourly parser's job (it is an hourly
        # file); keep only the hours that carry a value (issue #107).
        zero_degree: dict[datetime, float] = {}
        if HOURLY_ZERO_DEGREE in block_texts:
            hours = await loop.run_in_executor(
                None,
                parse_hourly,
                {HOURLY_ZERO_DEGREE: block_texts[HOURLY_ZERO_DEGREE]},
                point,
            )
            zero_degree = {
                h.time: h.zero_degree_level
                for h in hours
                if h.zero_degree_level is not None
            }

        return DailyBundle(daily=daily, zero_degree_level=zero_degree)

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
        # When fetch_daily() has already fetched the point-major wind,
        # probability and zero-degree blocks for this run, reuse their cached
        # texts without a second download (issues #60, #112, #107). When
        # the cache is absent (no prior daily call, or a different run), the
        # requested params are fetched the normal way — the same as before
        # issue #60.
        run = await latest_run(self._session, COLLECTION_FORECAST, params)
        now = datetime.now(UTC)
        horizon_end = horizon_end_utc(horizon_days, now)
        horizon_start = now.replace(minute=0, second=0, microsecond=0)

        # Direct cache check (no re-probe): only hit if daily already ran for
        # this very run. A block that degraded on the daily path is absent from
        # the cache and is fetched here like any other hourly file.
        block_cache = (
            self._block_texts if self._block_run == run.timestamp else {}
        )
        params_to_fetch = [p for p in params if p not in block_cache]

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

        # Only fold in cached block texts for params this call requested, so a
        # temperature-only (near/far) fetch stays temperature-only.
        text_by_param.update({p: t for p, t in block_cache.items() if p in params})

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
            None, parse_hourly, text_by_param, point, horizon_end, horizon_start
        )
