"""MeteoSwiss Weather: Home Assistant integration on the official Open Data platform.

Sets up the two data coordinators (ADR-0002) and stores them, together with
the resolved forecast point, station and forecast backend, on
``entry.runtime_data``. The weather platform consumes that handle; the sensor
platform lands in its own issue.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import ConfigEntrySelector
from homeassistant.util import dt as dt_util

from .const import (
    CONF_HOURLY_CLOUD_LAYERS,
    CONF_HOURLY_FORECAST,
    CONF_HOURLY_HORIZON_DAYS,
    CONF_HOURLY_TEMP_PERCENTILES,
    CONF_POINT_ID,
    CONF_POINT_NAME,
    CONF_POINT_TYPE_ID,
    CONF_POLLEN,
    CONF_POLLEN_STATION,
    CONF_POSTAL_CODE,
    CONF_PRECIP_STATION_ABBR,
    CONF_PRECIP_STATION_NAME,
    CONF_STATION_ABBR,
    DEFAULT_HOURLY_HORIZON_DAYS,
    DOMAIN,
)
from .coordinator import (
    ForecastCoordinator,
    PollenCoordinator,
    PrecipStationCoordinator,
    StationCoordinator,
)
from .history import async_backfill
from .ogd import (
    BulkCsvBackend,
    ForecastBackend,
    ForecastPoint,
    OgdError,
    fetch_datainventory,
    fetch_pollen_datainventory,
)

# ``import_history`` service (B12b, ADR-0007): a one-off, user-triggered import
# of a station's official hourly history into long-term statistics. Nothing polls
# the history files — this is the only recurring-budget exception, and it only
# runs when the user calls it.
SERVICE_IMPORT_HISTORY = "import_history"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_START = "start"
ATTR_END = "end"

_IMPORT_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): ConfigEntrySelector(
            {"integration": DOMAIN}
        ),
        vol.Optional(ATTR_START): cv.datetime,
        vol.Optional(ATTR_END): cv.datetime,
    }
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
    # Pollen coordinator; ``None`` when the pollen option is off (ADR-0005).
    pollen_coordinator: PollenCoordinator | None
    # Taxon codes the pollen station actually measures (from the inventory).
    # ``None`` means inventory was unavailable or pollen is off; sensor.py
    # creates all known sensors as a fallback when pollen is on.
    pollen_inventory: frozenset[str] | None
    # Optional precipitation-only station (ADR-0006, #70): ``None`` unless the
    # user picked one. When set, the precipitation sensor and the weather
    # entity's current precipitation read from it instead of the main station.
    precip_coordinator: PrecipStationCoordinator | None
    precip_station_name: str


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
    # B9/B11 gated date-major additions (issue #69). Only meaningful with the
    # hourly opt-in on; each turns on extra expensive files, so both default off.
    hourly_cloud_layers = hourly_enabled and bool(
        entry.options.get(CONF_HOURLY_CLOUD_LAYERS, False)
    )
    hourly_temp_percentiles = hourly_enabled and bool(
        entry.options.get(CONF_HOURLY_TEMP_PERCENTILES, False)
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
        hourly_cloud_layers=hourly_cloud_layers,
        hourly_temp_percentiles=hourly_temp_percentiles,
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

    # Pollen coordinator (ADR-0005): only when the option is on.
    # The first refresh is non-fatal — pollen is opt-in and an unavailable
    # pollen station should not block the whole entry from loading.
    pollen_coordinator: PollenCoordinator | None = None
    pollen_inventory: frozenset[str] | None = None
    if entry.options.get(CONF_POLLEN):
        pollen_abbr = str(entry.options.get(CONF_POLLEN_STATION, ""))
        if pollen_abbr:
            pollen_coordinator = PollenCoordinator(hass, entry, session, pollen_abbr)
            await pollen_coordinator.async_refresh()
            try:
                pollen_inv_all = await fetch_pollen_datainventory(session)
                pollen_inventory = pollen_inv_all.get(pollen_abbr.upper())
            except OgdError:
                pass

    # Optional precipitation-only station (ADR-0006, #70): a second 10-minute
    # conditional poll, only when the user picked one. Non-fatal like pollen —
    # an unavailable opt-in station must not block the whole entry from loading.
    precip_coordinator: PrecipStationCoordinator | None = None
    precip_abbr = str(entry.data.get(CONF_PRECIP_STATION_ABBR, ""))
    precip_name = str(entry.data.get(CONF_PRECIP_STATION_NAME, ""))
    if precip_abbr:
        precip_coordinator = PrecipStationCoordinator(
            hass, entry, session, precip_abbr
        )
        await precip_coordinator.async_refresh()

    entry.runtime_data = MeteoSwissRuntimeData(
        station_coordinator=station_coordinator,
        forecast_coordinator=forecast_coordinator,
        point=point,
        station_abbr=station_abbr,
        backend=backend,
        station_parameters=station_parameters,
        pollen_coordinator=pollen_coordinator,
        pollen_inventory=pollen_inventory,
        precip_coordinator=precip_coordinator,
        precip_station_name=precip_name or precip_abbr,
    )

    # An options change (the hourly-forecast toggle) reloads the entry so the
    # coordinator and the weather entity pick up the new feature set (ADR-0002).
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    _async_register_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register the domain-level ``import_history`` service once."""
    if hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORY):
        return

    async def _async_import_history(call: ServiceCall) -> None:
        """Import a station's official hourly history into long-term statistics.

        Defaults to the current year when no range is given (ADR-0007). Reports
        the outcome as a persistent notification; a fetch/parse error names the
        offending file. A one-off traffic exception outside the ADR-0002 budget.
        """
        entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(
                f"No MeteoSwiss Weather config entry with id {entry_id}"
            )

        now = dt_util.utcnow()
        # Default range is the current year; a naive user input is read as local
        # time (Home Assistant's convention) and converted to UTC to match the
        # history timestamps.
        start_in = call.data.get(ATTR_START)
        end_in = call.data.get(ATTR_END)
        start = (
            dt_util.as_utc(start_in)
            if start_in
            else datetime(now.year, 1, 1, tzinfo=UTC)
        )
        end = dt_util.as_utc(end_in) if end_in else now
        if end < start:
            raise ServiceValidationError("'end' must not be before 'start'")

        station_abbr = str(entry.data[CONF_STATION_ABBR])
        try:
            result = await async_backfill(hass, entry, station_abbr, start, end)
        except (OgdError, HomeAssistantError) as err:
            _async_notify(
                hass,
                f"Import for {entry.title} failed: {err}",
            )
            raise HomeAssistantError(str(err)) from err

        _async_notify(
            hass,
            f"Imported {result.rows} hourly history rows for {entry.title} "
            f"({station_abbr}) across {result.series} statistics, "
            f"{start.date().isoformat()} to {end.date().isoformat()}.",
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_HISTORY,
        _async_import_history,
        schema=_IMPORT_HISTORY_SCHEMA,
    )


@callback
def _async_notify(hass: HomeAssistant, message: str) -> None:
    """Post the import outcome as a persistent notification."""
    from homeassistant.components import persistent_notification

    persistent_notification.async_create(
        hass,
        message,
        title="MeteoSwiss Weather history import",
        notification_id=f"{DOMAIN}_import_history",
    )


async def _async_reload_entry(
    hass: HomeAssistant, entry: MeteoSwissConfigEntry
) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: MeteoSwissConfigEntry
) -> bool:
    """Unload a config entry, dropping the service once the last entry goes."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        remaining = [
            other
            for other in hass.config_entries.async_entries(DOMAIN)
            if other.entry_id != entry.entry_id
        ]
        if not remaining and hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORY):
            hass.services.async_remove(DOMAIN, SERVICE_IMPORT_HISTORY)
    return unloaded
