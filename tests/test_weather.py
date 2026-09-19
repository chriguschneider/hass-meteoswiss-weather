"""Tests for the weather platform (issue #10).

Spins up an in-process Home Assistant with the ``mock_ogd`` fixture
(conftest.py) so the entity is built from the trimmed real fixtures; no test
hits the network. Covers the current-condition attributes read from the
station observation, the daily forecast returned by ``weather.get_forecasts``,
and the availability contract across the two coordinators.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from freezegun import freeze_time
from homeassistant.components.sun import STATE_ABOVE_HORIZON, STATE_BELOW_HORIZON
from homeassistant.components.weather import WeatherEntityFeature
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.meteoswiss_weather.const import (
    CONF_HOURLY_CLOUD_LAYERS,
    CONF_HOURLY_FORECAST,
    CONF_HOURLY_TEMP_PERCENTILES,
    CONF_POINT_ID,
    CONF_POINT_NAME,
    CONF_POINT_TYPE_ID,
    CONF_POSTAL_CODE,
    CONF_STATION_ABBR,
    CONF_STATION_NAME,
    DOMAIN,
)
from custom_components.meteoswiss_weather.ogd.const import (
    HOURLY_SYMBOL,
    station_now_url,
)

_STATION_ABBR = "BER"
_ENTITY_ID = "weather.koniz"


def _entry_data() -> dict:
    return {
        CONF_POINT_ID: 309800,
        CONF_POINT_TYPE_ID: 2,
        CONF_POSTAL_CODE: "3098",
        CONF_POINT_NAME: "Köniz",
        CONF_STATION_ABBR: _STATION_ABBR,
        CONF_STATION_NAME: "Bern / Zollikofen",
    }


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A config entry shaped exactly like the config flow produces."""
    return MockConfigEntry(
        domain=DOMAIN,
        data=_entry_data(),
        title="Köniz",
        unique_id="2-309800",
    )


@pytest.fixture
def hourly_config_entry() -> MockConfigEntry:
    """A config entry with the opt-in hourly forecast enabled."""
    return MockConfigEntry(
        domain=DOMAIN,
        data=_entry_data(),
        options={CONF_HOURLY_FORECAST: True},
        title="Köniz",
        unique_id="2-309800",
    )


@pytest.fixture
def hourly_gated_config_entry() -> MockConfigEntry:
    """Hourly on with the B9 cloud layers and B11 percentiles enabled (issue #69)."""
    return MockConfigEntry(
        domain=DOMAIN,
        data=_entry_data(),
        options={
            CONF_HOURLY_FORECAST: True,
            CONF_HOURLY_CLOUD_LAYERS: True,
            CONF_HOURLY_TEMP_PERCENTILES: True,
        },
        title="Köniz",
        unique_id="2-309800",
    )


