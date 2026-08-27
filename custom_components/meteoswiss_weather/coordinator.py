"""Data update coordinators for MeteoSwiss Weather.

Two coordinators back a config entry (ADR-0002):

- :class:`StationCoordinator` polls the configured SwissMetNet station's
  10-minute ``now`` file, revalidating it conditionally so an unchanged file
  costs a single 304.
- :class:`ForecastCoordinator` checks the newest local-forecast run once an
  hour and only downloads the (small) daily parameter files when the run
  stamp actually changed, so a quiet hour costs one small STAC request.

Everything upstream-specific lives in the pure ``ogd`` client (ADR-0001);
the coordinators only translate its :class:`OgdError` into ``UpdateFailed``
and hand CSV parsing to the executor via the backend.
"""

from __future__ import annotations

import logging
from datetime import datetime

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    FORECAST_CHECK_INTERVAL,
    STATION_UPDATE_INTERVAL,
)
from .ogd import (
    CachedResponse,
    DailyForecast,
    ForecastBackend,
    ForecastPoint,
    Observation,
    OgdError,
    fetch_current,
    latest_run,
)
from .ogd.const import COLLECTION_FORECAST, DAILY_REQUIRED_PARAMS

_LOGGER = logging.getLogger(__name__)


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

    async def _async_update_data(self) -> Observation:
        try:
            return await fetch_current(
                self._session, self._station_abbr, cache=self._cache
            )
        except OgdError as err:
            raise UpdateFailed(
                f"station {self._station_abbr} update failed: {err}"
            ) from err


class ForecastCoordinator(DataUpdateCoordinator[list[DailyForecast]]):
    """Refresh the daily local forecast, skipping unchanged runs (ADR-0002)."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: aiohttp.ClientSession,
        backend: ForecastBackend,
        point: ForecastPoint,
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
        # Timestamp of the run the current data came from; exposed for
        # diagnostics and used to skip re-downloading an unchanged run.
        self.last_run: datetime | None = None

    async def _async_update_data(self) -> list[DailyForecast]:
        try:
            run = await latest_run(
                self._session, COLLECTION_FORECAST, DAILY_REQUIRED_PARAMS
            )
        except OgdError as err:
            raise UpdateFailed(f"forecast run discovery failed: {err}") from err

        # An unchanged run means the MB-scale daily files would be identical:
        # skip the download entirely and keep serving what we have (ADR-0002).
        if (
            self.last_run is not None
            and run.timestamp == self.last_run
            and self.data is not None
        ):
            return self.data

        try:
            # The backend downloads the small daily files and parses them off
            # the event loop; a future per-point backend swaps in here unchanged.
            daily = await self._backend.fetch_daily(self._point)
        except OgdError as err:
            raise UpdateFailed(f"daily forecast fetch failed: {err}") from err

        self.last_run = run.timestamp
        return daily
