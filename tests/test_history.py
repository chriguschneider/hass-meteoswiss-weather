"""Tests for the recorder-side history operations of the reconfigure flow (A9).

The pure upstream parser lives in ``ogd/history.py`` (tested in
``test_ogd_history.py``); this module covers the thin Home Assistant layer:
the discard path (purge states + clear statistics) and the keep logbook note.
The recorder is mocked — the tests never touch a real database.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meteoswiss_weather.const import (
    CONF_POINT_ID,
    CONF_POINT_NAME,
    CONF_POINT_TYPE_ID,
    CONF_POSTAL_CODE,
    CONF_STATION_ABBR,
    CONF_STATION_NAME,
    DOMAIN,
)
from custom_components.meteoswiss_weather.history import (
    async_discard_station_history,
    async_log_station_switch,
)

_DEVICE_UID = "2-800100"


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=_DEVICE_UID,
        data={
            CONF_POINT_ID: 800100,
            CONF_POINT_TYPE_ID: 2,
            CONF_POSTAL_CODE: "8001",
            CONF_POINT_NAME: "Zürich",
            CONF_STATION_ABBR: "SMA",
            CONF_STATION_NAME: "Zürich / Fluntern",
        },
        title="Zürich",
    )


def _spy_purge_service(hass: HomeAssistant) -> list[ServiceCall]:
    """Register a stand-in ``recorder.purge_entities`` and capture its calls."""
    calls: list[ServiceCall] = []

    async def _handler(call: ServiceCall) -> None:
        calls.append(call)

    hass.services.async_register("recorder", "purge_entities", _handler)
    return calls


async def test_discard_purges_states_and_clears_statistics(
    hass: HomeAssistant,
) -> None:
    """Discard purges the station sensors' states and clears their statistics.

    A forecast-derived sensor sharing the device prefix is registered too and
    must be left alone — only the station-backed sensors carry station history.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    station_entity = registry.async_get_or_create(
        "sensor", DOMAIN, f"{_DEVICE_UID}_temperature", config_entry=entry
    )
    forecast_entity = registry.async_get_or_create(
        "sensor", DOMAIN, f"{_DEVICE_UID}_temp_max_today", config_entry=entry
    )
    hass.config.components.add("recorder")
    calls = _spy_purge_service(hass)

    instance = Mock()
    with patch(
        "homeassistant.components.recorder.get_instance",
        return_value=instance,
    ):
        await async_discard_station_history(hass, entry)

    assert len(calls) == 1
    purged = calls[0].data["entity_id"]
    assert station_entity.entity_id in purged
    assert forecast_entity.entity_id not in purged

    instance.async_clear_statistics.assert_called_once()
    cleared = instance.async_clear_statistics.call_args.args[0]
    assert station_entity.entity_id in cleared
    assert forecast_entity.entity_id not in cleared


async def test_discard_noop_without_recorder(hass: HomeAssistant) -> None:
    """With the recorder unloaded, discard neither purges nor clears."""
    entry = _entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", DOMAIN, f"{_DEVICE_UID}_temperature", config_entry=entry
    )
    # No "recorder" in hass.config.components.
    calls = _spy_purge_service(hass)

    with patch("homeassistant.components.recorder.get_instance") as get_instance:
        await async_discard_station_history(hass, entry)

    assert calls == []
    get_instance.assert_not_called()


async def test_discard_noop_without_entities(hass: HomeAssistant) -> None:
    """No station entities yet → nothing to purge, recorder untouched."""
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config.components.add("recorder")
    calls = _spy_purge_service(hass)

    with patch("homeassistant.components.recorder.get_instance") as get_instance:
        await async_discard_station_history(hass, entry)

    assert calls == []
    get_instance.assert_not_called()


async def test_log_station_switch_writes_logbook(hass: HomeAssistant) -> None:
    """The keep note goes to the logbook when the logbook is loaded."""
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config.components.add("logbook")

    with patch(
        "homeassistant.components.logbook.async_log_entry"
    ) as log_entry:
        async_log_station_switch(hass, entry, "Zürich / Fluntern", "Zürich / Kloten")

    log_entry.assert_called_once()
    message = log_entry.call_args.args[2]
    assert "Zürich / Fluntern" in message
    assert "Zürich / Kloten" in message


async def test_log_station_switch_without_logbook(hass: HomeAssistant) -> None:
    """Without the logbook component the note is a no-op (only the debug log)."""
    entry = _entry()
    entry.add_to_hass(hass)
    # No "logbook" in hass.config.components.

    with patch(
        "homeassistant.components.logbook.async_log_entry"
    ) as log_entry:
        async_log_station_switch(hass, entry, "A", "B")

    log_entry.assert_not_called()