async def _setup(
    hass: HomeAssistant, entry: MockConfigEntry, *, sun: str = STATE_ABOVE_HORIZON
) -> None:
    """Set the sun state, add the entry and run setup to completion."""
    hass.states.async_set("sun.sun", sun)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_current_conditions_from_station(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """Current-condition attributes come from the latest station row."""
    await _setup(hass, config_entry)

    state = hass.states.get(_ENTITY_ID)
    assert state is not None

    # The last BER row carrying a temperature is 00:40 (docs/ogd.md §A1).
    attrs = state.attributes
    assert attrs["temperature"] == 19.5
    assert attrs["humidity"] == 88.0
    assert attrs["dew_point"] == 17.2
    assert attrs["pressure"] == 1014.8  # QFF, reduced to sea level
    assert attrs["wind_speed"] == 4.0
    assert attrs["wind_bearing"] == 245
    assert attrs["wind_gust_speed"] == 5.5
    assert attrs["attribution"] == "Source: MeteoSwiss"


async def test_condition_from_daily_symbol_daytime(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """With the sun up, 2026-08-29's daily symbol (code 1) becomes ``sunny``."""
    with freeze_time(datetime(2026, 8, 29, 12, 0, tzinfo=UTC)):
        await _setup(hass, config_entry, sun=STATE_ABOVE_HORIZON)
        assert hass.states.get(_ENTITY_ID).state == "sunny"


async def test_condition_from_daily_symbol_nighttime(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """With the sun down, the same day symbol (code 1) becomes ``clear-night``.

    A daytime daily symbol shown at night is substituted by its night
    counterpart (code + 100); 1 → 101 is ``clear-night``.
    """
    with freeze_time(datetime(2026, 8, 29, 23, 0, tzinfo=UTC)):
        await _setup(hass, config_entry, sun=STATE_BELOW_HORIZON)
        assert hass.states.get(_ENTITY_ID).state == "clear-night"


async def test_daily_forecast_service(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """``weather.get_forecasts`` (daily) returns the 9 fixture days."""
    await _setup(hass, config_entry)

    response = await hass.services.async_call(
        "weather",
        "get_forecasts",
        {"entity_id": _ENTITY_ID, "type": "daily"},
        blocking=True,
        return_response=True,
    )

    forecasts = response[_ENTITY_ID]["forecast"]
    assert len(forecasts) == 9

    first = forecasts[0]
    # Real 309800;2 (Köniz) values from run 2026-08-27 02:00 UTC.
    assert first["datetime"] == "2026-08-27"
    assert first["condition"] == "partlycloudy"  # symbol 2 → partlycloudy
    assert first["temperature"] == 29.3  # native max
    assert first["templow"] == 16.9  # native min
    assert first["precipitation"] == 0.0

    # Check the last day too.
    assert forecasts[-1]["datetime"] == "2026-09-04"
    assert forecasts[-1]["temperature"] == 23.9


async def test_device_info(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """The entity registers a service device keyed on the unique id."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    await _setup(hass, config_entry)

    entity_reg = er.async_get(hass)
    entry = entity_reg.async_get(_ENTITY_ID)
    assert entry is not None
    assert entry.unique_id == "2-309800"

    device_reg = dr.async_get(hass)
    device = device_reg.async_get(entry.device_id)
    assert device is not None
    assert (DOMAIN, "2-309800") in device.identifiers
    assert device.manufacturer == "MeteoSwiss"
    assert device.configuration_url == "https://opendatadocs.meteoswiss.ch"


async def test_transient_station_failure_keeps_entity_available(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A transient station 500 does not flip the entity to ``unavailable`` (issue #108).

    The coordinator retains the last good observation on a failed refresh, so
    current conditions remain available to automations across a brief outage.
    """
    await _setup(hass, config_entry)
    assert hass.states.get(_ENTITY_ID).state != "unavailable"

    coordinator = config_entry.runtime_data.station_coordinator
    mock_ogd.clear_requests()
    mock_ogd.get(station_now_url(_STATION_ABBR), status=500)

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # The coordinator still holds data from the previous successful update.
    assert coordinator.data is not None
    assert hass.states.get(_ENTITY_ID).state != "unavailable"


async def test_transient_forecast_failure_keeps_entity_available(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A transient forecast 500 does not flip the entity to ``unavailable`` (#108).

    The forecast coordinator retains the previous run's data so a brief STAC
    outage does not silence current conditions from the station coordinator.
    """
    from datetime import UTC, datetime

    from custom_components.meteoswiss_weather.ogd.const import stac_day_item_url

    await _setup(hass, config_entry)
    assert hass.states.get(_ENTITY_ID).state != "unavailable"

    forecast_coordinator = config_entry.runtime_data.forecast_coordinator
    mock_ogd.clear_requests()
    # Fail STAC run-discovery: only the day item (503) is registered; yesterday's
    # item and the full listing are not, so every fallback path raises
    # OgdConnectionError → UpdateFailed.
    today_id = datetime.now(UTC).strftime("%Y%m%d") + "-ch"
    day_item_url = stac_day_item_url("ch.meteoschweiz.ogd-local-forecasting", today_id)
    mock_ogd.get(day_item_url, status=503)

    await forecast_coordinator.async_refresh()
    await hass.async_block_till_done()

    # The coordinator still holds data from the previous successful update.
    assert forecast_coordinator.data is not None
    assert hass.states.get(_ENTITY_ID).state != "unavailable"


async def test_unavailable_when_coordinators_have_no_data(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """Entity goes unavailable when a coordinator has no data (issue #108).

    Tests the ``available`` property directly: after setup, clearing a
    coordinator's data simulates "never succeeded" and the property returns
    ``False``; restoring the data makes it ``True`` again.
    """
    from homeassistant.helpers import entity_registry as er

    from custom_components.meteoswiss_weather.weather import MeteoSwissWeather

    await _setup(hass, config_entry)

    entity_reg = er.async_get(hass)
    entry_entity = entity_reg.async_get(_ENTITY_ID)
    assert entry_entity is not None

    # Reach the live entity object from the platform.
    platform = hass.data["entity_components"]["weather"]
    entity_obj: MeteoSwissWeather = platform.get_entity(_ENTITY_ID)
    assert entity_obj is not None
    assert entity_obj.available

    orig_station = entity_obj.coordinator.data
    orig_forecast = entity_obj._forecast_coordinator.data

    # No station data → not available.
    entity_obj.coordinator.data = None
    assert not entity_obj.available

    # Both missing → not available.
    entity_obj._forecast_coordinator.data = None
    assert not entity_obj.available

    # Station restored, forecast still missing → not available.
    entity_obj.coordinator.data = orig_station
    assert not entity_obj.available

    # Both restored → available again.
    entity_obj._forecast_coordinator.data = orig_forecast
    assert entity_obj.available


# --- hourly forecast (opt-in, ADR-0002) ------------------------------------


async def test_hourly_feature_absent_when_option_off(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """FORECAST_HOURLY is not advertised while the option is off."""
    await _setup(hass, config_entry)
    features = hass.states.get(_ENTITY_ID).attributes["supported_features"]
    assert not features & WeatherEntityFeature.FORECAST_HOURLY
    assert features & WeatherEntityFeature.FORECAST_DAILY


async def test_hourly_feature_and_forecast_when_option_on(
    hass: HomeAssistant,
    hourly_config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """With the option on, FORECAST_HOURLY is advertised and returns 24 hours."""
    # Freeze at 00:00 UTC so horizon_start equals the first fixture hour and
    # no past-hour trimming occurs (issue #92).
    with freeze_time(datetime(2026, 8, 27, 0, 0, tzinfo=UTC)):
        await _setup(hass, hourly_config_entry)

        features = hass.states.get(_ENTITY_ID).attributes["supported_features"]
        assert features & WeatherEntityFeature.FORECAST_HOURLY

        response = await hass.services.async_call(
            "weather",
            "get_forecasts",
            {"entity_id": _ENTITY_ID, "type": "hourly"},
            blocking=True,
            return_response=True,
        )

    forecasts = response[_ENTITY_ID]["forecast"]
    assert len(forecasts) == 24

    first = forecasts[0]
    assert first["datetime"] == "2026-08-27T00:00:00+00:00"
    assert first["temperature"] == 10.0
    assert first["precipitation"] == 0.0
    assert first["wind_speed"] == 5.0
    assert first["wind_gust_speed"] == 8.0
    assert first["wind_bearing"] == 180
    assert first["condition"] == "sunny"  # symbol 1 at hour 0


async def test_hourly_cloud_and_percentile_attributes(
    hass: HomeAssistant,
    hourly_gated_config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """B9/B11 (issue #69): the gated options add the hourly attributes.

    ``cloud_coverage`` is documented as the maximum of the three layers. In the
    fixture for hour 0, the layers are high=20, mid=40, low=10, so the single
    number is 40; the three layers and the p10/p90 band ride along as extras.
    """
    # Freeze at 00:00 UTC so horizon_start equals the first fixture hour (issue #92).
    with freeze_time(datetime(2026, 8, 27, 0, 0, tzinfo=UTC)):
        await _setup(hass, hourly_gated_config_entry)

        response = await hass.services.async_call(
            "weather",
            "get_forecasts",
            {"entity_id": _ENTITY_ID, "type": "hourly"},
            blocking=True,
            return_response=True,
        )

    first = response[_ENTITY_ID]["forecast"][0]
    # cloud_coverage is the maximum of the three layers (documented).
    assert first["cloud_coverage"] == 40
    assert first["cloud_coverage_high"] == 20.0
    assert first["cloud_coverage_mid"] == 40.0
    assert first["cloud_coverage_low"] == 10.0
    # B11 percentile band brackets the median temperature (converted key).
    assert first["temperature_p10"] == 8.0
    assert first["temperature_p90"] == 13.0
    assert first["temperature_p10"] < first["temperature"]
    assert first["temperature"] < first["temperature_p90"]


async def test_hourly_gated_attributes_absent_without_options(
    hass: HomeAssistant,
    hourly_config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """Plain hourly (no gated options) exposes none of the B9/B11 attributes."""
    with freeze_time(datetime(2026, 8, 27, 2, 0, tzinfo=UTC)):
        await _setup(hass, hourly_config_entry)

        response = await hass.services.async_call(
            "weather",
            "get_forecasts",
            {"entity_id": _ENTITY_ID, "type": "hourly"},
            blocking=True,
            return_response=True,
        )

    first = response[_ENTITY_ID]["forecast"][0]
    assert "cloud_coverage" not in first
    assert "cloud_coverage_high" not in first
    assert "temperature_p10" not in first
    assert "temperature_p90" not in first


async def test_condition_prefers_current_hour_symbol(
    hass: HomeAssistant,
    hourly_config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """The current hour's symbol sharpens the condition, eagerly (ADR-0008).

    At 12:00 UTC the hourly symbol is 7 (snowy-rainy) while today's daily
    symbol is 2 (partlycloudy). The eager hourly refresh (issue #124) fills the
    store at setup, so the condition sharpens with no card open and no
    ``get_forecasts`` call — the store is the source, not a subscriber's fetch.
    """
    with freeze_time(datetime(2026, 8, 27, 12, 0, tzinfo=UTC)):
        await _setup(hass, hourly_config_entry)
        assert hass.states.get(_ENTITY_ID).state == "snowy-rainy"


async def test_condition_corrects_a_night_symbol_after_sunrise(
    hass: HomeAssistant,
    hourly_config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A night symbol outliving sunrise renders as its day variant (#103).

    Upstream keeps the night variant of ``jww003i0`` for a couple of hours past
    sunrise, which left the entity on ``clear-night`` in broad daylight. With
    ``sun.sun`` above the horizon the day counterpart wins: 101 → 1 → ``sunny``.
    """
    with freeze_time(datetime(2026, 8, 27, 12, 0, tzinfo=UTC)):
        await _setup(hass, hourly_config_entry, sun=STATE_ABOVE_HORIZON)

        # Rewrite the current hour's symbol in the store to the night code
        # upstream would still be sending.
        coordinator = hourly_config_entry.runtime_data.forecast_coordinator
        this_hour = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
        coordinator.store.put(
            HOURLY_SYMBOL,
            {this_hour: 101},
            run=coordinator.last_run,
            fetched_at=this_hour,
            source="hourly",
        )

        await hourly_config_entry.runtime_data.station_coordinator.async_refresh()
        await hass.async_block_till_done()

        assert hass.states.get(_ENTITY_ID).state == "sunny"


async def test_condition_keeps_a_night_symbol_while_the_sun_is_down(
    hass: HomeAssistant,
    hourly_config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """The #103 correction is one-way: at night the night symbol stands."""
    with freeze_time(datetime(2026, 8, 27, 12, 0, tzinfo=UTC)):
        await _setup(hass, hourly_config_entry, sun=STATE_BELOW_HORIZON)

        coordinator = hourly_config_entry.runtime_data.forecast_coordinator
        this_hour = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
        coordinator.store.put(
            HOURLY_SYMBOL,
            {this_hour: 101},
            run=coordinator.last_run,
            fetched_at=this_hour,
            source="hourly",
        )

        await hourly_config_entry.runtime_data.station_coordinator.async_refresh()
        await hass.async_block_till_done()

        assert hass.states.get(_ENTITY_ID).state == "clear-night"
