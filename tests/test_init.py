"""Integration-level tests for the config-entry lifecycle and coordinators.

Uses ``pytest-homeassistant-custom-component`` to spin up an in-process Home
Assistant instance. Upstream responses are replayed from ``tests/fixtures``
via the ``mock_ogd`` fixture (conftest.py); no test hits the network.
"""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
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
from custom_components.meteoswiss_weather.ogd.const import (
    DAILY_REQUIRED_PARAMS,
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


def _daily_calls(aioclient_mock: AiohttpClientMocker) -> int:
    """Number of daily parameter-file downloads recorded so far."""
    suffixes = tuple(f"{_RUN_TS}.{param}.csv" for param in DAILY_REQUIRED_PARAMS)
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
    assert len(forecast) == 9
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
