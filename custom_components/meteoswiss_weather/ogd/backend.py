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
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

import aiohttp

from .const import (
    COLLECTION_FORECAST,
    DAILY_BLOCK_PARAMS,
    DAILY_REQUIRED_PARAMS,
    DAILY_WIND_PARAMS,
    DAILY_ZERO_DEGREE_WINDOW_HOURS,
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
from .hourly import FileHint, fetch_hourly_file, fetch_series, horizon_end_utc
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
    """A source of daily and hourly forecasts for a resolved point.

    ``run`` is the run the caller already discovered this tick (ADR-0008
    section 3): a backend that needs a run uses it instead of discovering one
    again, and falls back to its own discovery when it is ``None`` or does not
    carry the files it needs.
    """

    async def fetch_daily(
        self, point: ForecastPoint, *, run: Run | None = None
    ) -> DailyBundle: ...

    async def fetch_hourly(
        self,
        point: ForecastPoint,
        *,
        horizon_days: int = HOURLY_HORIZON_FULL_RUN,
        params: tuple[str, ...] = HOURLY_REQUIRED_PARAMS,
        run: Run | None = None,
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
        # One :class:`FileHint` per parameter (ADR-0008, issue #121): the file's
        # layout, the UTC day it was learned on and the byte positions of the
        # point's rows (point-major block start, date-major row geometry, header,
        # first/last stamp). Remembered across the runs of a UTC day so the next
        # fetch skips classification and the probes and verifies through the rows
        # it finds. Shared between the daily block fetch and the lazy hourly
        # fetch; a hint only ever saves requests, a stale one costs a fresh look.
        self._hints: dict[str, FileHint] = {}
        # Cached block texts from the most recent daily refresh, keyed by
        # parameter and remembered with the run stamp, so the daily and hourly
        # paths never download a block twice for the same run (issue #60). A
        # parameter that degraded (missing, not point-major, unreachable) is
        # simply absent from the dict.
        self._block_texts: dict[str, str] = {}
        self._block_run: datetime | None = None
        # The cached texts that hold the point's *whole* run. Only those may
        # stand in for an hourly fetch; a text cut to the daily path's window
        # (a row-addressed date-major file) would silently shorten the hourly
        # forecast (ADR-0008 section 4).
        self._block_whole: set[str] = set()
        # Files proven (by a full download) to carry no row for the point,
        # remembered for the UTC day so the proof is not repeated every run.
        self._absent: dict[str, date] = {}

    async def _get_block_texts(
        self, point: ForecastPoint, run: Run
    ) -> dict[str, str]:
        """Return the point's rows of every daily block file for ``run``.

        Every parameter in :data:`DAILY_BLOCK_PARAMS` is fetched independently
        through the escalation ladder (:func:`~.hourly.fetch_series`, ADR-0008
        section 4): the cheapest strategy the file's layout admits, climbing to
        the whole file when nothing cheaper proves complete. A file is left out
        only when upstream has nothing to give — it is not published for this
        run yet, it is unreachable, or it carries no row for the point — and
        one file's trouble never takes the others down with it. The texts are
        cached so the hourly path reuses the whole-run ones for the same run.
        """
        # _block_run set means we already tried this run; _block_texts is the
        # result (possibly empty when upstream had nothing).
        if self._block_run == run.timestamp:
            return self._block_texts

        now = datetime.now(UTC)
        today = now.date()
        # The daily run is selected on DAILY_REQUIRED_PARAMS alone, so it can be
        # complete for the small daily files while an hourly file of the same
        # run has not landed yet. A missing asset must degrade, never crash the
        # default daily refresh with a KeyError from asset_url() (issue #60).
        present: list[str] = []
        for param in DAILY_BLOCK_PARAMS:
            if param not in run.assets:
                _LOGGER.warning(
                    "block %s skipped for run %s: file not published yet",
                    param,
                    run.timestamp.isoformat(),
                )
            elif self._absent.get(param) == today:
                _LOGGER.debug("block %s skipped: no row for the point today", param)
            else:
                present.append(param)

        # The zero-degree sensor needs the coming hours, not the whole run, so
        # a date-major file is row-addressed for this window only. The wind and
        # probability files feed per-day aggregates and need the whole run.
        this_hour = now.replace(minute=0, second=0, microsecond=0)
        windows: dict[str, tuple[datetime, datetime]] = {
            HOURLY_ZERO_DEGREE: (
                this_hour,
                this_hour + timedelta(hours=DAILY_ZERO_DEGREE_WINDOW_HOURS),
            )
        }

        # A connection error on one file degrades that file only, never the
        # whole daily update (ADR-0002 revision 3); the store keeps the last
        # good series for it (ADR-0008 section 1).
        results = await asyncio.gather(
            *(
                fetch_series(
                    self._session,
                    run.asset_url(param),
                    point,
                    window_start=windows.get(param, (None, None))[0],
                    window_end=windows.get(param, (None, None))[1],
                    hint=self._hints.get(param),
                    utc_day=run.timestamp.date(),
                )
                for param in present
            ),
            return_exceptions=True,
        )

        texts: dict[str, str] = {}
        whole: set[str] = set()
        for param, result in zip(present, results, strict=True):
            if isinstance(result, OgdConnectionError):
                _LOGGER.warning(
                    "block %s skipped for run %s: %s",
                    param,
                    run.timestamp.isoformat(),
                    result,
                )
                continue
            if isinstance(result, BaseException):
                raise result
            # Escalating to a prefix or the whole file is correct but worth
            # seeing: it usually means upstream re-sorted the file.
            log = _LOGGER.warning if result.level >= 3 else _LOGGER.debug
            log(
                "block %s for run %s: layout %s, level %d, %d requests, %d bytes",
                param,
                run.timestamp.isoformat(),
                result.layout.value,
                result.level,
                result.requests,
                result.bytes,
            )
            if result.hint is not None:
                self._hints[param] = result.hint
            if not result.has_rows:
                if result.level == 4:
                    self._absent[param] = today
                continue
            texts[param] = result.text
            if result.whole_run:
                whole.add(param)

        self._block_texts = texts
        self._block_whole = whole
        self._block_run = run.timestamp
        return texts

    async def _resolve_run(self, run: Run | None, params: tuple[str, ...]) -> Run:
        """Use the caller's run when it carries ``params``, else discover one.

        One STAC listing is ~600 KB (measured 2026-09-18), so the run the
        coordinator discovered this tick is reused instead of listing again.
        """
        if run is not None and all(param in run.assets for param in params):
            return run
        return await latest_run(self._session, COLLECTION_FORECAST, params)

    async def fetch_daily(
        self, point: ForecastPoint, *, run: Run | None = None
    ) -> DailyBundle:
        run = await self._resolve_run(run, DAILY_REQUIRED_PARAMS)
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
        run: Run | None = None,
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
        run = await self._resolve_run(run, params)
        now = datetime.now(UTC)
        horizon_end = horizon_end_utc(horizon_days, now)
        horizon_start = now.replace(minute=0, second=0, microsecond=0)

        # Direct cache check (no re-probe): only hit if daily already ran for
        # this very run. A block that degraded on the daily path is absent from
        # the cache and is fetched here like any other hourly file.
        block_cache = (
            {p: t for p, t in self._block_texts.items() if p in self._block_whole}
            if self._block_run == run.timestamp
            else {}
        )
        params_to_fetch = [p for p in params if p not in block_cache]

        results = await asyncio.gather(
            *(
                fetch_hourly_file(
                    self._session,
                    run.asset_url(param),
                    point,
                    horizon_end=horizon_end,
                    cached_start=(
                        hint.block_start
                        if (hint := self._hints.get(param)) is not None
                        else None
                    ),
                )
                for param in params_to_fetch
            )
        )
        text_by_param: dict[str, str] = {}
        run_day = run.timestamp.date()
        for param, result in zip(params_to_fetch, results, strict=True):
            text_by_param[param] = result.text
            if result.block_start is not None:
                # Fold the block offset into the shared per-file hint so the
                # daily path reuses it on the next run of this UTC day.
                header = result.text.split("\n", 1)[0] + "\n"
                self._hints[param] = FileHint(
                    layout=result.layout,
                    utc_day=run_day,
                    header=header,
                    block_start=result.block_start,
                )

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
