"""Data update coordinators for MeteoSwiss Weather.

Two coordinators back a config entry (ADR-0002):

- :class:`StationCoordinator` polls the configured SwissMetNet station's
  10-minute ``now`` file, revalidating it conditionally so an unchanged file
  costs a single 304.
- :class:`ForecastCoordinator` checks the newest local-forecast run once an
  hour and only downloads the (small) daily parameter files when the run
  stamp actually changed, so a quiet hour costs one small STAC request.

The hourly forecast is the whole traffic budget (~30 MB per file), so it is no
longer fetched from the coordinator's eager path. It hangs off a lazy
:class:`HourlyForecastProvider` that ``weather.async_forecast_hourly`` awaits:
Home Assistant only calls that method while a card or automation subscribes, or
on a ``weather.get_forecasts`` service call, so an instance nobody looks at pays
nothing (ADR-0002 revision 2, issue #54). The coordinator keeps tracking the run
stamp; the weather entity turns a run change into an ``async_update_listeners``
push, which downloads only when someone is actually listening.

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

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_HOURLY_HORIZON_DAYS,
    DOMAIN,
    FORECAST_CHECK_INTERVAL,
    HOURLY_FORECAST_MIN_INTERVAL,
    STATION_UPDATE_INTERVAL,
)
from .ogd import (
    CachedResponse,
    DailyForecast,
    ForecastBackend,
    ForecastPoint,
    HourlyForecast,
    Observation,
    OgdConnectionError,
    OgdParseError,
    fetch_current,
    latest_run,
)
from .ogd.const import COLLECTION_FORECAST, DAILY_REQUIRED_PARAMS

# Issue IDs used in the HA repair-issue registry.
_ISSUE_STATION_PARSE = "parse_error_station"
_ISSUE_FORECAST_PARSE = "parse_error_forecast"

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ForecastData:
    """The forecast coordinator's payload: the daily forecast.

    The hourly forecast is not carried here — it is fetched lazily through
    :class:`HourlyForecastProvider` only when something asks for it (issue #54).
    """

    daily: list[DailyForecast]


class HourlyForecastProvider:
    """Lazily fetches and caches the bulk hourly forecast (ADR-0002 revision 2).

    The hourly parameter files are ~30 MB each — the whole traffic budget — so
    they are downloaded only when the hourly forecast is actually requested:
    ``weather.async_forecast_hourly`` awaits :meth:`async_get_hourly`, which
    Home Assistant calls while a card or automation subscribes or on a
    ``weather.get_forecasts`` service call. No caller means no download.

    The result is cached and keyed by the forecast run stamp: a fetch happens
    only when the run changed *and* the cached data is older than
    :data:`HOURLY_FORECAST_MIN_INTERVAL` (the staleness floor B14a keeps; B14b
    replaces it with the measured model tiers). A :class:`asyncio.Lock`
    serialises concurrent callers so a card and a service call arriving together
    still download the set once.

    Errors never propagate out of the forecast method: a structural
    :class:`~ogd.OgdParseError` posts the shared forecast repair issue and a
    transient :class:`~ogd.OgdConnectionError` is logged, both keeping the last
    good data so the entity degrades rather than raises.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        backend: ForecastBackend,
        point: ForecastPoint,
        *,
        enabled: bool,
        horizon_days: int,
    ) -> None:
        self._hass = hass
        self._backend = backend
        self._point = point
        self._enabled = enabled
        self._horizon_days = horizon_days
        self._lock = asyncio.Lock()
        # Cached forecast, the run it came from, and when it was downloaded.
        self._hourly: list[HourlyForecast] | None = None
        self._cached_run: datetime | None = None
        self._last_fetch: datetime | None = None

    @property
    def enabled(self) -> bool:
        """Whether the hourly opt-in is on for this entry."""
        return self._enabled

    @property
    def last_fetch(self) -> datetime | None:
        """When the last hourly download completed; exposed for diagnostics."""
        return self._last_fetch

    @property
    def cached_hourly(self) -> list[HourlyForecast] | None:
        """The cached hourly forecast without triggering a fetch.

        The weather ``condition`` reads this so it can sharpen to the current
        hour's symbol *if* the data is already here, without paying the download
        just to compute a condition (issue #54).
        """
        return self._hourly

    def _is_stale(self) -> bool:
        """Whether the cached hourly data is older than the staleness floor."""
        if self._last_fetch is None:
            return True
        return dt_util.utcnow() - self._last_fetch >= HOURLY_FORECAST_MIN_INTERVAL

    async def async_get_hourly(
        self, run: datetime | None
    ) -> list[HourlyForecast] | None:
        """Return the hourly forecast for ``run``, fetching only if needed.

        ``run`` is the newest run stamp the coordinator tracked. A fetch happens
        only when it differs from the cached run and the cache is stale; an
        unchanged run or a fetch within the floor serves the cache untouched.
        """
        if not self._enabled or run is None:
            return None
        async with self._lock:
            if run != self._cached_run and self._is_stale():
                await self._fetch(run)
            return self._hourly

    async def _fetch(self, run: datetime) -> None:
        """Download and cache the hourly set; swallow errors keeping last-good."""
        try:
            hourly = await self._backend.fetch_hourly(
                self._point, horizon_days=self._horizon_days
            )
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
            return
        except OgdConnectionError as err:
            _LOGGER.warning("hourly forecast fetch failed: %s", err)
            return

        async_delete_issue(self._hass, DOMAIN, _ISSUE_FORECAST_PARSE)
        self._hourly = hourly
        self._cached_run = run
        self._last_fetch = dt_util.utcnow()
        _LOGGER.debug(
            "hourly forecast fetched for run %s (%d hours cached)",
            run.isoformat(),
            len(hourly),
        )


