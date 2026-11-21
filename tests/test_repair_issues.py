"""Parse-error repair issues are per config entry (issue #251).

Each parse-error repair issue id is ``<key>_<entry_id>`` and carries the entry
title as the ``location`` placeholder, so one entry's successful poll never
clears another entry's notice, unloading an entry removes its notices, and a
notice left under the old domain-wide id is dropped at setup.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
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
from custom_components.meteoswiss_weather.coordinator import (
    PARSE_ISSUE_KEYS,
    ImisCoordinator,
    PollenCoordinator,
    parse_issue_id,
)
from custom_components.meteoswiss_weather.ogd import OgdParseError
from custom_components.meteoswiss_weather.slf import SlfParseError

_STATION_KEY = "parse_error_station"


def _entry(title: str, unique_id: str) -> MockConfigEntry:
    # Both entries point at the fixture point and station so ``mock_ogd``
    # serves them alike; only the title and unique id differ.
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
        title=title,
        unique_id=unique_id,
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _issue(hass: HomeAssistant, key: str, entry: MockConfigEntry) -> ir.IssueEntry:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, parse_issue_id(key, entry.entry_id)
    )


async def test_other_entry_success_keeps_the_issue(
    hass: HomeAssistant, mock_ogd: AiohttpClientMocker
) -> None:
    """Entry B's successful poll leaves entry A's notice; A's own success clears it."""
    entry_a = _entry("Köniz", "2-309800")
    entry_b = _entry("Bern", "2-300000")
    await _setup(hass, entry_a)
    await _setup(hass, entry_b)
    coord_a = entry_a.runtime_data.station_coordinator
    coord_b = entry_b.runtime_data.station_coordinator

    with patch.object(coord_a, "_fetch", side_effect=OgdParseError("bad format")):
        await coord_a.async_refresh()
    await coord_b.async_refresh()
    await hass.async_block_till_done()

    assert coord_b.last_update_success is True
    issue = _issue(hass, _STATION_KEY, entry_a)
    assert issue is not None
    assert issue.translation_key == _STATION_KEY
    assert issue.translation_placeholders == {"location": "Köniz"}
    assert _issue(hass, _STATION_KEY, entry_b) is None

    await coord_a.async_refresh()
    await hass.async_block_till_done()
    assert coord_a.last_update_success is True
    assert _issue(hass, _STATION_KEY, entry_a) is None


async def test_unload_removes_the_entry_issues(
    hass: HomeAssistant, mock_ogd: AiohttpClientMocker
) -> None:
    """Unloading an entry deletes its parse-error notices, not another entry's."""
    entry_a = _entry("Köniz", "2-309800")
    entry_b = _entry("Bern", "2-300000")
    await _setup(hass, entry_a)
    await _setup(hass, entry_b)
    for entry in (entry_a, entry_b):
        coord = entry.runtime_data.station_coordinator
        with patch.object(coord, "_fetch", side_effect=OgdParseError("bad")):
            await coord.async_refresh()
    await hass.async_block_till_done()
    assert _issue(hass, _STATION_KEY, entry_a) is not None

    assert await hass.config_entries.async_unload(entry_a.entry_id)
    await hass.async_block_till_done()

    for key in PARSE_ISSUE_KEYS:
        assert _issue(hass, key, entry_a) is None
    assert _issue(hass, _STATION_KEY, entry_b) is not None


async def test_legacy_domain_wide_issue_is_dropped_at_setup(
    hass: HomeAssistant, mock_ogd: AiohttpClientMocker
) -> None:
    """A notice raised under the old domain-wide id leaves no orphan."""
    for key in PARSE_ISSUE_KEYS:
        ir.async_create_issue(
            hass,
            DOMAIN,
            key,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=key,
        )

    await _setup(hass, _entry("Köniz", "2-309800"))

    registry = ir.async_get(hass)
    for key in PARSE_ISSUE_KEYS:
        assert registry.async_get_issue(DOMAIN, key) is None


async def test_pollen_parse_error_issue_is_per_entry(hass: HomeAssistant) -> None:
    """The pollen poll shares the station base and its per-entry notice."""
    entry = _entry("Köniz", "2-309800")
    entry.add_to_hass(hass)
    coord = PollenCoordinator(hass, entry, async_get_clientsession(hass), "PBE")
    target = "custom_components.meteoswiss_weather.coordinator.fetch_pollen_current"

    with patch(target, side_effect=OgdParseError("bad")):
        await coord.async_refresh()
    assert coord.last_update_success is False
    issue = _issue(hass, "parse_error_pollen", entry)
    assert issue is not None
    assert issue.translation_placeholders == {"location": "Köniz"}

    with patch(target, return_value=object()):
        await coord.async_refresh()
    assert coord.last_update_success is True
    assert _issue(hass, "parse_error_pollen", entry) is None


@pytest.mark.parametrize("which", ["measurement", "daily"])
async def test_imis_parse_error_issue_is_per_entry(
    hass: HomeAssistant, which: str
) -> None:
    """Both IMIS paths post this entry's notice; a clean tick clears it."""
    entry = _entry("Weissfluhjoch", "imis-WFJ2")
    entry.add_to_hass(hass)
    coord = ImisCoordinator(
        hass, entry, async_get_clientsession(hass), "WFJ2", "Weissfluhjoch"
    )
    base = "custom_components.meteoswiss_weather.coordinator."
    measurement = SlfParseError("bad") if which == "measurement" else None
    daily = SlfParseError("bad") if which == "daily" else None

    with (
        patch(base + "fetch_imis_latest", side_effect=measurement),
        patch(base + "fetch_imis_daily_snow", side_effect=daily),
    ):
        await coord.async_refresh()
    issue = _issue(hass, "parse_error_imis", entry)
    assert issue is not None
    assert issue.translation_placeholders == {"location": "Weissfluhjoch"}

    with (
        # The auto-mocked fetch returns a stand-in measurement with a ``time``.
        patch(base + "fetch_imis_latest"),
        patch(base + "fetch_imis_daily_snow", return_value=None),
    ):
        await coord.async_refresh()
    assert _issue(hass, "parse_error_imis", entry) is None
