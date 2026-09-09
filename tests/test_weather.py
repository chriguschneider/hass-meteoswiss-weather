"""Tests for the weather platform (issue #10).

Spins up an in-process Home Assistant with the ``mock_ogd`` fixture
(conftest.py) so the entity is built from the trimmed real fixtures; no test
hits the network. Covers the current-condition attributes read from the
station observation, the daily forecast returned by ``weather.get_forecasts``,
and the availability contract across the two coordinators.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from freezegun import freeze_time
from homeassistant.components.sun import STATE_ABOVE_HORIZON, STATE_BELOW_HORIZON
from homeassistant.components.weather import WeatherEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.meteoswiss_weather.const import (
    AVAILABILITY_CHECK_INTERVAL,
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
    FORECAST_MAX_AGE,
    STATION_MAX_AGE,
)
from custom_components.meteoswiss_weather.ogd.const import (
    COLLECTION_FORECAST,
    stac_items_url,
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


def _freshen_station(entry: MockConfigEntry) -> None:
    """Anchor the station observation to 'now' and re-render the entity.

    Availability now keys on the observation's own timestamp (issue #108): the
    trimmed fixtures are older than the 1 h staleness bound, so a test that
    wants the entity available anchors the cached observation to the (possibly
    frozen) current time. Only the timestamp moves — every measured value is
    preserved via ``replace`` — and ``async_set_updated_data`` doubles as the
    listener push that re-renders the entity state.
    """
    coordinator = entry.runtime_data.station_coordinator
    if coordinator.data is not None:
        coordinator.async_set_updated_data(
            replace(coordinator.data, timestamp=dt_util.utcnow())
        )


def _freshen_forecast(entry: MockConfigEntry) -> None:
    """Anchor the forecast run stamp to 'now' for the age-based guard (#108).

    Used by tests frozen well past the fixture run (the daily-symbol tests read
    a forecast day days ahead of the run); the daily forecast is served from the
    coordinator's cached data, so only ``last_run`` — which availability reads —
    needs to move, never the request URLs the hourly path would build from it.
    """
    entry.runtime_data.forecast_coordinator.last_run = dt_util.utcnow()


async def _setup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    sun: str = STATE_ABOVE_HORIZON,
    freshen: bool = True,
) -> None:
    """Set the sun state, add the entry and run setup to completion.

    ``freshen`` anchors the station observation to 'now' after setup so the
    age-based availability guard (issue #108) treats the older fixtures as
    freshly fetched; availability-focused tests pass ``freshen=False`` to drive
    the timestamps themselves.
    """
    hass.states.async_set("sun.sun", sun)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    if freshen:
        _freshen_station(entry)


async def test_current_conditions_from_station(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """Current-condition attributes come from the latest station row."""
    await _setup(hass, config_entry, freshen=False)
    # Anchor the run stamp before the observation poke re-renders the entity, so
    # the age-based availability guard (#108) sees both coordinators fresh.
    _freshen_forecast(config_entry)
    _freshen_station(config_entry)

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
        await _setup(hass, config_entry, sun=STATE_ABOVE_HORIZON, freshen=False)
        # Frozen days ahead of the fixture run: anchor the run stamp first, then
        # the observation, whose poke re-renders the now-available entity (#108).
        _freshen_forecast(config_entry)
        _freshen_station(config_entry)
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
        await _setup(hass, config_entry, sun=STATE_BELOW_HORIZON, freshen=False)
        _freshen_forecast(config_entry)
        _freshen_station(config_entry)
        assert hass.states.get(_ENTITY_ID).state == "clear-night"


async def test_daily_forecast_service(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """``weather.get_forecasts`` (daily) returns the 9 fixture days."""
    await _setup(hass, config_entry, freshen=False)
    _freshen_forecast(config_entry)
    _freshen_station(config_entry)

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


# --- availability: degrade-don't-fail on a transient blip (issue #108) -----

# A moment comfortably within both staleness bounds of the fixture forecast run
# (2026-08-27 02:00 UTC): 10 h after the run (< FORECAST_MAX_AGE) and used as the
# anchor for the freshly-poked station observation.
_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


async def test_available_survives_a_transient_forecast_failure(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A failed forecast refresh with cached data leaves the entity available.

    The whole point of issue #108: the station-sourced current conditions must
    not vanish because a forecast file could not be fetched.
    """
    with freeze_time(_NOW):
        await _setup(hass, config_entry)
        assert hass.states.get(_ENTITY_ID).state != "unavailable"

        # A transient upstream error on the hourly forecast check: run discovery
        # (a STAC call) 500s. last_update_success flips false, but the cached
        # daily data and run stamp are untouched.
        mock_ogd.clear_requests()
        mock_ogd.get(stac_items_url(COLLECTION_FORECAST), status=500)
        forecast = config_entry.runtime_data.forecast_coordinator
        await forecast.async_refresh()
        await hass.async_block_till_done()
        assert forecast.last_update_success is False

        state = hass.states.get(_ENTITY_ID)
        assert state.state != "unavailable"
        # Current conditions from the station are intact.
        assert state.attributes["temperature"] == 19.5


async def test_available_survives_a_transient_station_failure(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A failed station refresh with fresh cached data likewise stays available."""
    with freeze_time(_NOW):
        await _setup(hass, config_entry)
        assert hass.states.get(_ENTITY_ID).state != "unavailable"

        station = config_entry.runtime_data.station_coordinator
        mock_ogd.clear_requests()
        mock_ogd.get(station_now_url(_STATION_ABBR), status=500)
        await station.async_refresh()
        await hass.async_block_till_done()
        assert station.last_update_success is False

        assert hass.states.get(_ENTITY_ID).state != "unavailable"


async def test_unavailable_when_station_observation_goes_stale(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """Once the observation is older than STATION_MAX_AGE the entity drops out."""
    with freeze_time(_NOW):
        await _setup(hass, config_entry)
        assert hass.states.get(_ENTITY_ID).state != "unavailable"

        # An observation just past the bound (the forecast is still fresh, so
        # this isolates the station guard).
        station = config_entry.runtime_data.station_coordinator
        stale = _NOW - STATION_MAX_AGE - timedelta(minutes=1)
        station.async_set_updated_data(replace(station.data, timestamp=stale))
        await hass.async_block_till_done()

        assert hass.states.get(_ENTITY_ID).state == "unavailable"


async def test_unavailable_when_forecast_run_goes_stale(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """Once the run is older than FORECAST_MAX_AGE the entity drops out."""
    with freeze_time(_NOW):
        await _setup(hass, config_entry)
        assert hass.states.get(_ENTITY_ID).state != "unavailable"

        # Age the run past the bound; keep the station fresh so this isolates the
        # forecast guard. Poking the station also re-renders the entity.
        forecast = config_entry.runtime_data.forecast_coordinator
        forecast.last_run = _NOW - FORECAST_MAX_AGE - timedelta(hours=1)
        _freshen_station(config_entry)
        await hass.async_block_till_done()

        assert hass.states.get(_ENTITY_ID).state == "unavailable"


async def test_unavailable_after_a_sustained_outage(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A total outage ages the entity out without any listener push to help it.

    The other staleness tests manufacture the push that re-renders the entity.
    This one does not, and that is the point: once a failure follows a failure,
    ``DataUpdateCoordinator`` stops notifying listeners altogether, so nothing
    would ever re-evaluate the age bounds. Only the entity's own staleness tick
    gets it to ``unavailable``.
    """
    with freeze_time(_NOW) as frozen:
        await _setup(hass, config_entry)
        assert hass.states.get(_ENTITY_ID).state != "unavailable"

        # Everything upstream 500s from here on; the cached data stays in place.
        mock_ogd.clear_requests()
        mock_ogd.get(station_now_url(_STATION_ABBR), status=500)
        mock_ogd.get(stac_items_url(COLLECTION_FORECAST), status=500)
        station = config_entry.runtime_data.station_coordinator
        forecast = config_entry.runtime_data.forecast_coordinator
        # Two failures in a row per coordinator: from the second one on, HA
        # swallows the failure without notifying listeners, so no refresh
        # attempt re-renders the entity any more.
        for coordinator in (station, forecast):
            await coordinator.async_refresh()
            await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert station.last_update_success is False
        assert forecast.last_update_success is False
        assert hass.states.get(_ENTITY_ID).state != "unavailable"

        # Age past the station bound and let the staleness tick fire.
        frozen.move_to(_NOW + STATION_MAX_AGE + AVAILABILITY_CHECK_INTERVAL)
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        assert hass.states.get(_ENTITY_ID).state == "unavailable"


async def test_unavailable_before_first_forecast_fetch(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """With no forecast data yet (cold start) the entity is unavailable.

    Data presence is required regardless of age: a coordinator that has never
    delivered data cannot make the entity available.
    """
    with freeze_time(_NOW):
        await _setup(hass, config_entry)
        assert hass.states.get(_ENTITY_ID).state != "unavailable"

        # Simulate "no forecast fetched yet": drop the cached data and re-render.
        config_entry.runtime_data.forecast_coordinator.data = None
        _freshen_station(config_entry)
        await hass.async_block_till_done()

        assert hass.states.get(_ENTITY_ID).state == "unavailable"


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
    """Once hourly data is cached, the current hour's symbol sharpens condition.

    At 12:00 UTC the hourly symbol is 7 (snowy-rainy) while today's daily
    symbol is 2 (partlycloudy). The hourly fetch is lazy (issue #54), so the
    condition only sharpens after something pulls the hourly forecast; before
    that it falls back to the daily symbol.
    """
    with freeze_time(datetime(2026, 8, 27, 12, 0, tzinfo=UTC)):
        await _setup(hass, hourly_config_entry)

        # Nothing has fetched hourly yet: condition uses the daily symbol.
        assert hass.states.get(_ENTITY_ID).state == "partlycloudy"

        # Pull the hourly forecast (as a card or automation would), which fills
        # the provider cache, then re-render the entity state.
        await hass.services.async_call(
            "weather",
            "get_forecasts",
            {"entity_id": _ENTITY_ID, "type": "hourly"},
            blocking=True,
            return_response=True,
        )
        # Re-render the entity while keeping the observation fresh (#108): a real
        # station re-fetch would replay the older fixture row and stale the guard.
        _freshen_station(hourly_config_entry)
        await hass.async_block_till_done()

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

        # Fill the provider cache, then rewrite the current hour's symbol to the
        # night code upstream would still be sending.
        await hass.services.async_call(
            "weather",
            "get_forecasts",
            {"entity_id": _ENTITY_ID, "type": "hourly"},
            blocking=True,
            return_response=True,
        )
        provider = hourly_config_entry.runtime_data.forecast_coordinator.hourly_provider
        this_hour = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
        provider._hourly = [
            replace(hour, symbol=101) if hour.time == this_hour else hour
            for hour in provider.cached_hourly
        ]

        # Re-render the entity while keeping the observation fresh (#108): a real
        # station re-fetch would replay the older fixture row and stale the guard.
        _freshen_station(hourly_config_entry)
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

        await hass.services.async_call(
            "weather",
            "get_forecasts",
            {"entity_id": _ENTITY_ID, "type": "hourly"},
            blocking=True,
            return_response=True,
        )
        provider = hourly_config_entry.runtime_data.forecast_coordinator.hourly_provider
        this_hour = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
        provider._hourly = [
            replace(hour, symbol=101) if hour.time == this_hour else hour
            for hour in provider.cached_hourly
        ]

        # Re-render the entity while keeping the observation fresh (#108): a real
        # station re-fetch would replay the older fixture row and stale the guard.
        _freshen_station(hourly_config_entry)
        await hass.async_block_till_done()

        assert hass.states.get(_ENTITY_ID).state == "clear-night"
