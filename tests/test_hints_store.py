"""Persisting the fetch ladder's per-file hints across restarts (issue #133).

The backend learns each forecast file's layout and byte positions and keeps
them in memory (ADR-0008, issue #121); losing them on every restart or reload
makes the next refresh cold. These tests exercise the integration glue that
persists them with ``helpers.storage.Store``: restore at setup, a debounced
save when they change, removal with the entry, and the diagnostics flag. The
pure serialisation and "garbage costs a fresh look" behaviour is covered in
``test_ogd_hourly.py``; here the concern is the wiring, not the ladder.
"""

from __future__ import annotations

from datetime import date

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    flush_store,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.meteoswiss_weather.const import (
    CONF_POINT_ID,
    CONF_POINT_NAME,
    CONF_POINT_TYPE_ID,
    CONF_POSTAL_CODE,
    CONF_STATION_ABBR,
    CONF_STATION_NAME,
    DOMAIN,
    HINTS_STORAGE_KEY,
    HINTS_STORAGE_VERSION,
)
from custom_components.meteoswiss_weather.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.meteoswiss_weather.ogd import FileLayout
from custom_components.meteoswiss_weather.ogd.hourly import FileHint, RowGeometry

_STATION_ABBR = "BER"

# A hint for a parameter the *default* (daily-only) refresh never fetches, so a
# real fetch during setup cannot overwrite or clear it — what we seed is exactly
# what the backend should still hold afterwards.
_UNFETCHED_PARAM = "tre200h0"


def _a_hint() -> FileHint:
    return FileHint(
        layout=FileLayout.DATE_MAJOR,
        utc_day=date(2026, 8, 27),
        header="point_id;point_type_id;Date;tre200h0\n",
        geometry=RowGeometry(
            block_bytes=1500.0, row_offset=64,
            anchor_stamp="202608270300", anchor_offset=120,
        ),
        first_stamp="202608262100",
        last_stamp="202608272000",
    )


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A default (daily-only) config entry, shaped like the config flow."""
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


def _store_key(entry: MockConfigEntry) -> str:
    return f"{HINTS_STORAGE_KEY}.{entry.entry_id}"


def _seed(hass_storage, entry: MockConfigEntry, data: object) -> None:
    """Pre-seed the entry's hint store with ``data`` as if written last run."""
    key = _store_key(entry)
    hass_storage[key] = {
        "version": HINTS_STORAGE_VERSION,
        "minor_version": 1,
        "key": key,
        "data": data,
    }


async def _flush_delayed_save(entry: MockConfigEntry) -> None:
    """Force the coordinator's debounced hint save to disk."""
    store = entry.runtime_data.forecast_coordinator._hints_store
    assert store is not None
    await flush_store(store)


async def test_hints_restored_at_setup(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
    hass_storage,
) -> None:
    """A stored hint is imported into the backend before the first refresh, and
    the coordinator flags that a warm start happened (issue #133)."""
    _seed(hass_storage, config_entry, {_UNFETCHED_PARAM: _a_hint().to_dict()})
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    runtime = config_entry.runtime_data
    assert runtime.forecast_coordinator.hints_restored is True
    # The seeded hint is still there (the default refresh never fetches it).
    assert runtime.backend.export_hints()[_UNFETCHED_PARAM] == _a_hint().to_dict()


async def test_corrupt_store_is_ignored(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
    hass_storage,
) -> None:
    """A corrupt store must not break setup and must not count as restored: it
    costs a fresh look, never an error (issue #133 acceptance)."""
    _seed(
        hass_storage,
        config_entry,
        {_UNFETCHED_PARAM: {"layout": "not_a_real_layout"}},
    )
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data.forecast_coordinator.hints_restored is False


async def test_hints_saved_when_they_change(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
    hass_storage,
) -> None:
    """A refresh that changed the backend's hints writes them to storage,
    debounced, so the next restart is warm (issue #133)."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    runtime = config_entry.runtime_data
    # Simulate the backend learning a position, then a coordinator tick.
    runtime.backend._hints = {_UNFETCHED_PARAM: _a_hint()}
    await runtime.forecast_coordinator.async_refresh()
    await hass.async_block_till_done()
    await _flush_delayed_save(config_entry)

    stored = hass_storage[_store_key(config_entry)]["data"]
    assert stored[_UNFETCHED_PARAM] == _a_hint().to_dict()


async def test_store_removed_with_the_entry(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
    hass_storage,
) -> None:
    """Removing the config entry drops its hint store (issue #133)."""
    _seed(hass_storage, config_entry, {_UNFETCHED_PARAM: _a_hint().to_dict()})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert _store_key(config_entry) in hass_storage

    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()

    assert _store_key(config_entry) not in hass_storage


async def test_diagnostics_report_restore(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
    hass_storage,
) -> None:
    """Diagnostics say whether hints were restored, without dumping them."""
    _seed(hass_storage, config_entry, {_UNFETCHED_PARAM: _a_hint().to_dict()})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    fc = diag["forecast_coordinator"]
    assert fc["hints_restored"] is True
    # The raw hints are never dumped — the store section lists parameters, not
    # byte offsets, and there is no key that carries the hint objects.
    assert "hints" not in fc
