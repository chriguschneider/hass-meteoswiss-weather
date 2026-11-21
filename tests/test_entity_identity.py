"""Pin the identity of every entity of a point entry and a snow-station entry.

A refactor of the entity scaffolding must not change a unique id, entity id,
device identifier or default-enabled state, or users lose history.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.meteoswiss_weather.const import (
    CONF_POINT_ID,
    CONF_POINT_NAME,
    CONF_POINT_TYPE_ID,
    CONF_POSTAL_CODE,
    CONF_STATION_ABBR,
    CONF_STATION_NAME,
    DOMAIN,
)
from tests.test_imis_station import _snow_station_entry
from tests.test_sensor import _setup

_POINT = [
    ("2-309800_air_temp_5cm", "sensor.koniz_air_temperature_5_cm", "integration"),
    ("2-309800_data_fetched_today", "sensor.koniz_data_fetched_today", None),
    ("2-309800_dew_point", "sensor.koniz_dew_point", "integration"),
    ("2-309800_diffuse_radiation", "sensor.koniz_diffuse_radiation", "integration"),
    ("2-309800_global_radiation", "sensor.koniz_global_radiation", "integration"),
    ("2-309800_gust_speed", "sensor.koniz_wind_gust_speed", None),
    ("2-309800_humidity", "sensor.koniz_humidity", None),
    ("2-309800_longwave_radiation", "sensor.koniz_longwave_radiation", "integration"),
    ("2-309800_measurement_time", "sensor.koniz_measurement_time", "integration"),
    ("2-309800_precipitation", "sensor.koniz_precipitation_10_min", None),
    ("2-309800_precipitation_today", "sensor.koniz_precipitation_today", None),
    ("2-309800_pressure_qfe", "sensor.koniz_pressure_qfe", "integration"),
    ("2-309800_pressure_qff", "sensor.koniz_pressure_qff", None),
    ("2-309800_pressure_qnh", "sensor.koniz_pressure_qnh", "integration"),
    ("2-309800_requests_today", "sensor.koniz_requests_today", "integration"),
    ("2-309800_snow_depth", "sensor.koniz_snow_depth", "integration"),
    ("2-309800_soil_temp_10cm", "sensor.koniz_soil_temperature_10_cm", "integration"),
    ("2-309800_soil_temp_20cm", "sensor.koniz_soil_temperature_20_cm", "integration"),
    ("2-309800_soil_temp_5cm", "sensor.koniz_soil_temperature_5_cm", "integration"),
    (
        "2-309800_sunshine_duration",
        "sensor.koniz_sunshine_duration_10_min",
        "integration",
    ),
    ("2-309800_sunshine_duration_today", "sensor.koniz_sunshine_duration_today", None),
    ("2-309800_temp_max_today", "sensor.koniz_high_temperature_today", None),
    ("2-309800_temp_min_today", "sensor.koniz_low_temperature_today", None),
    ("2-309800_temperature", "sensor.koniz_temperature", None),
    ("2-309800_wind_bearing", "sensor.koniz_wind_bearing", None),
    ("2-309800_wind_chill", "sensor.koniz_wind_chill", "integration"),
    ("2-309800_wind_speed", "sensor.koniz_wind_speed", None),
    ("2-309800_zero_degree_level", "sensor.koniz_zero_degree_level", "integration"),
    (
        "2-309800_zero_degree_level_48h",
        "sensor.koniz_zero_degree_level_lowest_in_48_h",
        "integration",
    ),
    ("2-309800", "weather.koniz", None),
]

_SNOW = [
    ("imis-STH2_elevation", "sensor.vorderstocken_elevation", None),
    ("imis-STH2_gust_speed", "sensor.vorderstocken_wind_gust_speed", None),
    ("imis-STH2_humidity", "sensor.vorderstocken_humidity", None),
    ("imis-STH2_new_snow_24h", "sensor.vorderstocken_new_snow_24_h_modelled", None),
    ("imis-STH2_reflected_radiation", "sensor.vorderstocken_reflected_radiation", None),
    ("imis-STH2_snow_height", "sensor.vorderstocken_snow_height", None),
    (
        "imis-STH2_snow_height_modelled",
        "sensor.vorderstocken_snow_height_modelled",
        None,
    ),
    (
        "imis-STH2_snow_surface_temperature",
        "sensor.vorderstocken_snow_surface_temperature",
        None,
    ),
    (
        "imis-STH2_snow_temperature_0cm",
        "sensor.vorderstocken_snow_temperature_0_cm",
        None,
    ),
    (
        "imis-STH2_snow_temperature_100cm",
        "sensor.vorderstocken_snow_temperature_100_cm",
        None,
    ),
    (
        "imis-STH2_snow_temperature_25cm",
        "sensor.vorderstocken_snow_temperature_25_cm",
        None,
    ),
    (
        "imis-STH2_snow_temperature_50cm",
        "sensor.vorderstocken_snow_temperature_50_cm",
        None,
    ),
    ("imis-STH2_temperature", "sensor.vorderstocken_temperature", None),
    ("imis-STH2_wind_bearing", "sensor.vorderstocken_wind_bearing", None),
    (
        "imis-STH2_wind_bearing_sd",
        "sensor.vorderstocken_wind_bearing_standard_deviation",
        None,
    ),
    ("imis-STH2_wind_speed", "sensor.vorderstocken_wind_speed", None),
]


def _identity(hass: HomeAssistant, config_entry_id: str) -> list[tuple]:
    registry = er.async_get(hass)
    return sorted(
        (
            e.unique_id,
            e.entity_id,
            e.disabled_by.value if e.disabled_by else None,
        )
        for e in er.async_entries_for_config_entry(registry, config_entry_id)
    )


def _devices(hass: HomeAssistant, config_entry_id: str) -> set[tuple]:
    registry = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    return {
        tuple(sorted(dev_reg.async_get(e.device_id).identifiers))
        for e in er.async_entries_for_config_entry(registry, config_entry_id)
    }


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_POINT_ID: 309800,
            CONF_POINT_TYPE_ID: 2,
            CONF_POSTAL_CODE: "3098",
            CONF_POINT_NAME: "Köniz",
            CONF_STATION_ABBR: "BER",
            CONF_STATION_NAME: "Bern / Zollikofen",
        },
        title="Köniz",
        unique_id="2-309800",
    )


async def test_point_entry_identity(hass, config_entry, mock_ogd) -> None:
    """Unique ids, entity ids and default-enabled state of a point entry."""
    await _setup(hass, config_entry)
    assert _identity(hass, config_entry.entry_id) == sorted(_POINT)
    assert _devices(hass, config_entry.entry_id) == {
        (("meteoswiss_weather", "2-309800"),)
    }


async def test_snow_entry_identity(hass, mock_slf: AiohttpClientMocker) -> None:
    """Unique ids, entity ids and default-enabled state of a snow-station entry."""
    entry = _snow_station_entry("STH2")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _identity(hass, entry.entry_id) == sorted(_SNOW)
    assert _devices(hass, entry.entry_id) == {(("meteoswiss_weather", "imis-STH2"),)}
