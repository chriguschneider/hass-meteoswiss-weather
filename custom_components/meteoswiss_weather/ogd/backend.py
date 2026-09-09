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
    HOURLY_ZERO_DEGREE,
)
from .forecast import (
    aggregate_daily_wind,
    parse_daily,
    parse_hourly,
    zero_degree_by_hour,
)
from .hourly import fetch_hourly_file, fetch_point_block, horizon_end_utc
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

    def latest_zero_degree(self) -> dict[datetime, float | None]:
        """Zero-degree level (m) per UTC hour from the last :meth:`fetch_daily`.

        Populated alongside the daily refresh so the zero-degree sensor has a
        value without the hourly opt-in (issue #107). Empty when the source
        file was missing or not point-major (the ADR-0002 guardrail).
        """

    async def fetch_zero_degree(
        self, point: ForecastPoint, run: Run
    ) -> dict[datetime, float | None]:
        """Fetch just the zero-degree series for ``run``, without the daily files.

        The retry path for a run whose daily files arrived before its zprfr0hs
        did (issue #107): one ~5 KB point block, no re-download of the daily
        files. Returns the same shape as :meth:`latest_zero_degree`, empty when
        the guardrail fired.
        """


class BulkCsvBackend:
    """Assembles the forecast from the bulk per-parameter CSV files.

    Discovers the newest complete run (STAC), downloads its small daily files
    and parses them off the event loop (ADR-0002). Two point-major groups are
    also fetched with each daily refresh (~5 KB each via the #50 block strategy):
    the three wind files that populate the daily wind fields (issue #60), and
    the zero-degree level that backs the zero-degree sensor without the hourly
    opt-in (issue #107). Their blocks are cached by run stamp so the lazy hourly
    fetch reuses them without a second download (ADR-0002 revision 3/5).
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        # Byte offset of the point's block in each point-major hourly file,
        # remembered across runs so the next fetch verifies it with one probe
        # instead of a fresh binary search (issue #50). Keyed by parameter code.
        # Shared between the daily block fetch and the lazy hourly fetch.
        self._block_starts: dict[str, int] = {}
        # Cached wind block texts from the most recent successful fetch, keyed
        # by run stamp so the daily and hourly paths never download them twice
        # for the same run (issue #60).
        self._wind_texts: dict[str, str] | None = None
        self._wind_run: datetime | None = None
        # Same pattern for the zero-degree block (issue #107): the fetched text
        # and the parsed per-hour series, keyed by run so the hourly path reuses
        # the text and the coordinator reads the series via latest_zero_degree().
        self._zero_text: str | None = None
        self._zero_by_hour: dict[datetime, float | None] = {}
        self._zero_run: datetime | None = None

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
                    fetch_point_block(
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

    async def _get_zero_text(
        self, point: ForecastPoint, run: Run
    ) -> str | None:
        """Return the ``zprfr0hs`` block text for ``run``, fetching only if needed.

        The zero-degree level is a point-major file (docs/ogd.md §E4), so its
        block is ~5 KB — cheap enough to fetch with every default daily refresh
        (issue #107). It degrades exactly like the wind blocks: ``None`` when the
        file is absent from the run or not point-major, so the full 30 MB
        download is never triggered for a default feature (ADR-0002 guardrail).
        The text is cached by run so the lazy hourly path reuses it.

        Only the two *stable* outcomes are memoised by run stamp: a successful
        fetch, and a file that turned out not to be point-major (a property of
        the file, which will not change within a run). A missing asset and a
        connection error are deliberately not memoised — the ~30 MB zprfr0hs
        often lands a few minutes after the small daily files it shares a run
        with, so a later call in the same run must be free to try again
        (issue #107). The caller controls how often that happens.
        """
        # _zero_run set means we already have a settled answer for this run;
        # _zero_text is the result (populated str on success, None when the
        # point-major guardrail fired).
        if self._zero_run == run.timestamp:
            return self._zero_text

        # Like the wind blocks: the run is selected on DAILY_REQUIRED_PARAMS, so
        # the ~30 MB zprfr0hs file of the same run may not have landed yet. A
        # missing asset degrades to None, never a KeyError from asset_url().
        if HOURLY_ZERO_DEGREE not in run.assets:
            _LOGGER.warning(
                "daily zero-degree skipped for run %s: the file is not published "
                "yet; retrying on the next forecast check",
                run.timestamp.isoformat(),
            )
            # Not memoised: the file may still land within this run.
            self._zero_text = None
            return None

        # A transient connection error must degrade zero-degree to None, never
        # fail the default daily refresh (same contract as the wind guardrails).
        try:
            result = await fetch_point_block(
                self._session,
                run.asset_url(HOURLY_ZERO_DEGREE),
                point,
                cached_start=self._block_starts.get(HOURLY_ZERO_DEGREE),
            )
        except OgdConnectionError as err:
            _LOGGER.warning(
                "daily zero-degree skipped for run %s: %s; retrying on the next "
                "forecast check",
                run.timestamp.isoformat(),
                err,
            )
            # Not memoised: a transient error says nothing about the next try.
            self._zero_text = None
            return None

        if result is None:
            _LOGGER.warning(
                "daily zero-degree skipped for run %s: the file is not "
                "point-major; the zero-degree sensor stays unknown this run",
                run.timestamp.isoformat(),
            )
            self._zero_text = None
            self._zero_run = run.timestamp
            return None

        if result.block_start is not None:
            self._block_starts[HOURLY_ZERO_DEGREE] = result.block_start
        self._zero_text = result.text
        self._zero_run = run.timestamp
        return result.text

    def latest_zero_degree(self) -> dict[datetime, float | None]:
        """Zero-degree level (m) per UTC hour from the last :meth:`fetch_daily`."""
        return self._zero_by_hour

    async def _parse_zero_text(
        self, text: str | None, point: ForecastPoint
    ) -> dict[datetime, float | None]:
        """Parse a zero-degree block into the per-hour series, off the event loop."""
        if text is None:
            return {}
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, zero_degree_by_hour, text, point)

    async def fetch_zero_degree(
        self, point: ForecastPoint, run: Run
    ) -> dict[datetime, float | None]:
        """Fetch and parse the zero-degree block for ``run`` on its own."""
        text = await self._get_zero_text(point, run)
        self._zero_by_hour = await self._parse_zero_text(text, point)
        return self._zero_by_hour

    async def fetch_daily(self, point: ForecastPoint) -> list[DailyForecast]:
        run = await latest_run(
            self._session, COLLECTION_FORECAST, DAILY_REQUIRED_PARAMS
        )
        # Daily files are small; fetch them concurrently, one per parameter.
        # Fetch the wind and zero-degree blocks concurrently with them (each
        # ~5 KB via the point-major block strategy — well inside the daily
        # budget). Zero-degree backs a sensor that no longer needs the hourly
        # opt-in (issue #107).
        bodies, wind_texts, zero_text = await asyncio.gather(
            asyncio.gather(
                *(
                    get_text(
                        self._session, run.asset_url(param), encoding=FORECAST_ENCODING
                    )
                    for param in DAILY_REQUIRED_PARAMS
                )
            ),
            self._get_wind_texts(point, run),
            self._get_zero_text(point, run),
        )
        text_by_param = {
            param: response.body
            for param, response in zip(DAILY_REQUIRED_PARAMS, bodies, strict=True)
        }
        # Parsing scans several MB per file; keep it off the event loop.
        loop = asyncio.get_running_loop()
        daily = await loop.run_in_executor(None, parse_daily, text_by_param, point)

        # Parse the zero-degree block into the per-hour series the sensor reads;
        # keep the (cheap) scan off the event loop like the other parses. Empty
        # when the guardrail fired above.
        self._zero_by_hour = await self._parse_zero_text(zero_text, point)

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
        # When fetch_daily() has already fetched a point-major block for this run
        # — the three wind files (issue #60) or the zero-degree file (issue #107)
        # — reuse its cached text without a second download (ADR-0002 revision
        # 3/5). When no cache exists (no prior daily call, or a different run),
        # the requested params are fetched the normal way.
        run = await latest_run(self._session, COLLECTION_FORECAST, params)
        now = datetime.now(UTC)
        horizon_end = horizon_end_utc(horizon_days, now)
        horizon_start = now.replace(minute=0, second=0, microsecond=0)

        # Direct cache checks (no re-probe): only hit if daily already ran for
        # this same run. Collected into one map keyed by parameter code.
        reuse_texts: dict[str, str] = {}
        if self._wind_run == run.timestamp and self._wind_texts:
            reuse_texts.update(self._wind_texts)
        if self._zero_run == run.timestamp and self._zero_text is not None:
            reuse_texts[HOURLY_ZERO_DEGREE] = self._zero_text

        params_to_fetch = [p for p in params if p not in reuse_texts]

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
                # A block_start means the point-major strategy ran, so this text
                # is exactly what _get_zero_text would have fetched (both go
                # through _fetch_point_major, neither trims by horizon). Feeding
                # the cache here makes the reuse two-directional: whichever path
                # runs first for a run, the other one skips the download
                # (ADR-0002 revision 5). Without it the "never twice per run"
                # guarantee only held when the daily refresh happened to go
                # first.
                if param == HOURLY_ZERO_DEGREE:
                    self._zero_text = result.text
                    self._zero_run = run.timestamp

        # Only fold in cached texts for params this call requested, so a
        # temperature-only (near/far) fetch stays temperature-only.
        text_by_param.update(
            {p: t for p, t in reuse_texts.items() if p in params}
        )

        # The download is the cost this option pays for; record it so a user can
        # see what enabling the hourly forecast actually spends (ADR-0002).
        total_bytes = sum(len(text.encode(FORECAST_ENCODING)) for text in
                          text_by_param.values())
        # Reused blocks are counted in too, so this is the size of the assembled
        # data, not of the traffic — the reuse is what keeps the two apart.
        _LOGGER.debug(
            "hourly forecast run %s (horizon_days=%s): %d bytes across "
            "%d files (%d fetched, the rest reused)",
            run.timestamp.isoformat(),
            horizon_days,
            total_bytes,
            len(text_by_param),
            len(params_to_fetch),
        )
        # Parsing keeps only the point's rows; keep it off the event loop.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, parse_hourly, text_by_param, point, horizon_end, horizon_start
        )
