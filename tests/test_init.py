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
    DAILY_BLOCK_PARAMS,
    DAILY_REQUIRED_PARAMS,
    HOURLY_PRECIP_PROBABILITY,
    HOURLY_REQUIRED_PARAMS,
    HOURLY_SYMBOL,
    HOURLY_ZERO_DEGREE,
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


# Hourly-only params: the files fetched exclusively by the opt-in hourly
# forecast and never by the default daily refresh. The wind files (issue #60),
# the rp0003i0 probability file (issue #112) and the zero-degree file (issue
# #107) are point-major block-fetched on every daily refresh, so they are
# tracked separately by _block_calls() below.
_HOURLY_ONLY_PARAMS = tuple(
    p for p in HOURLY_REQUIRED_PARAMS if p not in DAILY_BLOCK_PARAMS
)


def _hourly_calls(aioclient_mock: AiohttpClientMocker) -> int:
    """Opt-in hourly-forecast file downloads (excludes daily-wind files).

    Counts only the hourly-only parameters so tests that verify the lazy
    hourly behaviour are not confused by the daily block fetch (issue #60).
    """
    suffixes = tuple(f"{_RUN_TS}.{param}.csv" for param in _HOURLY_ONLY_PARAMS)
    return sum(
        1
        for _method, url, *_ in aioclient_mock.mock_calls
        if url.path.endswith(suffixes)
    )


