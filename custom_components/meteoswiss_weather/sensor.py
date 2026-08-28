"""Sensor platform: SwissMetNet station observations (issue #13).

One SensorEntity per measured field from the latest 10-minute observation,
all backed by the StationCoordinator from runtime_data. Every entity shares
the same device as the weather entity (same identifier keyed on the forecast
point, not the station abbreviation).
"""

from __future__ import annotations

from dataclasses import dataclass

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
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MeteoSwissConfigEntry
from .const import ATTRIBUTION, DOMAIN
from .coordinator import StationCoordinator
from .ogd import Observation


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
    # None means inventory was unavailable; create all sensors as a safe fallback.
    supported = [
        desc for desc in _SENSORS
        if station_params is None or desc.parameter_code in station_params
    ]

    # Remove entity registry entries for sensors the station no longer carries.
    if station_params is not None:
        valid_unique_ids = {f"{device_unique_id}_{desc.key}" for desc in supported}
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


class MeteoSwissSensor(CoordinatorEntity[StationCoordinator], SensorEntity):
    """One observation field from the configured SwissMetNet station."""

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: StationCoordinator,
        description: MeteoSwissSensorDescription,
        device_unique_id: str,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._observation_key = description.observation_key
        self._attr_unique_id = f"{device_unique_id}_{description.key}"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> float | None:
        """Return the sensor value, or ``None`` (→ ``unknown``) when missing."""
        obs: Observation | None = self.coordinator.data
        if obs is None:
            return None
        return getattr(obs, self._observation_key, None)
