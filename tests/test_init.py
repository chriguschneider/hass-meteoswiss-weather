"""Integration-level tests for the config-entry lifecycle and coordinators.

Uses ``pytest-homeassistant-custom-component`` to spin up an in-process Home
Assistant instance. Upstream responses are replayed from ``tests/fixtures``
via the ``mock_ogd`` fixture (conftest.py); no test hits the network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from freezegun import freeze_time
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.meteoswiss_weather.const import (
    CONF_HOURLY_FORECAST,
    CONF_POINT_ID,
    CONF_POINT_NAME,
    CONF_POINT_TYPE_ID,
    CONF_POSTAL_CODE,
    CONF_STATION_ABBR,
    CONF_STATION_NAME,
    DOMAIN,
    HOURLY_FAR_MAX_AGE,
)
from custom_components.meteoswiss_weather.ogd.const import (
    DAILY_REQUIRED_PARAMS,
    DAILY_WIND_PARAMS,
    HOURLY_REQUIRED_PARAMS,
    station_now_url,
)

# The fixture run (conftest) and the station whose ``now`` file it serves.
_RUN_TS = "202608270200"
_STATION_ABBR = "BER"
_ENTITY_ID = "weather.koniz"


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


@pytest.fixture
def hourly_config_entry() -> MockConfigEntry:
    """A config entry with the opt-in hourly forecast option enabled."""
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
        options={CONF_HOURLY_FORECAST: True},
        title="Köniz",
        unique_id="2-309800",
    )


def _daily_calls(aioclient_mock: AiohttpClientMocker) -> int:
    """Number of daily parameter-file downloads recorded so far."""
    suffixes = tuple(f"{_RUN_TS}.{param}.csv" for param in DAILY_REQUIRED_PARAMS)
    return sum(
        1
        for _method, url, *_ in aioclient_mock.mock_calls
        if url.path.endswith(suffixes)
    )


# Hourly-only params: the three non-wind files (temperature, precipitation,
# symbol) that are fetched exclusively by the opt-in hourly forecast and never
# by the default daily refresh. Wind files (fu3010h0, fu3010h1, dkl010h0) are
# point-major block-fetched on every daily refresh (issue #60) so they are
# tracked separately by _wind_calls() below.
_HOURLY_ONLY_PARAMS = tuple(
    p for p in HOURLY_REQUIRED_PARAMS if p not in DAILY_WIND_PARAMS
)


def _hourly_calls(aioclient_mock: AiohttpClientMocker) -> int:
    """Opt-in hourly-forecast file downloads (excludes daily-wind files).

    Counts only the three non-wind hourly parameters so tests that verify the
    lazy hourly behaviour are not confused by the daily wind fetch (issue #60).
    """
    suffixes = tuple(f"{_RUN_TS}.{param}.csv" for param in _HOURLY_ONLY_PARAMS)
    return sum(
        1
        for _method, url, *_ in aioclient_mock.mock_calls
        if url.path.endswith(suffixes)
    )


def _wind_calls(aioclient_mock: AiohttpClientMocker) -> int:
    """Number of wind-block file downloads (daily wind fetch, issue #60)."""
    suffixes = tuple(f"{_RUN_TS}.{param}.csv" for param in DAILY_WIND_PARAMS)
    return sum(
        1
        for _method, url, *_ in aioclient_mock.mock_calls
        if url.path.endswith(suffixes)
    )


async def test_setup_populates_both_coordinators(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """Setup loads the entry and the first refresh fills both coordinators."""
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED

    runtime = config_entry.runtime_data
    observation = runtime.station_coordinator.data
    assert observation is not None
    assert observation.station_abbr == "BER"
    assert observation.temperature is not None

    forecast = runtime.forecast_coordinator.data
    assert forecast is not None
    assert len(forecast.daily) == 9
    # Hourly is off by default (ADR-0002) and lazy even when on (issue #54):
    # nothing has been fetched at setup.
    assert runtime.forecast_coordinator.hourly_provider.last_fetch is None
    assert runtime.forecast_coordinator.last_run is not None

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_unchanged_run_skips_daily_download(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A second forecast refresh on the same run downloads no daily files."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = config_entry.runtime_data.forecast_coordinator
    after_setup = _daily_calls(mock_ogd)
    assert after_setup == len(DAILY_REQUIRED_PARAMS)

    # Same run in the STAC listing → no MB-scale daily files fetched again.
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success
    assert _daily_calls(mock_ogd) == after_setup


async def test_first_refresh_failure_is_not_ready(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A failing station file makes setup retry (ConfigEntryNotReady)."""
    # Only the station file is registered, and it errors: the station
    # coordinator refreshes first, so its OgdError raises ConfigEntryNotReady
    # before the forecast coordinator is ever reached.
    aioclient_mock.get(station_now_url(_STATION_ABBR), status=503)

    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_update_failed_after_setup(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A later station error flips the coordinator to an unsuccessful update."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = config_entry.runtime_data.station_coordinator
    assert coordinator.last_update_success

    # Drop the cached validators and make the next fetch fail hard.
    mock_ogd.clear_requests()
    mock_ogd.get(station_now_url(_STATION_ABBR), status=500)

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not coordinator.last_update_success


# --- hourly forecast opt-in (ADR-0002) -------------------------------------


async def test_hourly_option_off_downloads_no_hourly_files(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """With the option off, no hourly parameter file is ever downloaded."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # A second forecast refresh must not reach for hourly files either.
    coordinator = config_entry.runtime_data.forecast_coordinator
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # Even asking the provider directly stays silent while the option is off.
    provider = coordinator.hourly_provider
    assert await provider.async_get_hourly(coordinator.last_run) is None
    assert _hourly_calls(mock_ogd) == 0
    assert provider.last_fetch is None


async def test_hourly_is_lazy_not_fetched_at_setup(
    hass: HomeAssistant,
    hourly_config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """With the option on, setup and coordinator ticks fetch no hourly files.

    The bulk hourly download only happens when something asks for the hourly
    forecast (issue #54); a coordinator refresh only tracks the run stamp.
    """
    with freeze_time(datetime(2026, 8, 27, 2, 0, tzinfo=UTC)):
        hourly_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(hourly_config_entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hourly_config_entry.runtime_data.forecast_coordinator
        assert _hourly_calls(mock_ogd) == 0
        assert coordinator.hourly_provider.last_fetch is None

        # A plain coordinator refresh still fetches nothing hourly.
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert _hourly_calls(mock_ogd) == 0


def _tre_calls(aioclient_mock: AiohttpClientMocker) -> int:
    """Downloads of the date-major temperature file (near/far tier, issue #68)."""
    suffix = f"{_RUN_TS}.tre200h0.csv"
    return sum(
        1
        for _method, url, *_ in aioclient_mock.mock_calls
        if url.path.endswith(suffix)
    )


async def test_hourly_provider_tiers_fetch_and_cache(
    hass: HomeAssistant,
    hourly_config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """The near/far tiers refresh on their schedule; the cache serves the rest.

    Drives the provider directly (standing in for the ``weather.get_forecasts``
    call that reaches it in production) and asserts the date-major temperature
    file is refetched only when a tier is genuinely due (issue #68, ADR-0002
    revision 2). The point-major group refreshes with every new run.
    """
    start = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    run = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)  # hour 2: a near landing hour

    with freeze_time(start) as frozen:
        hourly_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(hourly_config_entry.entry_id)
        await hass.async_block_till_done()

        provider = hourly_config_entry.runtime_data.forecast_coordinator.hourly_provider

        # First request: the far tier is stale (never fetched) so it downloads
        # the temperature file once, and the point-major group fetches too.
        hourly = await provider.async_get_hourly(run)
        assert hourly is not None
        assert len(hourly) == 24
        assert _tre_calls(mock_ogd) == 1

        # A second request an hour later on the same run hits the cache entirely.
        frozen.move_to(start + timedelta(hours=1))
        hourly = await provider.async_get_hourly(run)
        assert len(hourly) == 24
        assert _tre_calls(mock_ogd) == 1

        # A new run at a non-landing hour (03 UTC), still inside both fallbacks:
        # no tier is due, so the temperature file is not refetched.
        non_landing = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)
        frozen.move_to(start + timedelta(hours=1, minutes=5))
        await provider.async_get_hourly(non_landing)
        assert _tre_calls(mock_ogd) == 1

        # Past the far fallback (6 h): the far tier goes stale and refetches.
        frozen.move_to(start + HOURLY_FAR_MAX_AGE + timedelta(seconds=1))
        hourly = await provider.async_get_hourly(non_landing)
        assert len(hourly) == 24
        assert _tre_calls(mock_ogd) == 2


async def test_run_change_fetches_hourly_only_with_subscriber(
    hass: HomeAssistant,
    hourly_config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A new run downloads hourly only while a card/automation subscribes (#54).

    The coordinator just tracks the run stamp; the weather entity turns a run
    change into an ``async_update_listeners`` push, which pulls the bulk files
    lazily and only when someone is listening.
    """
    from homeassistant.components.weather import DOMAIN as WEATHER_DOMAIN

    n_params = len(_HOURLY_ONLY_PARAMS)

    with freeze_time(datetime(2026, 8, 27, 2, 0, tzinfo=UTC)):
        hass.states.async_set("sun.sun", "above_horizon")
        hourly_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(hourly_config_entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hourly_config_entry.runtime_data.forecast_coordinator
        entity = hass.data[WEATHER_DOMAIN].get_entity(_ENTITY_ID)
        assert entity is not None
        assert _hourly_calls(mock_ogd) == 0

        # A new run with nobody subscribed: the run change downloads nothing.
        coordinator.last_run = coordinator.last_run + timedelta(hours=3)
        entity._handle_forecast_update()
        await hass.async_block_till_done()
        assert _hourly_calls(mock_ogd) == 0

        # A card subscribes; the next run change pulls the hourly set once.
        received: list = []
        unsub = entity.async_subscribe_forecast("hourly", received.append)
        coordinator.last_run = coordinator.last_run + timedelta(hours=3)
        entity._handle_forecast_update()
        await hass.async_block_till_done()

        assert _hourly_calls(mock_ogd) == n_params
        assert received and received[-1] is not None
        unsub()


async def test_options_change_reloads_entry(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """Turning the hourly option on reloads the entry with the option enabled.

    The reload does not download anything: the hourly fetch stays lazy (#54).
    """
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert _hourly_calls(mock_ogd) == 0
    coordinator = config_entry.runtime_data.forecast_coordinator
    assert coordinator.hourly_provider.enabled is False

    hass.config_entries.async_update_entry(
        config_entry, options={CONF_HOURLY_FORECAST: True}
    )
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    # The reload rebuilt the coordinator with hourly on, but nothing is fetched
    # until something asks for the hourly forecast.
    provider = config_entry.runtime_data.forecast_coordinator.hourly_provider
    assert provider.enabled is True
    assert _hourly_calls(mock_ogd) == 0
    assert provider.last_fetch is None
