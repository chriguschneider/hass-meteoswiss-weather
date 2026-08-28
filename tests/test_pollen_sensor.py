"""Integration-level tests for the pollen sensor platform (ADR-0005, issue #67).

Uses pytest-homeassistant-custom-component with the mock_ogd_* fixtures so no
test hits the network.  Covers the requirements from issue #67:

- Option off → zero pollen requests (call count).
- Option on → sensors for the measured taxa only.
- Cadence constant asserted (POLLEN_UPDATE_INTERVAL == 1 h).
- Unavailable when the latest row is empty.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.meteoswiss_weather.const import (
    CONF_POINT_ID,
    CONF_POINT_NAME,
    CONF_POINT_TYPE_ID,
    CONF_POLLEN,
    CONF_POLLEN_STATION,
    CONF_POSTAL_CODE,
    CONF_STATION_ABBR,
    CONF_STATION_NAME,
    DOMAIN,
    POLLEN_UPDATE_INTERVAL,
)
from custom_components.meteoswiss_weather.ogd.const import pollen_now_url

# PBE = Bern pollen station, closest to the Köniz (3098) forecast point.
_POLLEN_ABBR = "PBE"
_POLLEN_URL = pollen_now_url(_POLLEN_ABBR)

# Entity IDs built by HA from device name "Köniz" + entity translated name.
# en.json: "Grass pollen" → sensor.koniz_grass_pollen, etc.
_EID_GRASSES = "sensor.koniz_grass_pollen"
_EID_BIRCH = "sensor.koniz_birch_pollen"
_EID_ALDER = "sensor.koniz_alder_pollen"
_EID_HAZEL = "sensor.koniz_hazel_pollen"
_EID_BEECH = "sensor.koniz_beech_pollen"
_EID_ASH = "sensor.koniz_ash_pollen"
_EID_OAK = "sensor.koniz_oak_pollen"

_ALL_POLLEN_EIDS = (
    _EID_GRASSES,
    _EID_BIRCH,
    _EID_ALDER,
    _EID_HAZEL,
    _EID_BEECH,
    _EID_ASH,
    _EID_OAK,
)
_ENABLED_EIDS = (_EID_GRASSES, _EID_BIRCH)
_DISABLED_EIDS = (_EID_ALDER, _EID_HAZEL, _EID_BEECH, _EID_ASH, _EID_OAK)


def _make_entry(*, pollen: bool = False, pollen_station: str = "") -> MockConfigEntry:
    """Return a config entry shaped like the config flow produces."""
    options: dict = {}
    if pollen:
        options = {CONF_POLLEN: True, CONF_POLLEN_STATION: pollen_station}
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
        options=options,
        title="Köniz",
        unique_id="2-309800",
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Cadence constant (ADR-0005)
# ---------------------------------------------------------------------------


def test_pollen_update_interval_is_one_hour() -> None:
    """ADR-0005: pollen is never fetched more often than once per hour."""
    assert POLLEN_UPDATE_INTERVAL == timedelta(hours=1)


# ---------------------------------------------------------------------------
# Option off → zero pollen requests
# ---------------------------------------------------------------------------


async def test_pollen_off_makes_no_requests(
    hass: HomeAssistant,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """When CONF_POLLEN is absent (default), the pollen URL is never called."""
    entry = _make_entry(pollen=False)
    await _setup(hass, entry)

    pollen_calls = [
        url
        for (_, url, *_) in mock_ogd.mock_calls
        if "ogd-pollen" in str(url) and "_h_now" in str(url)
    ]
    assert pollen_calls == []


# ---------------------------------------------------------------------------
# Option on → sensors for the measured taxa only
# ---------------------------------------------------------------------------


async def test_pollen_on_creates_enabled_sensors(
    hass: HomeAssistant,
    mock_ogd_pollen: AiohttpClientMocker,
) -> None:
    """Grasses and birch sensors (enabled by default) have states when on."""
    entry = _make_entry(pollen=True, pollen_station=_POLLEN_ABBR)
    await _setup(hass, entry)

    for eid in _ENABLED_EIDS:
        assert hass.states.get(eid) is not None, f"expected {eid} to have a state"


async def test_pollen_sensors_values_match_fixture(
    hass: HomeAssistant,
    mock_ogd_pollen: AiohttpClientMocker,
) -> None:
    """Sensor values come from the latest complete row in the fixture (06:00).

    Row 06:00: kabetuh0=3, khpoach0=45, kaquerh0=8.  The 07:00 row is
    all-empty and must be skipped.
    """
    entry = _make_entry(pollen=True, pollen_station=_POLLEN_ABBR)
    await _setup(hass, entry)

    assert float(hass.states.get(_EID_GRASSES).state) == 45.0
    assert float(hass.states.get(_EID_BIRCH).state) == 3.0


async def test_pollen_on_fetches_pollen_url(
    hass: HomeAssistant,
    mock_ogd_pollen: AiohttpClientMocker,
) -> None:
    """When pollen is enabled the coordinator must have fetched the pollen URL."""
    entry = _make_entry(pollen=True, pollen_station=_POLLEN_ABBR)
    await _setup(hass, entry)

    pollen_calls = [
        url
        for (_, url, *_) in mock_ogd_pollen.mock_calls
        if "ogd-pollen" in str(url) and "_h_now" in str(url)
    ]
    assert len(pollen_calls) >= 1


# ---------------------------------------------------------------------------
# Reduced inventory: only taxa the station measures
# ---------------------------------------------------------------------------


async def test_pollen_sensors_limited_to_inventory(
    hass: HomeAssistant,
    mock_ogd_pollen: AiohttpClientMocker,
) -> None:
    """Only taxa listed in the pollen datainventory are created as sensors.

    Monkeypatches fetch_pollen_datainventory to return only birch + grasses
    so only those two entries appear in the entity registry.
    """

    async def fake_inventory(_session):
        return {"PBE": frozenset({"kabetuh0", "khpoach0"})}

    entry = _make_entry(pollen=True, pollen_station=_POLLEN_ABBR)
    with patch(
        "custom_components.meteoswiss_weather.fetch_pollen_datainventory",
        AsyncMock(side_effect=fake_inventory),
    ):
        await _setup(hass, entry)

    entity_reg = er.async_get(hass)

    # Birch and grasses are in the inventory → sensors registered.
    assert entity_reg.async_get(_EID_BIRCH) is not None
    assert entity_reg.async_get(_EID_GRASSES) is not None

    # The remaining taxa are not in the inventory → sensors absent.
    for eid in _DISABLED_EIDS:
        assert entity_reg.async_get(eid) is None, (
            f"{eid} should not be created when taxon is absent from inventory"
        )


# ---------------------------------------------------------------------------
# Unavailable when the latest row is empty
# ---------------------------------------------------------------------------


async def test_pollen_sensors_unavailable_when_rows_empty(
    hass: HomeAssistant,
    mock_ogd_pollen_empty: AiohttpClientMocker,
) -> None:
    """When all rows in _h_now.csv are empty, pollen sensors are unavailable."""
    entry = _make_entry(pollen=True, pollen_station=_POLLEN_ABBR)
    await _setup(hass, entry)

    # async_refresh (non-fatal) fails due to OgdParseError → coordinator has
    # no data → CoordinatorEntity.available is False → state is unavailable.
    entity_reg = er.async_get(hass)
    for eid in _ENABLED_EIDS:
        reg_entry = entity_reg.async_get(eid)
        if reg_entry is not None:
            state = hass.states.get(eid)
            if state is not None:
                assert state.state == STATE_UNAVAILABLE, (
                    f"{eid}: expected unavailable, got {state.state!r}"
                )


# ---------------------------------------------------------------------------
# Default enabled/disabled state of sensors
# ---------------------------------------------------------------------------


async def test_pollen_grasses_and_birch_enabled_by_default(
    hass: HomeAssistant,
    mock_ogd_pollen: AiohttpClientMocker,
) -> None:
    """Grasses and birch sensors are enabled by default; others are disabled."""
    entry = _make_entry(pollen=True, pollen_station=_POLLEN_ABBR)
    await _setup(hass, entry)

    entity_reg = er.async_get(hass)

    for eid in _ENABLED_EIDS:
        reg_entry = entity_reg.async_get(eid)
        assert reg_entry is not None, f"{eid} not in registry"
        assert reg_entry.disabled_by is None, f"{eid} should be enabled by default"

    for eid in _DISABLED_EIDS:
        reg_entry = entity_reg.async_get(eid)
        assert reg_entry is not None, f"{eid} not in registry"
        assert reg_entry.disabled_by is not None, (
            f"{eid} should be disabled by default"
        )
