"""Data update coordinators for MeteoSwiss Weather.

Two coordinators back a config entry (ADR-0002):

- :class:`StationCoordinator` polls the configured SwissMetNet station's
  10-minute ``now`` file, revalidating it conditionally so an unchanged file
  costs a single 304.
- :class:`ForecastCoordinator` checks the newest local-forecast run once an
  hour and only downloads the (small) daily parameter files when the run
  stamp actually changed, so a quiet hour costs one small STAC request. When
  the hourly option is on it also refreshes the demanded hourly series into the
  :class:`~.store.ForecastStore` as part of the same tick.

The hourly forecast used to hang off a lazy provider that only ran while a card
or automation subscribed (ADR-0002 revision 2, issue #54). ADR-0008 removes it:
with the hourly option on, the demanded files are fetched whether or not
anything subscribes, driven by :class:`HourlyRefresher` from the coordinator's
own refresh. Every consumer — the daily forecast, the zero-degree sensor, the
hourly forecast — reads the store and nothing else, so no entity depends on
another consumer's fetch (the class of bug behind issue #107). The cost still
gates on the **option**: a feature that is off demands nothing (:mod:`.demand`).

Everything upstream-specific lives in the pure ``ogd`` client (ADR-0001);
the coordinators only translate its :class:`OgdError` into ``UpdateFailed``
and hand CSV parsing to the executor via the backend.

Exponential backoff for transient :class:`~ogd.OgdConnectionError` is built
into :class:`~homeassistant.helpers.update_coordinator.DataUpdateCoordinator`
(it marks the update as failed and HA's listener machinery reschedules it with
back-off automatically). No second layer is added here.

A structural :class:`~ogd.OgdParseError` (upstream changed its file layout)
is different: it will recur on every poll until the integration is updated.
The coordinators therefore post a HA repair issue on the first occurrence and
clear it as soon as parsing succeeds again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_HOURLY_HORIZON_DAYS,
    DOMAIN,
    FORECAST_CHECK_INTERVAL,
    HINTS_SAVE_DELAY,
    HOURLY_CANARY_HOURS,
    HOURLY_FAR_MAX_AGE,
    HOURLY_HORIZON_FULL_RUN,
    HOURLY_NEAR_HORIZON_DAYS,
    HOURLY_NEAR_MAX_AGE,
    HOURLY_POINT_MAJOR_MAX_AGE,
    POLLEN_UPDATE_INTERVAL,
    STATION_UPDATE_INTERVAL,
)
from .demand import hourly_demand
from .ogd import (
    CachedResponse,
    DailyForecast,
    ForecastBackend,
    ForecastPoint,
    HourlyForecast,
    Observation,
    OgdConnectionError,
    OgdParseError,
    PollenObservation,
    Run,
    fetch_current,
    fetch_pollen_current,
    fetch_precip_current,
    latest_run_from_day_item,
)
from .ogd.const import (
    COLLECTION_FORECAST,
    DAILY_REQUIRED_PARAMS,
    HOURLY_WIND_SPEED,
    HOURLY_ZERO_DEGREE,
)
from .ogd.forecast import HOURLY_FIELD_BY_PARAM
from .store import ForecastStore

# Issue IDs used in the HA repair-issue registry.
_ISSUE_STATION_PARSE = "parse_error_station"
_ISSUE_PRECIP_PARSE = "parse_error_precip"
_ISSUE_FORECAST_PARSE = "parse_error_forecast"
_ISSUE_POLLEN_PARSE = "parse_error_pollen"
# Prefix for per-parameter escalated-fetch repair issues (ADR-0008 section 5).
# Full ID: f"{_ISSUE_ESCALATED_PREFIX}{param}".
_ISSUE_ESCALATED_PREFIX = "forecast_fetch_escalated_"
# Number of consecutive L3+ fetches that triggers the repair issue.
_ESCALATION_THRESHOLD = 3

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ForecastData:
    """The forecast coordinator's payload: the daily forecast.

    The hourly forecast is not carried here — its per-parameter series live in
    the coordinator's :class:`~.store.ForecastStore`, filled by the daily
    refresh and by :class:`HourlyRefresher`. Entities that show an hour of the
    forecast (the hourly forecast, the current-hour condition, the zero-degree
    sensor) read the store, because more than one path can deliver them
    (ADR-0008).
    """

    daily: list[DailyForecast]


# The point-major group's canary: a continuous field that moves with any model
# update (unlike precipitation, often 0 for hours), fetched with the daily blocks
# so on a coordinator tick its whole-run text is usually already in the shared
# cache — the canary then costs nothing (ADR-0008 section 3, issue #125).
_POINT_MAJOR_CANARY_PARAM = HOURLY_WIND_SPEED


class HourlyRefresher:
    """Refreshes the demanded hourly series into the store (ADR-0008).

    Owned by :class:`ForecastCoordinator` and driven from its refresh once per
    tick with the run the coordinator already discovered. When the hourly option
    is on the demanded files are fetched **whether or not anything subscribes**
    (ADR-0008, "Decided by the owner", item 1) — the lazy, card-driven provider
    of ADR-0002 revision 2 is gone. Everything it fetches is filed in the store,
    per parameter, so every forecast consumer reads the store and nothing else.

    What triggers a refresh is a **canary** read, not a timetable (ADR-0008
    section 3, owner decision 2, issue #125). On every new run, before refreshing
    a group, it reads the point's next few hours of one representative file
    (:data:`HOURLY_CANARY_HOURS`, a few KB with the remembered byte positions) and
    compares them with the store: equal values keep the stored series and re-stamp
    it to the run (:meth:`~.store.ForecastStore.confirm`), different values — or a
    canary that cannot be read — refresh the group. The date-major group (the
    temperature file plus the gated cloud and percentile files, issue #69) is
    represented by the temperature file and, when its canary changed, refreshed
    over the far horizon; the point-major group (precip, symbol, wind, gust,
    direction, the B7/B8/B10 additions) is represented by the wind file. The
    landing-hour timetable of ADR-0002 revision 2 is gone; the near/far/point-major
    ``max_age`` fallbacks remain and still force a refresh so a canary blind spot
    can never let a series go stale unbounded. Which files each group holds comes
    from the demand registry (:func:`~.demand.hourly_demand`), so a disabled
    feature demands nothing.

    A refresh that fails for one parameter keeps its last good series (the store
    does this) and must never fail the daily forecast: :meth:`async_refresh`
    swallows :class:`~ogd.OgdParseError` (posting the shared forecast repair
    issue) and :class:`~ogd.OgdConnectionError` (logging it), always returning
    to the coordinator so the daily payload is unaffected.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        backend: ForecastBackend,
        point: ForecastPoint,
        store: ForecastStore,
        *,
        enabled: bool,
        horizon_days: int,
        cloud_layers: bool = False,
        temp_percentiles: bool = False,
    ) -> None:
        self._hass = hass
        self._backend = backend
        self._point = point
        self._store = store
        self._enabled = enabled
        self._horizon_days = horizon_days
        # The fetch plan for the enabled features, split by fetch strategy
        # (ADR-0008 section 2). ``None`` when the hourly option is off, so a run
        # demands nothing hourly and no hourly-only file is ever requested.
        self._demand = hourly_demand(
            enabled=enabled,
            cloud_layers=cloud_layers,
            temp_percentiles=temp_percentiles,
        )
        # The near tier is a cheap prefix of the far window, so it must never
        # reach past the configured horizon: a user who narrows the horizon
        # below the near default (only horizon 0, "today only") would otherwise
        # see the near fetch leak tomorrow's temperature-only hours — with no
        # symbol/precip/wind, since the point-major group stays trimmed to the
        # configured horizon — that flicker in and out as near and far alternate.
        self._near_horizon_days = (
            self._horizon_days
            if (
                self._horizon_days != HOURLY_HORIZON_FULL_RUN
                and self._horizon_days < HOURLY_NEAR_HORIZON_DAYS
            )
            else HOURLY_NEAR_HORIZON_DAYS
        )
        # Per-group bookkeeping. The ``_fetch`` stamps are when each tier last
        # actually downloaded (the max-age clocks); the ``_run`` stamps are the
        # last run a group was evaluated for — fetched or canary-confirmed — so a
        # run is canaried at most once. The date-major near and far tiers share
        # one evaluated-run stamp because one canary read decides the group.
        self._date_major_run: datetime | None = None
        self._near_fetch: datetime | None = None
        self._far_fetch: datetime | None = None
        self._point_major_run: datetime | None = None
        self._point_major_fetch: datetime | None = None

    @property
    def enabled(self) -> bool:
        """Whether the hourly opt-in is on for this entry."""
        return self._enabled

    @property
    def horizon_days(self) -> int:
        """The configured hourly forecast horizon in local calendar days."""
        return self._horizon_days

    @property
    def demanded_params(self) -> tuple[str, ...]:
        """Every hourly parameter the enabled features demand (empty when off)."""
        return self._demand.params if self._demand is not None else ()

    @property
    def last_fetch(self) -> datetime | None:
        """When the most recent tier download completed; for diagnostics."""
        stamps = [
            stamp
            for stamp in (self._near_fetch, self._far_fetch, self._point_major_fetch)
            if stamp is not None
        ]
        return max(stamps) if stamps else None

    async def async_refresh(self, run: Run | None) -> bool:
        """Refresh whichever tiers are due for ``run``; return if the store changed.

        A no-op when the hourly option is off or no run has been discovered yet.
        Never raises: a structural parse error posts the shared forecast repair
        issue and a transient connection error is logged, both keeping the last
        good series so the daily forecast is unaffected (ADR-0008 section 1).
        """
        if self._demand is None or run is None:
            return False
        now = dt_util.utcnow()
        stamp = run.timestamp
        try:
            changed = await self._refresh_date_major(run, stamp, now)
            changed = await self._refresh_point_major(run, stamp, now) or changed
        except OgdParseError as err:
            async_create_issue(
                self._hass,
                DOMAIN,
                _ISSUE_FORECAST_PARSE,
                is_fixable=False,
                severity=IssueSeverity.WARNING,
                translation_key="parse_error_forecast",
            )
            _LOGGER.warning("hourly forecast parse failed: %s", err)
            return False
        except OgdConnectionError as err:
            _LOGGER.warning("hourly forecast fetch failed: %s", err)
            return False

        async_delete_issue(self._hass, DOMAIN, _ISSUE_FORECAST_PARSE)
        return changed

    async def _canary_changed(
        self, param: str, run: Run, now: datetime
    ) -> bool:
        """Whether ``param``'s next few hours differ from the store (ADR-0008 §3).

        Reads one representative file's coming hours cheaply and compares them
        with the stored series. A read that yields nothing (the file is
        unreachable, absent for the point, or no row was proven) counts as
        changed, so the group refreshes rather than trusting a series it could
        not verify.
        """
        canary = await self._backend.fetch_hourly_canary(
            self._point, param, hours=HOURLY_CANARY_HOURS, run=run
        )
        if not canary:
            return True
        return any(
            self._store.value_at(param, when) != value
            for when, value in canary.items()
        )

    async def _refresh_date_major(
        self, run: Run, stamp: datetime, now: datetime
    ) -> bool:
        """Refresh the date-major group when the canary or a fallback says so.

        The date-major group is the temperature file plus, when enabled, the B9
        cloud and B11 percentile files (issue #69), fetched together as one
        horizon prefix. The temperature file is the group's canary. A group whose
        canary changed is refreshed over the **far** horizon (with row addressing
        the whole horizon is ~100–150 KB and this guarantees the far days are
        current on every real change); the cheaper near-only prefix runs only when
        the near fallback fires while far is still fresh. An unchanged canary keeps
        the stored series and re-stamps it to the run.
        """
        params = self._demand.date_major
        run_changed = stamp != self._date_major_run
        # Read the canary at most once, and only when a fallback has not already
        # made a tier due (a never-fetched or stale tier fetches without a probe).
        verdict: list[bool] = []

        async def canary_changed() -> bool:
            if not verdict:
                verdict.append(await self._canary_changed(params[0], run, now))
            return verdict[0]

        far_due = (
            self._far_fetch is None
            or now - self._far_fetch >= HOURLY_FAR_MAX_AGE
            or (run_changed and await canary_changed())
        )
        if far_due:
            hours = await self._backend.fetch_hourly(
                self._point,
                horizon_days=self._horizon_days,
                params=params,
                run=run,
            )
            self._far_fetch = self._near_fetch = now
            self._date_major_run = stamp
            return self._publish(hours, params, stamp, now)

        near_due = (
            self._near_fetch is None
            or now - self._near_fetch >= HOURLY_NEAR_MAX_AGE
            or (run_changed and await canary_changed())
        )
        if near_due:
            hours = await self._backend.fetch_hourly(
                self._point,
                horizon_days=self._near_horizon_days,
                params=params,
                run=run,
            )
            self._near_fetch = now
            self._date_major_run = stamp
            return self._publish(hours, params, stamp, now)

        if run_changed:
            # A new run whose canary proved the group unchanged: keep the stored
            # series but re-stamp it to this run so it reads as current, not stale.
            for param in params:
                self._store.confirm(param, run=stamp, fetched_at=now)
            self._date_major_run = stamp
        return False

    async def _refresh_point_major(
        self, run: Run, stamp: datetime, now: datetime
    ) -> bool:
        """Refresh the point-major group when the canary or the fallback says so.

        The wind file is the group's canary. A new run whose canary changed (or
        the max-age fallback firing) refreshes the whole group; a new run the
        canary proves unchanged keeps the stored series and re-stamps it (#125).
        """
        params = self._demand.point_major
        run_changed = stamp != self._point_major_run
        due = (
            self._point_major_fetch is None
            or now - self._point_major_fetch >= HOURLY_POINT_MAJOR_MAX_AGE
            or (run_changed and await self._canary_changed(
                _POINT_MAJOR_CANARY_PARAM, run, now))
        )
        if due:
            hours = await self._backend.fetch_hourly(
                self._point,
                horizon_days=self._horizon_days,
                params=params,
                run=run,
            )
            self._point_major_run = stamp
            self._point_major_fetch = now
            return self._publish(hours, params, stamp, now)

        if run_changed:
            for param in params:
                self._store.confirm(param, run=stamp, fetched_at=now)
            self._point_major_run = stamp
        return False

    def _publish(
        self,
        hours: list[HourlyForecast],
        params: tuple[str, ...],
        run: datetime,
        now: datetime,
    ) -> bool:
        """File each fetched parameter's series in the store; return if it changed.

        An empty series (the file degraded) never replaces a stored one, so a
        partial refresh keeps the previous run's series for that parameter
        (ADR-0008 section 1, handled by :meth:`~.store.ForecastStore.put`).

        Passes escalation-ladder metadata (level, requests, bytes, layout) to
        the store when the backend exposes ``get_fetch_meta`` (ADR-0008 §5).
        Duck-typed so the future OGC Features backend need not implement it.
        """
        get_meta = getattr(self._backend, "get_fetch_meta", None)
        changed = False
        for param in params:
            field = HOURLY_FIELD_BY_PARAM[param]
            values = {
                hour.time: value
                for hour in hours
                if (value := getattr(hour, field)) is not None
            }
            meta = get_meta(param) if get_meta is not None else None
            changed = (
                self._store.put(
                    param,
                    values,
                    run=run,
                    fetched_at=now,
                    source="hourly",
                    level=meta[0] if meta is not None else None,
                    requests=meta[1] if meta is not None else None,
                    bytes_fetched=meta[2] if meta is not None else None,
                    layout=meta[3] if meta is not None else None,
                )
                or changed
            )
        return changed


def hourly_from_store(
    store: ForecastStore, params: tuple[str, ...]
) -> list[HourlyForecast]:
    """Rebuild the hourly forecast from the store's per-parameter series (ADR-0008).

    The store holds one series ``{hour → value}`` per parameter, whichever path
    filed it. This reassembles them by hour into :class:`~ogd.HourlyForecast`
    entries over the union of hours present in ``params``. Hours missing any of
    the four fields a weather card must render (temperature, symbol,
    precipitation, wind speed) are dropped so a tier boundary or a ragged file
    head never emits a blank or half-filled entry (issue #92).
    """
    series_by_param: dict[str, object] = {}
    hours: set[datetime] = set()
    for param in params:
        series = store.get(param)
        if series is None:
            continue
        series_by_param[param] = series.values
        hours.update(series.values)

    result: list[HourlyForecast] = []
    for when in sorted(hours):
        fields: dict[str, float | int] = {}
        for param, values in series_by_param.items():
            value = values.get(when)  # type: ignore[attr-defined]
            if value is not None:
                fields[HOURLY_FIELD_BY_PARAM[param]] = value
        hour = HourlyForecast(time=when, **fields)  # type: ignore[arg-type]
        if (
            hour.temperature is None
            or hour.symbol is None
            or hour.precipitation is None
            or hour.wind_speed_kmh is None
        ):
            continue
        result.append(hour)
    return result


class StationCoordinator(DataUpdateCoordinator[Observation]):
    """Poll one station's latest 10-minute observation.

    :class:`PrecipStationCoordinator` reuses this by overriding
    :meth:`_fetch`, the repair-issue key and the log label (ADR-0006); the
    conditional-request cache and the parse/connection handling are shared.
    """

    # Repair-issue key posted on a structural parse failure and the label used
    # in the coordinator name; overridden by the precipitation subclass.
    _issue_id = _ISSUE_STATION_PARSE
    _translation_key = "parse_error_station"
    _label = "observations"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: aiohttp.ClientSession,
        station_abbr: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{station_abbr} {self._label}",
            update_interval=STATION_UPDATE_INTERVAL,
        )
        self._session = session
        self._station_abbr = station_abbr
        # Reused across polls so the station file is revalidated conditionally
        # (If-None-Match / If-Modified-Since); get_text mutates it in place.
        self._cache = CachedResponse(body="")
        # Timestamp of the last successful update; exposed for diagnostics.
        self.last_success: datetime | None = None

    async def _fetch(self) -> Observation:
        """Fetch the latest observation for the configured station."""
        return await fetch_current(
            self._session, self._station_abbr, cache=self._cache
        )

    async def _async_update_data(self) -> Observation:
        try:
            obs = await self._fetch()
        except OgdParseError as err:
            async_create_issue(
                self.hass,
                DOMAIN,
                self._issue_id,
                is_fixable=False,
                severity=IssueSeverity.WARNING,
                translation_key=self._translation_key,
            )
            raise UpdateFailed(
                f"station {self._station_abbr} parse failed: {err}"
            ) from err
        except OgdConnectionError as err:
            raise UpdateFailed(
                f"station {self._station_abbr} update failed: {err}"
            ) from err

        async_delete_issue(self.hass, DOMAIN, self._issue_id)
        self.last_success = dt_util.utcnow()
        return obs


