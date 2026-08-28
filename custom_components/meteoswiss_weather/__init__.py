"""MeteoSwiss Weather: Home Assistant integration on the official Open Data platform.

Sets up the two data coordinators (ADR-0002) and stores them, together with
the resolved forecast point, station and forecast backend, on
``entry.runtime_data``. The weather platform consumes that handle; the sensor
platform lands in its own issue.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_HOURLY_FORECAST,
    CONF_HOURLY_HORIZON_DAYS,
    CONF_POINT_ID,
    CONF_POINT_NAME,
    CONF_POINT_TYPE_ID,
    CONF_POSTAL_CODE,
    CONF_STATION_ABBR,
    DEFAULT_HOURLY_HORIZON_DAYS,
)
from .coordinator import ForecastCoordinator, StationCoordinator
from .ogd import (
    BulkCsvBackend,
    ForecastBackend,
    ForecastPoint,
    OgdError,
    fetch_datainventory,
)

# Seam for tests: replace with a FakeBackend to run without network I/O.
# A future OGC Features backend (announced by MeteoSwiss for end-2026) will
# land here as well, keeping the swap out of the coordinator and entities.
_backend_factory: Callable[[aiohttp.ClientSession], ForecastBackend] = BulkCsvBackend

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.WEATHER]


@dataclass
class MeteoSwissRuntimeData:
    """Everything a platform needs, hung off ``entry.runtime_data``."""

    station_coordinator: StationCoordinator
    forecast_coordinator: ForecastCoordinator
    point: ForecastPoint
    station_abbr: str
    backend: ForecastBackend
    # Parameter codes the chosen station actually measures, from the data
    # inventory (issue #46). ``None`` means the inventory was unavailable;
    # sensor.py falls back to creating all sensors in that case.
    station_parameters: frozenset[str] | None


type MeteoSwissConfigEntry = ConfigEntry[MeteoSwissRuntimeData]


def _point_from_entry(entry: ConfigEntry) -> ForecastPoint:
    """Rebuild the forecast point from the entry (no network at startup).

    The config flow already resolved the point; the daily parser keys on
    ``(point_id, point_type_id)`` only (docs/ogd.md §E4), so the coordinates
    are not needed at runtime and are not stored in the entry. Rebuilding
    here keeps setup independent of upstream availability.
    """
    data = entry.data
    return ForecastPoint(
        point_id=int(data[CONF_POINT_ID]),
        point_type_id=int(data[CONF_POINT_TYPE_ID]),
        postal_code=str(data.get(CONF_POSTAL_CODE, "")),
        name=str(data.get(CONF_POINT_NAME, "")),
        lat=0.0,
        lon=0.0,
        height_masl=None,
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: MeteoSwissConfigEntry
) -> bool:
    """Set up a config entry: build the coordinators and do the first refresh."""
    session = async_get_clientsession(hass)
    point = _point_from_entry(entry)
    station_abbr = str(entry.data[CONF_STATION_ABBR])
    backend: ForecastBackend = _backend_factory(session)
    hourly_enabled = bool(entry.options.get(CONF_HOURLY_FORECAST, False))
    hourly_horizon_days = int(
        entry.options.get(CONF_HOURLY_HORIZON_DAYS, DEFAULT_HOURLY_HORIZON_DAYS)
    )

    station_coordinator = StationCoordinator(hass, entry, session, station_abbr)
    forecast_coordinator = ForecastCoordinator(
        hass,
        entry,
        session,
        backend,
        point,
        hourly_enabled=hourly_enabled,
        hourly_horizon_days=hourly_horizon_days,
    )

    # A first-refresh failure raises ConfigEntryNotReady so HA retries setup.
    await station_coordinator.async_config_entry_first_refresh()
    await forecast_coordinator.async_config_entry_first_refresh()

    # Fetch the data inventory to know which parameters the station measures.
    # Non-fatal: if unavailable, sensor.py creates all sensors as a safe fallback.
    station_parameters: frozenset[str] | None = None
    try:
        inventory = await fetch_datainventory(session)
        station_parameters = inventory.get(station_abbr.upper())
    except OgdError:
        pass

    entry.runtime_data = MeteoSwissRuntimeData(
        station_coordinator=station_coordinator,
        forecast_coordinator=forecast_coordinator,
        point=point,
        station_abbr=station_abbr,
        backend=backend,
        station_parameters=station_parameters,
    )

    # An options change (the hourly-forecast toggle) reloads the entry so the
    # coordinator and the weather entity pick up the new feature set (ADR-0002).
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload_entry(
    hass: HomeAssistant, entry: MeteoSwissConfigEntry
) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: MeteoSwissConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