class StationCoordinator(DataUpdateCoordinator[Observation]):
    """Poll one station's latest 10-minute observation."""

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
            name=f"{station_abbr} observations",
            update_interval=STATION_UPDATE_INTERVAL,
        )
        self._session = session
        self._station_abbr = station_abbr
        # Reused across polls so the station file is revalidated conditionally
        # (If-None-Match / If-Modified-Since); get_text mutates it in place.
        self._cache = CachedResponse(body="")
        # Timestamp of the last successful update; exposed for diagnostics.
        self.last_success: datetime | None = None

    async def _async_update_data(self) -> Observation:
        try:
            obs = await fetch_current(
                self._session, self._station_abbr, cache=self._cache
            )
        except OgdParseError as err:
            async_create_issue(
                self.hass,
                DOMAIN,
                _ISSUE_STATION_PARSE,
                is_fixable=False,
                severity=IssueSeverity.WARNING,
                translation_key="parse_error_station",
            )
            raise UpdateFailed(
                f"station {self._station_abbr} parse failed: {err}"
            ) from err
        except OgdConnectionError as err:
            raise UpdateFailed(
                f"station {self._station_abbr} update failed: {err}"
            ) from err

        async_delete_issue(self.hass, DOMAIN, _ISSUE_STATION_PARSE)
        self.last_success = dt_util.utcnow()
        return obs


class ForecastCoordinator(DataUpdateCoordinator[ForecastData]):
    """Refresh the daily local forecast, skipping unchanged runs (ADR-0002).

    Keeps the daily forecast fresh from the newest complete run and tracks the
    run stamp so the weather entity can push a lazy hourly refresh when it
    changes. The bulk hourly download itself lives in :attr:`hourly_provider`,
    off the coordinator's eager path (issue #54).
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
        # Timestamp of the run the current daily data came from; exposed for
        # diagnostics and used to skip re-downloading an unchanged run. The
        # weather entity also watches it to trigger the lazy hourly refresh.
        self.last_run: datetime | None = None
        # Timestamp of the last successful update; exposed for diagnostics.
        self.last_success: datetime | None = None
        # The lazy hourly download hangs here; the coordinator never calls it,
        # the weather entity does when HA asks (ADR-0002 revision 2, issue #54).
        self.hourly_provider = HourlyForecastProvider(
            hass,
            backend,
            point,
            enabled=hourly_enabled,
            horizon_days=hourly_horizon_days,
        )

    async def _async_update_data(self) -> ForecastData:
        try:
            run = await latest_run(
                self._session, COLLECTION_FORECAST, DAILY_REQUIRED_PARAMS
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
                # The backend downloads the small daily files and parses them
                # off the event loop; a future per-point backend swaps in here.
                daily = await self._backend.fetch_daily(self._point)
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
            self.last_run = run.timestamp

        async_delete_issue(self.hass, DOMAIN, _ISSUE_FORECAST_PARSE)
        self.last_success = dt_util.utcnow()
        return ForecastData(daily=daily)
