"""Sensor platform: SwissMetNet station observations (issue #13) and
today's forecast aggregates from the daily forecast (issue #48).

Station sensors are backed by StationCoordinator; forecast sensors are backed
by ForecastCoordinator and also recompute at local midnight so the "today"
values flip to the new day without waiting for the next forecast fetch.
Every entity shares the same device as the weather entity (same identifier
keyed on the forecast point, not the station abbreviation).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    DEGREE,
    PERCENTAGE,
    EntityCategory,
    UnitOfIrradiance,
    UnitOfLength,
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import MeteoSwissConfigEntry
from .const import ATTRIBUTION, DOMAIN
from .coordinator import ForecastCoordinator, PollenCoordinator, StationCoordinator
from .ogd import DailyForecast, HourlyForecast, Observation, PollenObservation


@dataclass(frozen=True, slots=True)
class MeteoSwissSensorDescription(SensorEntityDescription):
    """SensorEntityDescription extended with the Observation attribute name."""

    observation_key: str = ""
    # 10-minute parameter code in the station CSV (e.g. ``"tre200s0"``).
    # Checked against the data-inventory set to skip sensors the station
    # does not carry (issue #46).
    parameter_code: str = ""


# Sensors are ordered most-to-least useful; rarely-used ones are disabled by
# default so the entity registry is not cluttered for the common case.
_SENSORS: tuple[MeteoSwissSensorDescription, ...] = (
    MeteoSwissSensorDescription(
        key="temperature",
        translation_key="temperature",
        observation_key="temperature",
        parameter_code="tre200s0",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    MeteoSwissSensorDescription(
        key="humidity",
        translation_key="humidity",
        observation_key="humidity",
        parameter_code="ure200s0",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    MeteoSwissSensorDescription(
        key="pressure_qff",
        translation_key="pressure_qff",
        observation_key="pressure_qff",
        parameter_code="pp0qffs0",
        device_class=SensorDeviceClass.ATMOSPHERIC_PRESSURE,
        native_unit_of_measurement=UnitOfPressure.HPA,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    MeteoSwissSensorDescription(
        key="wind_speed",
        translation_key="wind_speed",
        observation_key="wind_speed_kmh",
        parameter_code="fu3010z0",
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    MeteoSwissSensorDescription(
        key="wind_bearing",
        translation_key="wind_bearing",
        observation_key="wind_bearing",
        parameter_code="dkl010z0",
        native_unit_of_measurement=DEGREE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    MeteoSwissSensorDescription(
        key="gust_speed",
        translation_key="gust_speed",
        observation_key="gust_kmh",
        parameter_code="fu3010z1",
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    MeteoSwissSensorDescription(
        key="precipitation",
        translation_key="precipitation",
        observation_key="precipitation_10min",
        parameter_code="rre150z0",
        device_class=SensorDeviceClass.PRECIPITATION,
        native_unit_of_measurement=UnitOfPrecipitationDepth.MILLIMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    # Rarely used sensors: disabled in the entity registry by default.
    MeteoSwissSensorDescription(
        key="dew_point",
        translation_key="dew_point",
        observation_key="dew_point",
        parameter_code="tde200s0",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
    ),
    MeteoSwissSensorDescription(
        key="pressure_qfe",
        translation_key="pressure_qfe",
        observation_key="pressure_qfe",
        parameter_code="prestas0",
        device_class=SensorDeviceClass.ATMOSPHERIC_PRESSURE,
        native_unit_of_measurement=UnitOfPressure.HPA,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    MeteoSwissSensorDescription(
        key="sunshine_duration",
        translation_key="sunshine_duration",
        observation_key="sunshine_10min",
        parameter_code="sre000z0",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
    ),
    MeteoSwissSensorDescription(
        key="global_radiation",
        translation_key="global_radiation",
        observation_key="global_radiation",
        parameter_code="gre000z0",
        device_class=SensorDeviceClass.IRRADIANCE,
        native_unit_of_measurement=UnitOfIrradiance.WATTS_PER_SQUARE_METER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
    ),
    # B1 — snow depth
    MeteoSwissSensorDescription(
        key="snow_depth",
        translation_key="snow_depth",
        observation_key="snow_depth",
        parameter_code="htoauts0",
        native_unit_of_measurement=UnitOfLength.CENTIMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:snowflake",
        entity_registry_enabled_default=False,
    ),
    # B2 — wind chill and QNH pressure
    MeteoSwissSensorDescription(
        key="wind_chill",
        translation_key="wind_chill",
        observation_key="wind_chill",
        parameter_code="xchills0",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
    ),
    MeteoSwissSensorDescription(
        key="pressure_qnh",
        translation_key="pressure_qnh",
        observation_key="pressure_qnh",
        parameter_code="pp0qnhs0",
        device_class=SensorDeviceClass.ATMOSPHERIC_PRESSURE,
        native_unit_of_measurement=UnitOfPressure.HPA,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
    ),
    # B3 — soil temperatures
    MeteoSwissSensorDescription(
        key="soil_temp_5cm",
        translation_key="soil_temp_5cm",
        observation_key="soil_temp_5cm",
        parameter_code="tso005s0",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
    ),
    MeteoSwissSensorDescription(
        key="soil_temp_10cm",
        translation_key="soil_temp_10cm",
        observation_key="soil_temp_10cm",
        parameter_code="tso010s0",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
    ),
    MeteoSwissSensorDescription(
        key="soil_temp_20cm",
        translation_key="soil_temp_20cm",
        observation_key="soil_temp_20cm",
        parameter_code="tso020s0",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
    ),
    # B4 — 5 cm air temperature (ground-frost material)
    MeteoSwissSensorDescription(
        key="air_temp_5cm",
        translation_key="air_temp_5cm",
        observation_key="air_temp_5cm",
        parameter_code="tre005s0",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
    ),
    # B5 — diffuse and incoming long-wave radiation
    MeteoSwissSensorDescription(
        key="diffuse_radiation",
        translation_key="diffuse_radiation",
        observation_key="diffuse_radiation",
        parameter_code="ods000z0",
        device_class=SensorDeviceClass.IRRADIANCE,
        native_unit_of_measurement=UnitOfIrradiance.WATTS_PER_SQUARE_METER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
    ),
    MeteoSwissSensorDescription(
        key="longwave_radiation",
        translation_key="longwave_radiation",
        observation_key="longwave_radiation",
        parameter_code="oli000z0",
        device_class=SensorDeviceClass.IRRADIANCE,
        native_unit_of_measurement=UnitOfIrradiance.WATTS_PER_SQUARE_METER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
    ),
)


@dataclass(frozen=True, slots=True)
class ForecastSensorDescription(SensorEntityDescription):
    """SensorEntityDescription for a sensor derived from today's daily forecast row."""

    forecast_key: str = ""
    # Attribute name on :class:`~ogd.DailyForecast` that carries this value.


