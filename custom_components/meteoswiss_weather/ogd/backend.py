"""Forecast backend seam (ADR-0002).

The daily forecast is assembled from the bulk CSV files today; MeteoSwiss has
announced a per-point OGC Features API for the end of 2026. Both live behind
:class:`ForecastBackend`, so swapping to the point API when it ships is a
contained change that never reaches the coordinator or the entities.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

import aiohttp

from .const import (
    COLLECTION_FORECAST,
    DAILY_BLOCK_PARAMS,
    DAILY_MAX_AGE,
    DAILY_REQUIRED_PARAMS,
    DAILY_TEMP_MAX,
    DAILY_WIND_PARAMS,
    DAILY_ZERO_DEGREE_WINDOW_HOURS,
    FORECAST_ENCODING,
    HOURLY_HORIZON_FULL_RUN,
    HOURLY_PRECIP_PROBABILITY,
    HOURLY_REQUIRED_PARAMS,
    HOURLY_ZERO_DEGREE,
    OGD_MAX_CONCURRENT_REQUESTS,
)
from .forecast import (
    HOURLY_FIELD_BY_PARAM,
    aggregate_daily_precip_probability,
    aggregate_daily_wind,
    parse_daily,
    parse_hourly,
)
from .hourly import FileHint, fetch_series, horizon_end_utc
from .models import (
    DailyBundle,
    ForecastPoint,
    HourlyForecast,
    OgdConnectionError,
)
from .stac import Run, latest_run

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _CachedSeries:
    """One parameter's fetched rows for the current run and the window they cover.

    The two forecast paths share one per-run cache (ADR-0008 section 4): both
    ``fetch_daily()`` and ``fetch_hourly()`` read and fill it, so neither
    downloads a file the other already has for the run. A ``whole_run`` text
    (a point-major block, or a date-major file addressed for the whole run)
    serves any request; a windowed text — a date-major file row-addressed to
    the demanded hours — serves only a request whose window it contains, so the
    daily path's 48 h zero-degree window never silently shortens a longer hourly
    horizon (issue #123).
    """

    text: str
    whole_run: bool
    window_start: datetime | None
    window_end: datetime | None

    def covers(
        self, window_start: datetime | None, window_end: datetime | None
    ) -> bool:
        """Whether this cached text answers a request for ``[start, end)``."""
        if self.whole_run:
            return True
        # A whole-run request (window None) is only served by a whole-run text.
        if window_start is None or window_end is None:
            return False
        if self.window_start is None or self.window_end is None:
            return False
        return self.window_start <= window_start and self.window_end >= window_end


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

    async def fetch_hourly_canary(
        self,
        point: ForecastPoint,
        param: str,
        *,
        hours: int,
        run: Run | None = None,
    ) -> dict[datetime, float | int] | None: ...


class BulkCsvBackend:
    """Assembles the forecast from the bulk per-parameter CSV files.

    Discovers the newest complete run (STAC), downloads its small daily files
    and parses them off the event loop (ADR-0002). The point-major blocks of
    :data:`DAILY_BLOCK_PARAMS` are also fetched with each daily refresh (~5 KB
    each via the #50 block strategy): the three wind files for the daily wind
    fields (issue #60, ADR-0002 revision 3), ``rp0003i0`` for the derived daily
    precipitation probability (issue #112, revision 5) and ``zprfr0hs`` for the
    hourly zero-degree levels (issue #107, revision 6). Every file a run fetches
    lands in one shared per-run series cache keyed by parameter, so the daily and
    hourly paths never download the same file twice for a run (issue #123).
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        # One shared limiter across every file of a refresh (issue #132, ADR-0008).
        # A cold refresh fans out ~19 files, each row-addressed with several
        # byte-range probes, all under ``asyncio.gather``; without a bound that is
        # ~800 requests in ~2 s against ``data.geo.admin.ch``. This semaphore caps
        # the requests in flight to :data:`OGD_MAX_CONCURRENT_REQUESTS`, so the
        # daily and hourly paths together stay a steady trickle instead of a burst.
        # Bound on the fetch loop, so it is created here and reused across ticks.
        self._limiter = asyncio.Semaphore(OGD_MAX_CONCURRENT_REQUESTS)
        # One :class:`FileHint` per parameter (ADR-0008, issue #121): the file's
        # layout, the UTC day it was learned on and the byte positions of the
        # point's rows (point-major block start, date-major row geometry, header,
        # first/last stamp). Remembered across the runs of a UTC day so the next
        # fetch skips classification and the probes and verifies through the rows
        # it finds. Shared between the daily and hourly paths; a hint only ever
        # saves requests, a stale one costs a fresh look.
        self._hints: dict[str, FileHint] = {}
        # One per-run series cache keyed by parameter (ADR-0008 section 4, issue
        # #123). Both the daily and the hourly path read and fill it, so a file
        # either path fetched for the current run is reused by the other without
        # a second download. Each entry records the window its text covers so a
        # windowed date-major fetch is never mistaken for the whole run.
        self._series: dict[str, _CachedSeries] = {}
        self._series_run: datetime | None = None
        # Files proven (by a full download) to carry no row for the point,
        # remembered for the UTC day so the proof is not repeated every run.
        self._absent: dict[str, date] = {}
        # The last daily bundle this backend built, and when. On a new run the
        # daily canary (ADR-0008 section 3, issue #125) fetches the representative
        # temperature-maxima file and, when its per-day values still match this
        # bundle, keeps it instead of re-fetching the other daily files and the
        # point-major blocks. Reset never lives longer than DAILY_MAX_AGE.
        self._last_daily: DailyBundle | None = None
        self._last_daily_at: datetime | None = None
        # Escalation metadata (level, requests, bytes, layout) for each parameter
        # that was freshly fetched (not served from the per-run cache) in the
        # current run. Reset with the per-run series cache on every new run so
        # only parameters actually fetched in the current run appear here
        # (ADR-0008 section 5).
        self._last_meta: dict[str, tuple[int, int, int, str | None]] = {}

    def _reset_series_cache(self, run: Run) -> None:
        """Drop the per-run series cache when a new run has landed (issue #123)."""
        if self._series_run != run.timestamp:
            self._series = {}
            self._last_meta = {}
            self._series_run = run.timestamp

    async def _fetch_one(
        self,
        point: ForecastPoint,
        run: Run,
        param: str,
        *,
        window_start: datetime | None,
        window_end: datetime | None,
        step: timedelta,
        label: str,
        degrade_absent: bool,
    ) -> str | None:
        """Return ``point``'s rows of ``param`` for ``run``, using the shared cache.

        Serves the request from :attr:`_series` when a text already fetched for
        this run covers the window (so neither forecast path downloads a file the
        other has, ADR-0008 section 4); otherwise climbs the escalation ladder
        (:func:`~.hourly.fetch_series`) and files the result. ``degrade_absent``
        distinguishes the optional daily blocks — where a file with no row for the
        point degrades to ``None`` and is remembered absent for the UTC day — from
        the required daily and hourly files, whose text is always returned.
        Raises :class:`OgdConnectionError` on an unreachable file; the caller
        decides whether that degrades one field or the whole refresh.
        """
        cached = self._series.get(param)
        if cached is not None and cached.covers(window_start, window_end):
            return cached.text

        today = datetime.now(UTC).date()
        if degrade_absent and self._absent.get(param) == today:
            return None

        result = await fetch_series(
            self._session,
            run.asset_url(param),
            point,
            window_start=window_start,
            window_end=window_end,
            hint=self._hints.get(param),
            utc_day=run.timestamp.date(),
            step=step,
            limiter=self._limiter,
        )
        # Escalating to a prefix or the whole file is correct but worth seeing:
        # it usually means upstream re-sorted the file.
        log = _LOGGER.warning if result.level >= 3 else _LOGGER.debug
        log(
            "%s %s for run %s: layout %s, level %d, %d requests, %d bytes",
            label,
            param,
            run.timestamp.isoformat(),
            result.layout.value,
            result.level,
            result.requests,
            result.bytes,
        )
        if result.hint is not None:
            self._hints[param] = result.hint
        # Remember the fetch metadata so the coordinator can pass it to the store
        # (ADR-0008 section 5). Stored per-run (reset with the series cache above).
        self._last_meta[param] = (
            result.level,
            result.requests,
            result.bytes,
            result.layout.value if result.layout is not None else None,
        )
        if degrade_absent and not result.has_rows:
            if result.level == 4:
                self._absent[param] = today
            return None
        self._series[param] = _CachedSeries(
            text=result.text,
            whole_run=result.whole_run,
            window_start=None if result.whole_run else window_start,
            window_end=None if result.whole_run else window_end,
        )
        return result.text

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
        one file's trouble never takes the others down with it. Results land in
        the shared per-run cache so the hourly path reuses them (issue #123).
        """
        self._reset_series_cache(run)
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
                self._fetch_one(
                    point,
                    run,
                    param,
                    window_start=windows.get(param, (None, None))[0],
                    window_end=windows.get(param, (None, None))[1],
                    step=timedelta(hours=1),
                    label="block",
                    degrade_absent=True,
                )
                for param in present
            ),
            return_exceptions=True,
        )

        texts: dict[str, str] = {}
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
            if result is not None:
                texts[param] = result
        return texts

    async def _fetch_daily_texts(
        self, point: ForecastPoint, run: Run
    ) -> dict[str, str]:
        """Return the point's rows of the four daily files, via the ladder.

        The daily ``p``-variants are date-major with nine day blocks (~148 KB
        each), so each is row-addressed for the whole run at a one-day step and
        climbs to the full file only when addressing cannot prove all nine days
        (issue #122, ADR-0008 section 4). The shared per-file hint (issue #121)
        skips classification on the next run of the same UTC day; ``parse_daily``
        reads the returned text — header plus the point's rows — unchanged.

        Unlike the optional blocks, these files are required to build any daily
        forecast, so a connection error propagates rather than degrading to a
        partial bundle (the coordinator keeps the last good data, ADR-0008). The
        whole-run texts land in the shared per-run cache like every other file.
        """
        results = await asyncio.gather(
            *(
                self._fetch_one(
                    point,
                    run,
                    param,
                    window_start=None,
                    window_end=None,
                    step=timedelta(days=1),
                    label="daily",
                    degrade_absent=False,
                )
                for param in DAILY_REQUIRED_PARAMS
            )
        )
        # degrade_absent=False never returns None, so every required file has a
        # text (header plus the point's rows) for parse_daily to read.
        return {
            param: text
            for param, text in zip(DAILY_REQUIRED_PARAMS, results, strict=True)
            if text is not None
        }

    def get_fetch_meta(
        self, param: str
    ) -> tuple[int, int, int, str | None] | None:
        """Fetch metadata ``(level, requests, bytes, layout)`` for the last real
        fetch of ``param`` in the current run.

        Returns ``None`` when ``param`` was not freshly fetched this run (served
        from the per-run series cache, or the run cache was reset before
        ``param`` was fetched). The coordinator uses this to decide between a
        provenanced :meth:`~.store.ForecastStore.put` and a lightweight
        :meth:`~.store.ForecastStore.confirm` (ADR-0008 section 5).
        """
        return self._last_meta.get(param)

    def export_hints(self) -> dict[str, dict[str, Any]]:
        """The per-file fetch hints as JSON-serialisable dicts (issue #133).

        The hints (layout, the UTC day they were learned on, the point's byte
        positions) live only in memory, so after a Home Assistant restart the
        next refresh is cold — a point block costs ~37 requests and a
        row-addressed file re-learns its geometry with a ~150 KB scan. The
        integration layer persists this mapping with ``helpers.storage.Store``
        and feeds it back through :meth:`import_hints` at the next setup. Kept
        HA-free (ADR-0001): the storage itself is done in the coordinator.
        """
        return {param: hint.to_dict() for param, hint in self._hints.items()}

    def import_hints(self, data: Mapping[str, Any] | None) -> int:
        """Load hints previously produced by :meth:`export_hints`; return the count.

        Defensive by design (issue #133): a hint is only ever a hint, so a
        malformed, stale or corrupt entry is skipped with a debug log rather
        than raised, and the ladder re-verifies every position it is handed —
        a bad store costs a fresh look, never a wrong row (ADR-0008). Offsets
        from another UTC day or a different point simply fail their verification
        on the next fetch and are re-learned.
        """
        if not isinstance(data, Mapping):
            return 0
        restored: dict[str, FileHint] = {}
        for param, raw in data.items():
            try:
                restored[str(param)] = FileHint.from_dict(raw)
            except (AttributeError, KeyError, TypeError, ValueError) as err:
                _LOGGER.debug("ignoring stored hint for %s: %s", param, err)
        self._hints = restored
        return len(restored)

    async def _resolve_run(self, run: Run | None, params: tuple[str, ...]) -> Run:
        """Use the caller's run when it carries ``params``, else discover one.

        One STAC listing is ~600 KB (measured 2026-09-18), so the run the
        coordinator discovered this tick is reused instead of listing again.
        """
        if run is not None and all(param in run.assets for param in params):
            return run
        return await latest_run(self._session, COLLECTION_FORECAST, params)

    async def _daily_canary_unchanged(
        self, point: ForecastPoint, run: Run, now: datetime
    ) -> bool:
        """Whether ``run`` leaves the last daily forecast's per-day maxima intact.

        The canary (ADR-0008 section 3, issue #125): the representative daily file
        (``tre200px``, temperature maxima) is fetched for the whole run — the same
        whole-run read :meth:`_fetch_daily_texts` needs, so it lands in the shared
        per-run cache and a full fetch below reuses it. When its per-day values
        still equal the last built forecast and that forecast is within
        :data:`DAILY_MAX_AGE`, the run did not move the daily figures and the other
        three daily files plus the point-major blocks are not re-fetched. A file
        that cannot be read, an empty comparison or a stale last forecast counts
        as changed, so the ladder does a full fetch.
        """
        if (
            self._last_daily is None
            or self._last_daily_at is None
            or now - self._last_daily_at >= DAILY_MAX_AGE
        ):
            return False
        text = await self._fetch_one(
            point,
            run,
            DAILY_TEMP_MAX,
            window_start=None,
            window_end=None,
            step=timedelta(days=1),
            label="daily-canary",
            degrade_absent=False,
        )
        if not text:
            return False
        loop = asyncio.get_running_loop()
        canary = await loop.run_in_executor(
            None, parse_daily, {DAILY_TEMP_MAX: text}, point
        )
        by_day = {d.date: d.temp_max for d in canary}
        previous = {d.date: d.temp_max for d in self._last_daily.daily}
        return bool(by_day) and by_day == previous

    async def fetch_daily(
        self, point: ForecastPoint, *, run: Run | None = None
    ) -> DailyBundle:
        run = await self._resolve_run(run, DAILY_REQUIRED_PARAMS)
        self._reset_series_cache(run)
        now = datetime.now(UTC)
        # Canary first (issue #125): if the representative temperature file is
        # unchanged, keep the last forecast and skip the rest of the daily fetch.
        if await self._daily_canary_unchanged(point, run, now):
            _LOGGER.debug(
                "daily canary unchanged for run %s: keeping the last forecast",
                run.timestamp.isoformat(),
            )
            assert self._last_daily is not None  # guaranteed by the canary check
            # Nothing but the temperature canary was fetched, so the returned
            # bundle must not carry the earlier real fetch's escalation metadata
            # (ADR-0008 §5): an empty fetch_meta is the signal that lets the
            # coordinator confirm() the stored series instead of re-put()ing it,
            # which would re-count the old level in the escalation streak.
            return replace(self._last_daily, fetch_meta={})
        # Route the daily files through the escalation ladder, concurrently with
        # the point-major blocks. Each daily p-variant is date-major with nine
        # ~148 KB day blocks, so row addressing at a one-day step reads a few KB
        # per file instead of the ~1.3 MB whole file (issue #122, ADR-0008). The
        # canary already fetched tre200px into the shared cache, so its whole-run
        # text is reused here rather than downloaded again.
        text_by_param, block_texts = await asyncio.gather(
            self._fetch_daily_texts(point, run),
            self._get_block_texts(point, run),
        )
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

        # Collect fetch metadata for every parameter actually fetched in this
        # call (not served from cache). The coordinator uses it to pass
        # provenance to the store and to decide put vs confirm (ADR-0008 §5).
        fetch_meta = {
            p: meta
            for p in list(DAILY_REQUIRED_PARAMS) + list(DAILY_BLOCK_PARAMS)
            if (meta := self._last_meta.get(p)) is not None
        }
        bundle = DailyBundle(
            daily=daily, zero_degree_level=zero_degree, fetch_meta=fetch_meta
        )
        # Remember it so the next run's canary can prove it still holds (#125).
        self._last_daily = bundle
        self._last_daily_at = now
        return bundle

    async def fetch_hourly_canary(
        self,
        point: ForecastPoint,
        param: str,
        *,
        hours: int,
        run: Run | None = None,
    ) -> dict[datetime, float | int] | None:
        """Read ``point``'s next ``hours`` of one file's ``param`` cheaply (#125).

        The canary behind the run-scoped refresh (ADR-0008 section 3): a small
        window ``[start of the current hour, +hours)`` fetched through the same
        escalation ladder and shared cache as a full fetch, using the remembered
        byte positions. Returns ``{hour(UTC) → value}`` for the parameter's field,
        or ``None`` when nothing could be read (the file is unreachable, absent
        for the point, or no row was proven) so the caller treats it as changed
        and refreshes. A point-major file's whole block is read regardless of the
        window (it is contiguous), so it lands whole in the cache and a following
        group refresh of the same run reuses it without a second download.
        """
        run = await self._resolve_run(run, (param,))
        self._reset_series_cache(run)
        now = datetime.now(UTC)
        start = now.replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=hours)
        try:
            text = await self._fetch_one(
                point,
                run,
                param,
                window_start=start,
                window_end=end,
                step=timedelta(hours=1),
                label="canary",
                degrade_absent=False,
            )
        except OgdConnectionError as err:
            _LOGGER.debug("canary %s unreadable, treating as changed: %s", param, err)
            return None
        if not text:
            return None
        loop = asyncio.get_running_loop()
        hourly = await loop.run_in_executor(
            None, parse_hourly, {param: text}, point, end, start
        )
        field = HOURLY_FIELD_BY_PARAM[param]
        values = {
            hour.time: value
            for hour in hourly
            if (value := getattr(hour, field)) is not None
        }
        return values or None

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
        # the provider enforces (ADR-0002 revision 2). Every requested parameter
        # is fetched through the escalation ladder (:func:`~.hourly.fetch_series`,
        # ADR-0008 section 4, issue #123) for the window ``[start of the current
        # hour, horizon_end)``: a date-major file (``tre200h0``) is row-addressed
        # to the horizon for ~100–300 KB instead of the ~10 MB date-major prefix,
        # and a point-major file returns its ~5 KB block. A ``None`` horizon
        # (the full-run option) demands the whole run, which the ladder serves by
        # the full file when row addressing would overrun the request cap.
        #
        # ``params`` is the subset to fetch — the tiered provider (issue #68) asks
        # for the date-major temperature file (near/far horizon) and the
        # point-major group on independent schedules, so this fetches only what a
        # given tier needs rather than the whole set every time.
        #
        # Both forecast paths read and fill the shared per-run cache, so a file
        # fetch_daily() already fetched for this run (the wind, probability and
        # zero-degree blocks) is reused here without a second download, and a file
        # fetched here is likewise reused by a later daily refresh of the same run
        # (issue #123).
        run = await self._resolve_run(run, params)
        self._reset_series_cache(run)
        now = datetime.now(UTC)
        horizon_end = horizon_end_utc(horizon_days, now)
        horizon_start = now.replace(minute=0, second=0, microsecond=0)
        # A None horizon is the whole-run demand: pass an open window so the
        # ladder does not row-address ~220 hour blocks (issue #123).
        window_start = horizon_start if horizon_end is not None else None

        results = await asyncio.gather(
            *(
                self._fetch_one(
                    point,
                    run,
                    param,
                    window_start=window_start,
                    window_end=horizon_end,
                    step=timedelta(hours=1),
                    label="hourly",
                    degrade_absent=False,
                )
                for param in params
            )
        )
        text_by_param: dict[str, str] = {
            param: text
            for param, text in zip(params, results, strict=True)
            if text is not None
        }

        # The download is the cost this option pays for; record it so a user can
        # see what enabling the hourly forecast actually spends (ADR-0002).
        total_bytes = sum(len(text.encode(FORECAST_ENCODING)) for text in
                          text_by_param.values())
        _LOGGER.debug(
            "hourly forecast run %s (horizon_days=%s): %d bytes across %d files",
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
