"""Tests for the sensor platform (issue #13).

Spins up an in-process Home Assistant with the ``mock_ogd`` fixture so the
entities are built from the trimmed real fixtures; no test hits the network.
Covers: entity values from the BER station fixture, ``None`` field → state
``unknown``, device shared with the weather entity, disabled-by-default flags,
and entity_category for the QFE sensor.
"""

from __future__ import annotations

import pytest
from homeassistant.const import STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
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
from custom_components.meteoswiss_weather.ogd import Observation
from custom_components.meteoswiss_weather.sensor import _SENSORS

_STATION_ABBR = "BER"


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A config entry shaped exactly like the config flow produces."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_POINT_ID: 309800,
            CONF_POINT_TYPE_ID: 2,
            CONF_POSTAL_CODE: "3098",
            CONF_POINT_NAME: "Köniz",
            CONF_STATION_ABBR: _STATION_ABBR,
            CONF_STATION_NAME: "Bern / Zollikofen",
        },
        title="Köniz",
        unique_id="2-309800",
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add the entry and run setup to completion."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Helper to fetch state of a sensor by key suffix
# ---------------------------------------------------------------------------

def _state(hass: HomeAssistant, key: str) -> str:
    """Return the state string of the sensor whose entity_id ends with ``key``."""
    entity_id = f"sensor.koniz_{key}"
    state = hass.states.get(entity_id)
    assert state is not None, f"entity {entity_id!r} not found"
    return state.state


def _attr(hass: HomeAssistant, key: str, attr: str):
    """Return an attribute of the sensor whose entity_id ends with ``key``."""
    entity_id = f"sensor.koniz_{key}"
    state = hass.states.get(entity_id)
    assert state is not None, f"entity {entity_id!r} not found"
    return state.attributes.get(attr)


# ---------------------------------------------------------------------------
# Entity values from the BER fixture (last valid row at 00:40)
# ---------------------------------------------------------------------------