_FORECAST_SENSORS: tuple[ForecastSensorDescription, ...] = (
    # B6 — today's aggregates from the daily forecast (issue #48).
    # Enabled by default: they are the primary automation/dashboard value.
    ForecastSensorDescription(
        key="temp_max_today",
        translation_key="temp_max_today",
        forecast_key="temp_max",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    ForecastSensorDescription(
        key="temp_min_today",
        translation_key="temp_min_today",
        forecast_key="temp_min",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    ForecastSensorDescription(
        key="precipitation_today",
        translation_key="precipitation_today",
        forecast_key="precipitation",
        device_class=SensorDeviceClass.PRECIPITATION,
        native_unit_of_measurement=UnitOfPrecipitationDepth.MILLIMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
)

# B8 — zero-degree level sensor (issue #55): the current hour's zero-degree
# level from the hourly forecast cache. Requires hourly opt-in; shows
# ``unknown`` when hourly data has not yet been fetched.
_ZERO_DEGREE_DESCRIPTION = SensorEntityDescription(
    key="zero_degree_level",
    translation_key="zero_degree_level",
    native_unit_of_measurement=UnitOfLength.METERS,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=0,
    icon="mdi:snowflake-thermometer",
    entity_registry_enabled_default=False,
)


# Pollen sensors (ADR-0005): one per taxon the station measures. Taxon codes
# match the column names in the upstream ``_h_now.csv`` file header. Grasses
# and birch are the most relevant allergens and are enabled by default; the
# remaining taxa are disabled so the entity registry stays uncluttered.
@dataclass(frozen=True, slots=True)
class PollenSensorDescription(SensorEntityDescription):
    """SensorEntityDescription extended with the pollen taxon code."""

    taxon_code: str = ""


# Unit used by the upstream MeteoSwiss pollen dataset (docs/ogd.md §Pollen).
_POLLEN_UNIT = "grains/m³"

_POLLEN_SENSORS: tuple[PollenSensorDescription, ...] = (
    PollenSensorDescription(
        key="pollen_grasses",
        translation_key="pollen_grasses",
        taxon_code="khpoach0",
        native_unit_of_measurement=_POLLEN_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:flower-pollen",
    ),
    PollenSensorDescription(
        key="pollen_birch",
        translation_key="pollen_birch",
        taxon_code="kabetuh0",
        native_unit_of_measurement=_POLLEN_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:flower-pollen",
    ),
    PollenSensorDescription(
        key="pollen_alder",
        translation_key="pollen_alder",
        taxon_code="kaalnuh0",
        native_unit_of_measurement=_POLLEN_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:flower-pollen",
        entity_registry_enabled_default=False,
    ),
    PollenSensorDescription(
        key="pollen_hazel",
        translation_key="pollen_hazel",
        taxon_code="kacoryh0",
        native_unit_of_measurement=_POLLEN_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:flower-pollen",
        entity_registry_enabled_default=False,
    ),
    PollenSensorDescription(
        key="pollen_beech",
        translation_key="pollen_beech",
        taxon_code="kafaguh0",
        native_unit_of_measurement=_POLLEN_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:flower-pollen",
        entity_registry_enabled_default=False,
    ),
    PollenSensorDescription(
        key="pollen_ash",
        translation_key="pollen_ash",
        taxon_code="kafraxh0",
        native_unit_of_measurement=_POLLEN_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:flower-pollen",
        entity_registry_enabled_default=False,
    ),
    PollenSensorDescription(
        key="pollen_oak",
        translation_key="pollen_oak",
        taxon_code="kaquerh0",
        native_unit_of_measurement=_POLLEN_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:flower-pollen",
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MeteoSwissConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up station sensor entities for a config entry."""
    runtime = entry.runtime_data
    point = runtime.point
    device_unique_id = f"{point.point_type_id}-{point.point_id}"
    device_info = DeviceInfo(
        identifiers={(DOMAIN, device_unique_id)},
        name=point.name,
        manufacturer="MeteoSwiss",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://opendatadocs.meteoswiss.ch",
    )

    station_params = runtime.station_parameters
    precip_coordinator = runtime.precip_coordinator
    # None means inventory was unavailable; create all sensors as a safe fallback.
    # The precipitation sensor is handled separately below: when an optional
    # precipitation station is configured (ADR-0006), it reads from that station
    # (which always carries rre150z0) rather than from the main station and its
    # inventory, so it is excluded from the main-station set here.
    supported = [
        desc for desc in _SENSORS
        if (station_params is None or desc.parameter_code in station_params)
        and not (precip_coordinator is not None and desc.key == "precipitation")
    ]

    # Determine which pollen sensors to create (only when coordinator exists).
    pollen_params = runtime.pollen_inventory
    supported_pollen: list[PollenSensorDescription] = []
    if runtime.pollen_coordinator is not None:
        supported_pollen = [
            desc for desc in _POLLEN_SENSORS
            if pollen_params is None or desc.taxon_code in pollen_params
        ]

    # Remove entity registry entries for sensors the station no longer carries.
    # Forecast and pollen sensor unique IDs are included in the valid set so
    # they are never incorrectly removed by the station-inventory cleanup.
    if station_params is not None:
        valid_unique_ids = (
            {f"{device_unique_id}_{desc.key}" for desc in supported}
            | {f"{device_unique_id}_{desc.key}" for desc in _FORECAST_SENSORS}
            | {f"{device_unique_id}_{_ZERO_DEGREE_DESCRIPTION.key}"}
            | {f"{device_unique_id}_{desc.key}" for desc in _POLLEN_SENSORS}
        )
        # The precipitation sensor is created from the precip station below and
        # is excluded from ``supported``, so keep its id in the valid set.
        if precip_coordinator is not None:
            valid_unique_ids.add(f"{device_unique_id}_precipitation")
        sensor_uid_prefix = f"{device_unique_id}_"
        entity_reg = er.async_get(hass)
        for entity_entry in er.async_entries_for_config_entry(
            entity_reg, entry.entry_id
        ):
            if (
                entity_entry.unique_id.startswith(sensor_uid_prefix)
                and entity_entry.unique_id not in valid_unique_ids
            ):
                entity_reg.async_remove(entity_entry.entity_id)

    async_add_entities(
        MeteoSwissSensor(
            runtime.station_coordinator, description, device_unique_id, device_info
        )
        for description in supported
    )
    # Precipitation from the optional precipitation station (ADR-0006, #70): its
    # attribution and a ``station`` attribute name the station it reads.
    if precip_coordinator is not None:
        precip_description = next(
            desc for desc in _SENSORS if desc.key == "precipitation"
        )
        async_add_entities(
            [
                MeteoSwissSensor(
                    precip_coordinator,
                    precip_description,
                    device_unique_id,
                    device_info,
                    station_name=runtime.precip_station_name,
                )
            ]
        )
    async_add_entities(
        ForecastSensor(
            runtime.forecast_coordinator, description, device_unique_id, device_info
        )
        for description in _FORECAST_SENSORS
    )
    async_add_entities([
        ZeroDegreeSensor(
            runtime.forecast_coordinator, _ZERO_DEGREE_DESCRIPTION,
            device_unique_id, device_info,
        )
    ])
    if supported_pollen:
        async_add_entities(
            PollenSensor(
                runtime.pollen_coordinator,  # type: ignore[arg-type]
                description,
                device_unique_id,
                device_info,
            )
            for description in supported_pollen
        )


class MeteoSwissSensor(CoordinatorEntity[StationCoordinator], SensorEntity):
    """One observation field from the configured SwissMetNet station.

    When ``station_name`` is given the sensor reads from a different station
    than the entry's main one (the optional precipitation station, ADR-0006):
    the attribution then names that station and it is exposed as a ``station``
    state attribute, so a dashboard shows where the value comes from.
    """

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: StationCoordinator,
        description: MeteoSwissSensorDescription,
        device_unique_id: str,
        device_info: DeviceInfo,
        *,
        station_name: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._observation_key = description.observation_key
        self._attr_unique_id = f"{device_unique_id}_{description.key}"
        self._attr_device_info = device_info
        if station_name:
            self._attr_attribution = f"{ATTRIBUTION} ({station_name})"
            self._attr_extra_state_attributes = {"station": station_name}

    @property
    def native_value(self) -> float | None:
        """Return the sensor value, or ``None`` (→ ``unknown``) when missing."""
        obs: Observation | None = self.coordinator.data
        if obs is None:
            return None
        return getattr(obs, self._observation_key, None)


class ForecastSensor(CoordinatorEntity[ForecastCoordinator], SensorEntity):
    """A sensor whose value comes from today's row of the daily forecast.

    Updates on every forecast coordinator refresh and also at local midnight
    so the "today" values flip to the new day without waiting for the next
    fetch (issue #48).
    """

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: ForecastCoordinator,
        description: ForecastSensorDescription,
        device_unique_id: str,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._forecast_key: str = description.forecast_key
        self._attr_unique_id = f"{device_unique_id}_{description.key}"
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """Subscribe to local midnight so today's date rolls over promptly."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_change(
                self.hass,
                self._handle_day_rollover,
                hour=0,
                minute=0,
                second=0,
            )
        )

    @callback
    def _handle_day_rollover(self, _now: date) -> None:
        """Re-write state when local midnight flips the calendar date."""
        self.async_write_ha_state()

    def _today_row(self) -> DailyForecast | None:
        """Return the daily forecast entry whose date matches today, or ``None``."""
        data = self.coordinator.data
        if data is None or not data.daily:
            return None
        today = dt_util.now().date()
        for day in data.daily:
            if day.date == today:
                return day
        return None

    @property
    def native_value(self) -> float | None:
        """Return today's value, or ``None`` (→ ``unknown``) when absent."""
        row = self._today_row()
        if row is None:
            return None
        return getattr(row, self._forecast_key, None)


class PollenSensor(CoordinatorEntity[PollenCoordinator], SensorEntity):
    """One hourly pollen concentration from the configured pollen station.

    One entity per taxon the station actually measures (ADR-0005, issue #67).
    Grasses and birch are enabled by default; the remaining taxa are disabled.
    Shows ``unavailable`` when the coordinator's last update failed (e.g. the
    pollen station file had no complete measurement rows).
    """

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: PollenCoordinator,
        description: PollenSensorDescription,
        device_unique_id: str,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._taxon_code = description.taxon_code
        self._attr_unique_id = f"{device_unique_id}_{description.key}"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> float | None:
        """Return the latest hourly concentration, or ``None`` when missing."""
        obs: PollenObservation | None = self.coordinator.data
        if obs is None:
            return None
        return obs.values.get(self._taxon_code)


class ZeroDegreeSensor(CoordinatorEntity[ForecastCoordinator], SensorEntity):
    """Current hour's zero-degree level from the hourly forecast cache (B8, issue #55).

    Requires the hourly opt-in: shows ``unknown`` (``None``) until the first
    hourly fetch completes. Once populated the value is the zero-degree level
    (m) for the current UTC hour from the ``zprfr0hs`` point-major block.
    Updates when the forecast coordinator fires (every hour), at which point
    the hourly provider may have refreshed the cache from a new run.
    """

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: ForecastCoordinator,
        description: SensorEntityDescription,
        device_unique_id: str,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{device_unique_id}_{description.key}"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> float | None:
        """Return the current hour's zero-degree level, or ``None`` when absent."""
        hourly: list[HourlyForecast] | None = (
            self.coordinator.hourly_provider.cached_hourly
        )
        if not hourly:
            return None
        this_hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        for hour in hourly:
            if hour.time == this_hour:
                return hour.zero_degree_level
        return None