class PrecipStationCoordinator(StationCoordinator):
    """Poll a precipitation-only station's latest 10-minute value (ADR-0006, #70).

    Only created when the user picks a second station from
    ``ch.meteoschweiz.ogd-smn-precip``. Same 10-minute conditional cadence as
    :class:`StationCoordinator`; the returned :class:`~ogd.Observation` carries
    only ``precipitation_10min`` (the collection's ``_t_now.csv`` has just
    ``rre150z0``), which is all the precipitation sensor and weather entity read
    from it.
    """

    _issue_id = _ISSUE_PRECIP_PARSE
    _translation_key = "parse_error_precip"
    _label = "precipitation"

    async def _fetch(self) -> Observation:
        return await fetch_precip_current(
            self._session, self._station_abbr, cache=self._cache
        )


class ForecastCoordinator(DataUpdateCoordinator[ForecastData]):
    """Refresh the daily local forecast, skipping unchanged runs (ADR-0002).

    Keeps the daily forecast fresh from the newest complete run. When the hourly
    option is on it also refreshes the demanded hourly series into
    :attr:`store` through :attr:`hourly_refresher`, as part of the same tick and
    whether or not anything subscribes (ADR-0008). Entities that show an hour of
    the forecast read the store via :meth:`hourly_forecast` and
    :meth:`~.store.ForecastStore.value_at`.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: aiohttp.ClientSession,
        backend: ForecastBackend,
        point: ForecastPoint,
        *,
        hourly_enabled: bool = False,
        hourly_horizon_days: int = DEFAULT_HOURLY_HORIZON_DAYS,
        hourly_cloud_layers: bool = False,
        hourly_temp_percentiles: bool = False,
        hints_store: Store | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"forecast {point.name}",
            update_interval=FORECAST_CHECK_INTERVAL,
        )
        self._session = session
        self._backend = backend
        self._point = point
        # Persisted fetch-ladder hints (issue #133, ADR-0008). The backend keeps
        # its per-file hints in memory; this store lets them survive a restart or
        # reload so the next refresh is not cold. ``None`` (a backend without
        # hints, or a test) simply disables persistence. Restored at setup via
        # :meth:`async_load_hints` and saved (debounced) after every refresh whose
        # hints changed. ``_saved_hints`` is the last snapshot written, so an
        # unchanged tick never schedules a write.
        self._hints_store = hints_store
        self._saved_hints: dict | None = None
        # Whether a persisted hint set was restored at setup; for diagnostics
        # (the raw hints are never dumped — they are kept small, issue #133).
        self.hints_restored = False
        # Timestamp of the run the current daily data came from; exposed for
        # diagnostics and used to skip re-downloading an unchanged run. The
        # weather entity also watches it to trigger the lazy hourly refresh.
        self.last_run: datetime | None = None
        # The run discovered by the latest tick, handed down to every fetch of
        # that tick so nothing below lists STAC again (ADR-0008 section 3).
        self.run: Run | None = None
        # Timestamp of the last successful update; exposed for diagnostics.
        self.last_success: datetime | None = None
        # Day-item caches for conditional run discovery (issue #120, ADR-0008).
        # Two caches survive between ticks so ETag-based 304 responses are
        # possible; they are rotated on a UTC day boundary (today → yesterday).
        self._day_item_date: date | None = None
        self._today_item_cache = CachedResponse(body="")
        self._yesterday_item_cache = CachedResponse(body="")
        # Per-parameter series of the point; the one source for entities that
        # show an hour of the forecast, whichever path fetched it (ADR-0008).
        self.store = ForecastStore()
        # Parameters that currently have an active escalation repair issue, so
        # the issue can be cleared when the streak drops below the threshold.
        self._escalation_issue_params: set[str] = set()
        # The eager hourly refresh: the coordinator drives it once per tick from
        # its own refresh, so the demanded files are fetched whenever the option
        # is on, with no dependency on a subscriber (ADR-0008).
        self.hourly_refresher = HourlyRefresher(
            hass,
            backend,
            point,
            self.store,
            enabled=hourly_enabled,
            horizon_days=hourly_horizon_days,
            cloud_layers=hourly_cloud_layers,
            temp_percentiles=hourly_temp_percentiles,
        )

    async def _async_update_data(self) -> ForecastData:
        # Rotate the day-item caches on a UTC day boundary so the ETag for
        # today's item does not pollute yesterday's URL on the following day.
        today_date = dt_util.utcnow().date()
        if self._day_item_date != today_date:
            self._yesterday_item_cache = self._today_item_cache
            self._today_item_cache = CachedResponse(body="")
            self._day_item_date = today_date

        try:
            run = await latest_run_from_day_item(
                self._session,
                COLLECTION_FORECAST,
                DAILY_REQUIRED_PARAMS,
                self._today_item_cache,
                self._yesterday_item_cache,
            )
        except OgdParseError as err:
            async_create_issue(
                self.hass,
                DOMAIN,
                _ISSUE_FORECAST_PARSE,
                is_fixable=False,
                severity=IssueSeverity.WARNING,
                translation_key="parse_error_forecast",
            )
            raise UpdateFailed(f"forecast run discovery parse failed: {err}") from err
        except OgdConnectionError as err:
            raise UpdateFailed(f"forecast run discovery failed: {err}") from err
        self.run = run

        # An unchanged run means the MB-scale daily files would be identical:
        # skip the download entirely and keep serving what we have (ADR-0002).
        if (
            self.last_run is not None
            and run.timestamp == self.last_run
            and self.data is not None
        ):
            daily = self.data.daily
        else:
            try:
                # The backend downloads the small daily files (plus the
                # point-major wind and zero-degree blocks) and parses them off
                # the event loop; a future per-point backend swaps in here.
                # Pass the hourly horizon so the daily path fetches zprfr0hs
                # with a covering window when the hourly option is on, avoiding
                # a duplicate download of the same file (issue #134).
                bundle = await self._backend.fetch_daily(
                    self._point,
                    run=run,
                    hourly_horizon_days=(
                        self.hourly_refresher.horizon_days
                        if self.hourly_refresher.enabled
                        else None
                    ),
                )
            except OgdParseError as err:
                async_create_issue(
                    self.hass,
                    DOMAIN,
                    _ISSUE_FORECAST_PARSE,
                    is_fixable=False,
                    severity=IssueSeverity.WARNING,
                    translation_key="parse_error_forecast",
                )
                raise UpdateFailed(f"daily forecast parse failed: {err}") from err
            except OgdConnectionError as err:
                raise UpdateFailed(f"daily forecast fetch failed: {err}") from err
            daily = bundle.daily
            # Pass escalation-ladder metadata when the backend freshly fetched
            # zprfr0hs; use confirm() when the daily canary said nothing changed
            # (fetch_meta is empty) so the store keeps the last good provenance
            # (ADR-0008 section 5).
            zmeta = bundle.fetch_meta.get(HOURLY_ZERO_DEGREE)
            now = dt_util.utcnow()
            if zmeta is not None:
                self.store.put(
                    HOURLY_ZERO_DEGREE,
                    bundle.zero_degree_level,
                    run=run.timestamp,
                    fetched_at=now,
                    source="daily",
                    level=zmeta[0],
                    requests=zmeta[1],
                    bytes_fetched=zmeta[2],
                    layout=zmeta[3],
                )
            else:
                # Canary confirmed: keep the stored series, re-stamp to new run.
                self.store.confirm(
                    HOURLY_ZERO_DEGREE,
                    run=run.timestamp,
                    fetched_at=now,
                )
            self.last_run = run.timestamp

        async_delete_issue(self.hass, DOMAIN, _ISSUE_FORECAST_PARSE)
        # With the hourly option on, refresh the demanded hourly series into the
        # store as part of the coordinator's own tick (ADR-0008): no card,
        # subscription or service call is needed. It swallows its own errors, so
        # an hourly fetch failure keeps the last good series and never fails the
        # daily forecast (ADR-0008 section 1).
        await self.hourly_refresher.async_refresh(run)
        # Sync escalation repair issues once per tick after all paths have
        # written to the store (ADR-0008 section 5).
        self._sync_escalation_issues(run)
        # Persist the backend's fetch hints when this tick changed them, so the
        # next restart is warm (issue #133). Debounced and no-op on no change.
        self._save_hints_if_changed()
        self.last_success = dt_util.utcnow()
        return ForecastData(daily=daily)

    async def async_load_hints(self) -> None:
        """Restore the backend's fetch-ladder hints from disk (issue #133).

        Called once before the first refresh so it is already warm. A missing,
        unreadable or corrupt store is treated as "no hints": the ladder starts
        cold, which is correct, never wrong. Kept off the ``ogd`` package
        (ADR-0001) — the backend only exposes plain export/import of dicts.
        """
        if self._hints_store is None:
            return
        import_hints = getattr(self._backend, "import_hints", None)
        if import_hints is None:
            return
        try:
            stored = await self._hints_store.async_load()
        except (HomeAssistantError, ValueError, OSError) as err:
            _LOGGER.debug("stored forecast hints unreadable, starting cold: %s", err)
            return
        if not stored:
            return
        count = import_hints(stored)
        self.hints_restored = count > 0
        # Snapshot what the backend now holds so a following unchanged tick does
        # not re-save the very hints just loaded.
        export = getattr(self._backend, "export_hints", None)
        self._saved_hints = export() if export is not None else None
        _LOGGER.debug("restored %d forecast fetch hint(s) from storage", count)

    def _save_hints_if_changed(self) -> None:
        """Schedule a debounced save when the backend's hints changed (issue #133).

        Comparing against the last written snapshot keeps a quiet tick — most
        ticks, once positions are learned — from writing at all; only a genuine
        change (a new UTC day's offsets, a re-learned geometry) hits the disk,
        and even then the ``Store`` debounce coalesces bursts of a single run.
        """
        if self._hints_store is None:
            return
        export = getattr(self._backend, "export_hints", None)
        if export is None:
            return
        hints = export()
        if hints == self._saved_hints:
            return
        self._saved_hints = hints
        self._hints_store.async_delay_save(lambda: hints, HINTS_SAVE_DELAY)

    def _sync_escalation_issues(self, run: Run) -> None:
        """Post or clear ``forecast_fetch_escalated_<param>`` repair issues.

        A parameter whose escalation streak reaches :data:`_ESCALATION_THRESHOLD`
        consecutive L3+ fetches raises an issue naming the file and the bytes
        spent on the last fetch, so an upstream re-sort is visible within hours
        (ADR-0008 section 5). The issue is cleared as soon as the streak drops
        below the threshold (the fetch ladder found a cheap strategy again).
        """
        params_with_issue: set[str] = set()
        for param, series in self.store._series.items():
            streak = self.store.escalation_streak(param)
            issue_id = f"{_ISSUE_ESCALATED_PREFIX}{param}"
            if streak >= _ESCALATION_THRESHOLD:
                prov = series.provenance
                file_url = run.assets.get(param, param)
                async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=IssueSeverity.WARNING,
                    translation_key="forecast_fetch_escalated",
                    translation_placeholders={
                        "param": param,
                        "file": file_url,
                        "bytes": str(
                            prov.bytes_fetched
                            if prov.bytes_fetched is not None
                            else "unknown"
                        ),
                    },
                )
                params_with_issue.add(param)
            elif param in self._escalation_issue_params:
                async_delete_issue(self.hass, DOMAIN, issue_id)
        # Clear issues for params that are no longer in the store at all.
        for param in self._escalation_issue_params - params_with_issue:
            if param not in self.store._series:
                async_delete_issue(
                    self.hass, DOMAIN, f"{_ISSUE_ESCALATED_PREFIX}{param}"
                )
        self._escalation_issue_params = params_with_issue

    def hourly_forecast(self) -> list[HourlyForecast] | None:
        """Rebuild the hourly forecast from the store, or ``None`` (ADR-0008).

        Reads the store — the single source the eager hourly refresh fills — and
        never triggers a download of its own. ``None`` when the hourly option is
        off or nothing has been delivered yet.
        """
        if not self.hourly_refresher.enabled:
            return None
        hourly = hourly_from_store(self.store, self.hourly_refresher.demanded_params)
        return hourly or None


class PollenCoordinator(DataUpdateCoordinator[PollenObservation]):
    """Poll one pollen station's latest hourly observation (ADR-0005).

    Fetches ``_h_now.csv`` at most once per hour, conditional on the ETag so an
    unchanged file costs only a 304. Raises a HA repair issue on a structural
    parse failure and clears it as soon as parsing succeeds again.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: aiohttp.ClientSession,
        station_abbr: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{station_abbr} pollen",
            update_interval=POLLEN_UPDATE_INTERVAL,
        )
        self._session = session
        self._station_abbr = station_abbr
        self._cache = CachedResponse(body="")
        self.last_success: datetime | None = None

    async def _async_update_data(self) -> PollenObservation:
        try:
            obs = await fetch_pollen_current(
                self._session, self._station_abbr, cache=self._cache
            )
        except OgdParseError as err:
            async_create_issue(
                self.hass,
                DOMAIN,
                _ISSUE_POLLEN_PARSE,
                is_fixable=False,
                severity=IssueSeverity.WARNING,
                translation_key="parse_error_pollen",
            )
            raise UpdateFailed(
                f"pollen {self._station_abbr} parse failed: {err}"
            ) from err
        except OgdConnectionError as err:
            raise UpdateFailed(
                f"pollen {self._station_abbr} update failed: {err}"
            ) from err

        async_delete_issue(self.hass, DOMAIN, _ISSUE_POLLEN_PARSE)
        self.last_success = dt_util.utcnow()
        return obs