async def test_temperature(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    await _setup(hass, config_entry)
    assert _state(hass, "temperature") == "19.5"


async def test_humidity(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    await _setup(hass, config_entry)
    assert _state(hass, "humidity") == "88.0"


async def test_pressure_qff(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    await _setup(hass, config_entry)
    assert _state(hass, "pressure_qff") == "1014.8"


async def test_wind_speed(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    await _setup(hass, config_entry)
    assert _state(hass, "wind_speed") == "4.0"


async def test_wind_bearing(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    await _setup(hass, config_entry)
    assert _state(hass, "wind_bearing") == "245.0"


async def test_gust_speed(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    await _setup(hass, config_entry)
    # Entity id uses the translated name "Wind gust speed".
    assert _state(hass, "wind_gust_speed") == "5.5"


async def test_precipitation(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    await _setup(hass, config_entry)
    # Entity id uses the translated name "Precipitation (10 min)".
    assert _state(hass, "precipitation_10_min") == "0.2"


async def test_dew_point(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """Dew point entity exists and is disabled by default."""
    await _setup(hass, config_entry)
    entity_reg = er.async_get(hass)
    entry = entity_reg.async_get("sensor.koniz_dew_point")
    assert entry is not None
    assert entry.disabled_by is not None  # disabled by default


async def test_pressure_qfe_is_diagnostic(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """QFE sensor carries entity_category=DIAGNOSTIC."""
    await _setup(hass, config_entry)
    entity_reg = er.async_get(hass)
    entry = entity_reg.async_get("sensor.koniz_pressure_qfe")
    assert entry is not None
    assert entry.entity_category == EntityCategory.DIAGNOSTIC


async def test_sunshine_duration(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """Sunshine duration sensor exists and is disabled by default."""
    await _setup(hass, config_entry)
    entity_reg = er.async_get(hass)
    # Entity id uses the translated name "Sunshine duration (10 min)".
    entry = entity_reg.async_get("sensor.koniz_sunshine_duration_10_min")
    assert entry is not None
    assert entry.disabled_by is not None  # disabled by default


async def test_global_radiation(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """Global radiation sensor exists and is disabled by default."""
    await _setup(hass, config_entry)
    entity_reg = er.async_get(hass)
    entry = entity_reg.async_get("sensor.koniz_global_radiation")
    assert entry is not None
    assert entry.disabled_by is not None  # disabled by default


# ---------------------------------------------------------------------------
# None field → state ``unknown``, not an exception
# ---------------------------------------------------------------------------

async def test_none_field_gives_unknown_state(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """When an observation field is None the sensor state is 'unknown'."""
    from datetime import UTC, datetime

    null_obs = Observation(
        station_abbr="BER",
        timestamp=datetime(2026, 8, 27, 0, 40, tzinfo=UTC),
        # All measured fields left at their defaults (None).
    )

    await _setup(hass, config_entry)

    coordinator = config_entry.runtime_data.station_coordinator
    coordinator.async_set_updated_data(null_obs)
    await hass.async_block_till_done()

    assert _state(hass, "temperature") == STATE_UNKNOWN
    assert _state(hass, "humidity") == STATE_UNKNOWN
    assert _state(hass, "wind_speed") == STATE_UNKNOWN


# ---------------------------------------------------------------------------
# Device shared with the weather entity
# ---------------------------------------------------------------------------

async def test_sensor_uses_same_device_as_weather(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """All sensor entities share the device created by the weather entity."""
    from homeassistant.helpers import device_registry as dr

    await _setup(hass, config_entry)

    entity_reg = er.async_get(hass)
    device_reg = dr.async_get(hass)

    weather_entry = entity_reg.async_get("weather.koniz")
    assert weather_entry is not None
    weather_device_id = weather_entry.device_id

    sensor_entry = entity_reg.async_get("sensor.koniz_temperature")
    assert sensor_entry is not None
    assert sensor_entry.device_id == weather_device_id

    device = device_reg.async_get(weather_device_id)
    assert device is not None
    assert (DOMAIN, "2-309800") in device.identifiers


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

async def test_attribution(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """Every sensor carries the required MeteoSwiss attribution."""
    await _setup(hass, config_entry)
    assert _attr(hass, "temperature", "attribution") == "Source: MeteoSwiss"


# ---------------------------------------------------------------------------
# Data-inventory filtering (issue #46)
# ---------------------------------------------------------------------------


async def test_full_station_creates_all_sensors(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """BER with all parameters in the inventory → all sensor descriptions created."""
    await _setup(hass, config_entry)
    entity_reg = er.async_get(hass)
    device_unique_id = "2-309800"
    sensor_count = sum(
        1
        for desc in _SENSORS
        if entity_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{device_unique_id}_{desc.key}"
        ) is not None
    )
    # All 11 descriptions must produce a registry entry.
    assert sensor_count == len(_SENSORS)


async def test_reduced_station_creates_only_carried_sensors(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd_reduced: AiohttpClientMocker,
) -> None:
    """BER with only precipitation in the inventory → only that sensor created."""
    await _setup(hass, config_entry)
    entity_reg = er.async_get(hass)
    device_unique_id = "2-309800"

    # Precipitation sensor must be registered.
    assert entity_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{device_unique_id}_precipitation"
    ) is not None

    # Every other sensor must NOT be registered.
    for desc in _SENSORS:
        if desc.parameter_code == "rre150z0":
            continue
        entity_id = entity_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{device_unique_id}_{desc.key}"
        )
        assert entity_id is None, (
            f"sensor for key={desc.key!r} should not exist for a reduced station"
        )


async def test_orphan_registry_entries_removed_on_setup(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd_reduced: AiohttpClientMocker,
) -> None:
    """Registry entries for non-carried parameters are removed when setup runs."""
    # Pre-register a stale entry as if a previous full-station setup had run.
    entity_reg = er.async_get(hass)
    config_entry.add_to_hass(hass)
    stale_entry = entity_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        "2-309800_temperature",
        config_entry=config_entry,
        suggested_object_id="koniz_temperature",
    )
    assert stale_entry is not None  # pre-condition: orphan exists

    # Set up the integration with reduced inventory (precipitation only).
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # The stale temperature entry must have been removed.
    assert entity_reg.async_get("sensor.koniz_temperature") is None
