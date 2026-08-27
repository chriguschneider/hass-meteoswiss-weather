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
    HOURLY_FORECAST_MIN_INTERVAL,
)
from custom_components.meteoswiss_weather.ogd.const import (
    DAILY_REQUIRED_PARAMS,
    HOURLY_REQUIRED_PARAMS,
    station_now_url,
)

# The fixture run (conftest) and the station whose ``now`` file it serves.
_RUN_TS = "202608270200"
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


def _hourly_calls(aioclient_mock: AiohttpClientMocker) -> int:
    """Number of hourly parameter-file downloads recorded so far."""
    suffixes = tuple(f"{_RUN_TS}.{param}.csv" for param in HOURLY_REQUIRED_PARAMS)
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
    # Hourly is off by default (ADR-0002): no hourly data fetched.
    assert forecast.hourly is None
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
    await config_entry.runtime_data.forecast_coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _hourly_calls(mock_ogd) == 0
    assert config_entry.runtime_data.forecast_coordinator.data.hourly is None


async def test_hourly_option_on_fetches_once_and_throttles(
    hass: HomeAssistant,
    hourly_config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """Hourly is fetched at setup, then never faster than the 3 h floor."""
    start = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    n_params = len(HOURLY_REQUIRED_PARAMS)

    with freeze_time(start) as frozen:
        hourly_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(hourly_config_entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hourly_config_entry.runtime_data.forecast_coordinator
        # First refresh downloaded the full hourly set exactly once.
        assert _hourly_calls(mock_ogd) == n_params
        assert coordinator.data.hourly is not None
        assert len(coordinator.data.hourly) == 24

        # A refresh one hour later is inside the 3 h floor: no new download.
        frozen.move_to(start + timedelta(hours=1))
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.last_update_success
        assert _hourly_calls(mock_ogd) == n_params

        # Just past the 3 h floor the hourly files are fetched again.
        frozen.move_to(start + HOURLY_FORECAST_MIN_INTERVAL + timedelta(seconds=1))
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.last_update_success
        assert _hourly_calls(mock_ogd) == 2 * n_params


async def test_options_change_reloads_entry(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """Turning the hourly option on reloads the entry and starts fetching it."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert _hourly_calls(mock_ogd) == 0

    hass.config_entries.async_update_entry(
        config_entry, options={CONF_HOURLY_FORECAST: True}
    )
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    # The reload rebuilt the coordinator with hourly on, so it fetched the set.
    assert _hourly_calls(mock_ogd) == len(HOURLY_REQUIRED_PARAMS)
    assert config_entry.runtime_data.forecast_coordinator.data.hourly is not None
