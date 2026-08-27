"""Integration-level tests for the MeteoSwiss Weather config entry lifecycle.

Uses ``pytest-homeassistant-custom-component`` to spin up a real (in-process)
Home Assistant instance. The ``enable_custom_integrations`` autouse fixture
(conftest.py) ensures the integration is loaded from the repo tree.

No network: the integration scaffold has no platforms and therefore makes
no upstream requests during setup or teardown.
"""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meteoswiss_weather.const import CONF_POSTAL_CODE, DOMAIN


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A minimal config entry that mimics what the config flow produces."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_POSTAL_CODE: 3098},
        title="MeteoSwiss 3098",
        unique_id="3098",
    )


async def test_setup_and_unload(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """The config entry loads and unloads cleanly with no platforms active."""
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.NOT_LOADED