def _block_calls(aioclient_mock: AiohttpClientMocker, param: str) -> int:
    """Requests to one point-major block file (daily block fetch, #60/#107).

    The mock answers every Range probe with the whole file, so the reader
    caches it after the first request and one fetch is exactly one call.
    """
    suffix = f"{_RUN_TS}.{param}.csv"
    return sum(
        1
        for _method, url, *_ in aioclient_mock.mock_calls
        if url.path.endswith(suffix)
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
    # The daily refresh block-fetches rp0003i0 once for the probability field
    # (issue #112), next to the wind blocks — with the hourly option off.
    assert _block_calls(mock_ogd, HOURLY_PRECIP_PROBABILITY) == 1
    # Hourly is off by default (ADR-0002): the refresher demands nothing and
    # never fetches.
    assert runtime.forecast_coordinator.hourly_refresher.last_fetch is None
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

    # The refresher demands nothing while the option is off (ADR-0008 §2).
    refresher = coordinator.hourly_refresher
    assert refresher.demanded_params == ()
    assert coordinator.hourly_forecast() is None
    assert _hourly_calls(mock_ogd) == 0
    assert refresher.last_fetch is None


async def test_hourly_fetched_eagerly_at_setup(
    hass: HomeAssistant,
    hourly_config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """With the option on, the first refresh fills the store — no subscriber (#124).

    ADR-0008 removes the lazy provider: the demanded hourly files are fetched as
    part of the coordinator's own refresh, whether or not anything subscribes, so
    the store holds the hourly series after setup and ``hourly_forecast`` builds
    from it.
    """
    with freeze_time(datetime(2026, 8, 27, 0, 0, tzinfo=UTC)):
        hourly_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(hourly_config_entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hourly_config_entry.runtime_data.forecast_coordinator
        # The hourly-only files were downloaded eagerly, and the store now holds
        # the series (acceptance: store filled after the first refresh).
        assert _hourly_calls(mock_ogd) > 0
        assert coordinator.hourly_refresher.last_fetch is not None
        assert coordinator.store.get(HOURLY_SYMBOL) is not None
        hourly = coordinator.hourly_forecast()
        assert hourly is not None and len(hourly) == 24


def _tre_calls(aioclient_mock: AiohttpClientMocker) -> int:
    """Downloads of the date-major temperature file (near/far tier, issue #68)."""
    suffix = f"{_RUN_TS}.tre200h0.csv"
    return sum(
        1
        for _method, url, *_ in aioclient_mock.mock_calls
        if url.path.endswith(suffix)
    )


async def test_hourly_tiers_fetch_eagerly_via_coordinator(
    hass: HomeAssistant,
    hourly_config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """The coordinator's own ticks drive the near/far tiers; the cache serves rest.

    The date-major temperature file is downloaded once per run as part of the
    coordinator's refresh: a due tier still asks the backend, but the shared
    per-run cache serves an unchanged run without a second ~10 MB download (issue
    #68, ADR-0002 revision 2; ADR-0008, issue #123). No card or service call is
    involved — the fetch is eager (issue #124).
    """
    # Freeze at 00:00 UTC so all 24 fixture hours (starting 00:00) are current
    # and the horizon_start trim does not drop any of them (issue #92).
    start = datetime(2026, 8, 27, 0, 0, tzinfo=UTC)

    with freeze_time(start) as frozen:
        hourly_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(hourly_config_entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hourly_config_entry.runtime_data.forecast_coordinator

        # Setup already fetched the far tier (stale, never fetched) once, plus
        # the point-major group; the store holds the full 24 hours.
        assert _tre_calls(mock_ogd) == 1
        assert coordinator.hourly_forecast() is not None
        assert len(coordinator.hourly_forecast()) == 24

        # A second coordinator tick an hour later, same run, no tier due: the
        # temperature file is not refetched.
        frozen.move_to(start + timedelta(hours=1))
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert _tre_calls(mock_ogd) == 1

        # Past the far fallback (6 h) the far tier goes stale and asks the
        # backend again. The discovered run has not moved (the STAC mock returns
        # one run) and a run's file is immutable, so the shared per-run cache
        # serves the temperature file without a second download (ADR-0008,
        # issue #123).
        frozen.move_to(start + HOURLY_FAR_MAX_AGE + timedelta(seconds=1))
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert _tre_calls(mock_ogd) == 1


def _symbol_calls(aioclient_mock: AiohttpClientMocker) -> int:
    """Downloads of the point-major weather-symbol file (the canary skips these)."""
    suffix = f"{_RUN_TS}.{HOURLY_SYMBOL}.csv"
    return sum(
        1
        for _method, url, *_ in aioclient_mock.mock_calls
        if url.path.endswith(suffix)
    )


async def test_new_run_unchanged_canary_confirms_without_refetch(
    hass: HomeAssistant,
    hourly_config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A new run whose content is unchanged confirms the store, no group refetch.

    The canary decides now (ADR-0008 section 3, owner decision 2, issue #125):
    the fixture serves one run, so a bumped run stamp presents the same content.
    Its coming hours match the store, so no group is re-downloaded — the symbol
    file is not re-fetched — and the stored series read as current for the new
    run (confirmed), not stale.
    """
    from dataclasses import replace

    with freeze_time(datetime(2026, 8, 27, 2, 0, tzinfo=UTC)):
        hass.states.async_set("sun.sun", "above_horizon")
        hourly_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(hourly_config_entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hourly_config_entry.runtime_data.forecast_coordinator
        symbol_after_setup = _symbol_calls(mock_ogd)
        assert symbol_after_setup == 1  # fetched once at setup

        # A new run (same fixture files, bumped stamp): the canary sees no change.
        new_run = replace(
            coordinator.run, timestamp=coordinator.run.timestamp + timedelta(hours=3)
        )
        assert await coordinator.hourly_refresher.async_refresh(new_run) is False
        await hass.async_block_till_done()

        # The point-major symbol file was not downloaded again — the canary skip.
        assert _symbol_calls(mock_ogd) == symbol_after_setup
        # ... and the stored series now reads as current for the new run.
        series = coordinator.store.get(HOURLY_SYMBOL)
        assert series is not None
        assert series.provenance.run == new_run.timestamp
        assert series.provenance.confirmed is True
        assert not coordinator.store.is_stale(HOURLY_SYMBOL, new_run.timestamp)


async def test_zero_degree_block_fetched_with_daily_refresh_hourly_off(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """The zero-degree block rides along with the daily refresh (issue #107).

    With the hourly option off, no card open and no ``get_forecasts`` call,
    the first refresh already carries the zero-degree levels by hour; the
    hourly-only files are still never touched.
    """
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = config_entry.runtime_data.forecast_coordinator
    series = coordinator.store.get(HOURLY_ZERO_DEGREE)
    assert series is not None
    # Fixture: 24 hours for Köniz, 2500 m at 00:00 UTC rising 5 m per hour.
    assert len(series.values) == 24
    assert series.values[datetime(2026, 8, 27, 0, 0, tzinfo=UTC)] == 2500.0
    assert series.provenance.source == "daily"
    assert not coordinator.store.is_stale(HOURLY_ZERO_DEGREE, coordinator.last_run)
    assert _block_calls(mock_ogd, HOURLY_ZERO_DEGREE) == 1
    assert _hourly_calls(mock_ogd) == 0


async def test_zero_degree_block_not_fetched_twice_with_hourly_on(
    hass: HomeAssistant,
    hourly_config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """With the hourly option on, the eager hourly fetch reuses the daily block.

    The daily refresh fetches the zero-degree block for the run; the refresher's
    point-major fetch for the same run — in the same tick — folds in the cached
    text instead of downloading the file again (issue #107, the #60/#123
    cache-sharing contract).
    """
    with freeze_time(datetime(2026, 8, 27, 0, 0, tzinfo=UTC)):
        hourly_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(hourly_config_entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hourly_config_entry.runtime_data.forecast_coordinator
        hourly = coordinator.hourly_forecast()

    assert _block_calls(mock_ogd, HOURLY_ZERO_DEGREE) == 1
    assert hourly is not None and len(hourly) == 24
    # The hourly forecast still carries the value, sourced from the shared block.
    assert hourly[0].zero_degree_level == 2500.0


async def test_options_change_reloads_entry(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """Turning the hourly option on reloads the entry and fetches eagerly (#124).

    The reload rebuilds the coordinator with hourly on; its first refresh then
    fills the store, no card open and no service call (ADR-0008).
    """
    with freeze_time(datetime(2026, 8, 27, 0, 0, tzinfo=UTC)):
        config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        assert _hourly_calls(mock_ogd) == 0
        coordinator = config_entry.runtime_data.forecast_coordinator
        assert coordinator.hourly_refresher.enabled is False

        hass.config_entries.async_update_entry(
            config_entry, options={CONF_HOURLY_FORECAST: True}
        )
        await hass.async_block_till_done()

        assert config_entry.state is ConfigEntryState.LOADED
        # The reload rebuilt the coordinator with hourly on and its first refresh
        # fetched the demanded files eagerly.
        refresher = config_entry.runtime_data.forecast_coordinator.hourly_refresher
        assert refresher.enabled is True
        assert _hourly_calls(mock_ogd) > 0
        assert refresher.last_fetch is not None


async def test_setup_uses_day_item_not_listing(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """The coordinator discovers the run via the day item, not the full listing.

    The day item (~80 KB, ETag-capable) is the primary discovery path (issue
    #120, ADR-0008). A setup that hits the cheaper day item and never fetches
    the 600 KB listing is the expected behaviour. The run is handed down to the
    backend so nothing below re-discovers it (ADR-0008 section 3).
    """
    from datetime import UTC, datetime

    from custom_components.meteoswiss_weather.ogd.const import (
        COLLECTION_FORECAST,
        stac_day_item_url,
        stac_items_url,
    )

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    today_id = datetime.now(UTC).strftime("%Y%m%d") + "-ch"
    day_item = stac_day_item_url(COLLECTION_FORECAST, today_id)
    listing = stac_items_url(COLLECTION_FORECAST)

    day_item_calls = [1 for _m, url, *_ in mock_ogd.mock_calls if str(url) == day_item]
    listing_calls = [1 for _m, url, *_ in mock_ogd.mock_calls if str(url) == listing]

    assert len(day_item_calls) == 1, "day item must be fetched exactly once"
    assert len(listing_calls) == 0, "listing must not be fetched when day item succeeds"
    assert config_entry.runtime_data.forecast_coordinator.run is not None
