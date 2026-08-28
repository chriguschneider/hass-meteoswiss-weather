"""Data update coordinators for MeteoSwiss Weather.

Two coordinators back a config entry (ADR-0002):

- :class:`StationCoordinator` polls the configured SwissMetNet station's
  10-minute ``now`` file, revalidating it conditionally so an unchanged file
  costs a single 304.
- :class:`ForecastCoordinator` checks the newest local-forecast run once an
  hour and only downloads the (small) daily parameter files when the run
  stamp actually changed, so a quiet hour costs one small STAC request. When
  the hourly-forecast option is on it also fetches the bulk hourly files, but
  never more often than ``HOURLY_FORECAST_MIN_INTERVAL`` (ADR-0002).

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
    """The forecast coordinator's payload: the daily forecast and, when the
    hourly option is on, the hourly forecast. ``hourly`` is ``None`` while the
    option is off or before the first hourly fetch has completed.
    """

    daily: list[DailyForecast]
    hourly: list[HourlyForecast] | None = None


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
    """Refresh the local forecast, skipping unchanged runs (ADR-0002).

    Always keeps the daily forecast fresh from the newest complete run. When
    ``hourly_enabled`` is set it also fetches the bulk hourly files, but never
    more often than :data:`HOURLY_FORECAST_MIN_INTERVAL` — the coordinator
    still checks the run stamp hourly, yet the ~180 MB hourly download is
    throttled regardless of how often a new run appears.
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
        self._hourly_enabled = hourly_enabled
        self._hourly_horizon_days = hourly_horizon_days
        # Timestamp of the run the current daily data came from; exposed for
        # diagnostics and used to skip re-downloading an unchanged run.
        self.last_run: datetime | None = None
        # When the last hourly download completed; ``None`` until the first one.
        # Drives the 3 h throttle (ADR-0002); reset by the entry reload an
        # option change triggers, so enabling hourly fetches promptly.
        self._last_hourly_fetch: datetime | None = None
        # Timestamp of the last successful update; exposed for diagnostics.
        self.last_success: datetime | None = None

    def _should_fetch_hourly(self) -> bool:
        """Whether enough time has passed to fetch the hourly files again."""
        if not self._hourly_enabled:
            return False
        if self._last_hourly_fetch is None:
            return True
        elapsed = dt_util.utcnow() - self._last_hourly_fetch
        return elapsed >= HOURLY_FORECAST_MIN_INTERVAL

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

        # Carry the previous hourly forecast forward while the throttle holds
        # or the option is off; only download when both allow it.
        hourly = self.data.hourly if self.data is not None else None
        if self._should_fetch_hourly():
            try:
                hourly = await self._backend.fetch_hourly(
                    self._point, horizon_days=self._hourly_horizon_days
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
                raise UpdateFailed(f"hourly forecast parse failed: {err}") from err
            except OgdConnectionError as err:
                raise UpdateFailed(f"hourly forecast fetch failed: {err}") from err
            self._last_hourly_fetch = dt_util.utcnow()

        async_delete_issue(self.hass, DOMAIN, _ISSUE_FORECAST_PARSE)
        self.last_success = dt_util.utcnow()
        return ForecastData(daily=daily, hourly=hourly)
