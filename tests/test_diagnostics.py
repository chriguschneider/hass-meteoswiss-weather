"""Tests for diagnostics (issue #15).

Covers the config-entry diagnostics payload and the availability/resilience
contracts: entities flip to ``unavailable`` after a coordinator failure and
recover on the next successful update. Also covers repair-issue creation on
:class:`~ogd.OgdParseError` and deletion on the subsequent successful parse.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
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
from custom_components.meteoswiss_weather.ogd import OgdConnectionError, OgdParseError
from custom_components.meteoswiss_weather.ogd.const import station_now_url

_STATION_ABBR = "BER"
_WEATHER_ENTITY = "weather.koniz"
_TEMP_SENSOR = "sensor.koniz_temperature"


@pytest.fixture
def config_entry() -> MockConfigEntry:
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


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Diagnostics payload
# ---------------------------------------------------------------------------


async def test_diagnostics_payload(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """async_get_config_entry_diagnostics returns the expected structure."""
    from custom_components.meteoswiss_weather.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    await _setup(hass, config_entry)

    diag = await async_get_config_entry_diagnostics(hass, config_entry)

    # Entry section — postal_code is redacted.
    assert diag["entry"]["data"]["postal_code"] == "**REDACTED**"
    assert diag["entry"]["data"]["point_id"] == 309800
    assert diag["entry"]["options"] == {}

    # Point section — location fields are redacted.
    point = diag["point"]
    assert point["point_id"] == 309800
    assert point["postal_code"] == "**REDACTED**"
    assert point["lat"] == "**REDACTED**"
    assert point["lon"] == "**REDACTED**"

    assert diag["station_abbr"] == _STATION_ABBR

    # Station coordinator populated after a successful setup.
    sc = diag["station_coordinator"]
    assert sc["last_update_success"] is True
    assert sc["last_success"] is not None
    assert sc["last_exception"] is None

    # Forecast coordinator populated after a successful setup.
    fc = diag["forecast_coordinator"]
    assert fc["last_update_success"] is True
    assert fc["last_success"] is not None
    assert fc["last_run"] is not None
    assert fc["last_exception"] is None


async def test_diagnostics_after_station_failure(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """After a station error, last_update_success is False and last_exception is set."""
    from custom_components.meteoswiss_weather.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    await _setup(hass, config_entry)

    coordinator = config_entry.runtime_data.station_coordinator
    mock_ogd.clear_requests()
    mock_ogd.get(station_now_url(_STATION_ABBR), status=500)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    sc = diag["station_coordinator"]
    assert sc["last_update_success"] is False
    assert sc["last_exception"] is not None


# ---------------------------------------------------------------------------
# Availability: weather entity
# ---------------------------------------------------------------------------


async def test_weather_unavailable_after_station_failure(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """Weather entity becomes ``unavailable`` when the station coordinator fails."""
    await _setup(hass, config_entry)
    assert hass.states.get(_WEATHER_ENTITY).state != "unavailable"

    coordinator = config_entry.runtime_data.station_coordinator
    mock_ogd.clear_requests()
    mock_ogd.get(station_now_url(_STATION_ABBR), status=503)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(_WEATHER_ENTITY).state == "unavailable"


async def test_weather_recovers_after_station_failure(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """Weather entity becomes available again after the station coordinator recovers."""
    from datetime import UTC, datetime

    from custom_components.meteoswiss_weather.ogd import Observation

    await _setup(hass, config_entry)

    coordinator = config_entry.runtime_data.station_coordinator
    mock_ogd.clear_requests()
    mock_ogd.get(station_now_url(_STATION_ABBR), status=503)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(_WEATHER_ENTITY).state == "unavailable"

    # Inject a successful observation directly to confirm recovery.
    obs = Observation(
        station_abbr=_STATION_ABBR,
        timestamp=datetime(2026, 8, 27, 0, 40, tzinfo=UTC),
        temperature=19.5,
    )
    coordinator.async_set_updated_data(obs)
    await hass.async_block_till_done()

    assert hass.states.get(_WEATHER_ENTITY).state != "unavailable"


# ---------------------------------------------------------------------------
# Availability: sensor entity
# ---------------------------------------------------------------------------


async def test_sensor_unavailable_after_station_failure(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """Sensor entities become ``unavailable`` when the station coordinator fails."""
    await _setup(hass, config_entry)
    assert hass.states.get(_TEMP_SENSOR).state != "unavailable"

    coordinator = config_entry.runtime_data.station_coordinator
    mock_ogd.clear_requests()
    mock_ogd.get(station_now_url(_STATION_ABBR), status=503)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(_TEMP_SENSOR).state == "unavailable"


async def test_sensor_recovers_after_station_failure(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """Sensor entities recover once the station coordinator succeeds again."""
    from datetime import UTC, datetime

    from custom_components.meteoswiss_weather.ogd import Observation

    await _setup(hass, config_entry)

    coordinator = config_entry.runtime_data.station_coordinator
    mock_ogd.clear_requests()
    mock_ogd.get(station_now_url(_STATION_ABBR), status=503)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(_TEMP_SENSOR).state == "unavailable"

    # Inject a successful observation directly to confirm recovery.
    obs = Observation(
        station_abbr=_STATION_ABBR,
        timestamp=datetime(2026, 8, 27, 0, 40, tzinfo=UTC),
        temperature=19.5,
    )
    coordinator.async_set_updated_data(obs)
    await hass.async_block_till_done()

    assert hass.states.get(_TEMP_SENSOR).state != "unavailable"


# ---------------------------------------------------------------------------
# Repair issues for OgdParseError
# ---------------------------------------------------------------------------


async def test_station_parse_error_creates_repair_issue(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A station OgdParseError creates a HA repair issue."""
    await _setup(hass, config_entry)

    coordinator = config_entry.runtime_data.station_coordinator

    with patch(
        "custom_components.meteoswiss_weather.coordinator.fetch_current",
        side_effect=OgdParseError("bad format"),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    issue_reg = ir.async_get(hass)
    assert issue_reg.async_get_issue(DOMAIN, "parse_error_station") is not None


async def test_station_parse_error_repair_issue_cleared_on_success(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """The station parse-error repair issue is deleted when parsing succeeds again."""
    await _setup(hass, config_entry)

    coordinator = config_entry.runtime_data.station_coordinator

    # Trigger the repair issue.
    with patch(
        "custom_components.meteoswiss_weather.coordinator.fetch_current",
        side_effect=OgdParseError("bad format"),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    issue_reg = ir.async_get(hass)
    assert issue_reg.async_get_issue(DOMAIN, "parse_error_station") is not None

    # A successful refresh clears it.
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert issue_reg.async_get_issue(DOMAIN, "parse_error_station") is None


async def test_forecast_parse_error_creates_repair_issue(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A forecast OgdParseError creates a HA repair issue."""
    await _setup(hass, config_entry)

    coordinator = config_entry.runtime_data.forecast_coordinator

    with patch(
        "custom_components.meteoswiss_weather.coordinator.latest_run",
        side_effect=OgdParseError("stac broken"),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    issue_reg = ir.async_get(hass)
    assert issue_reg.async_get_issue(DOMAIN, "parse_error_forecast") is not None


async def test_forecast_parse_error_repair_issue_cleared_on_success(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """The forecast parse-error repair issue is deleted when parsing succeeds again."""
    await _setup(hass, config_entry)

    coordinator = config_entry.runtime_data.forecast_coordinator

    with patch(
        "custom_components.meteoswiss_weather.coordinator.latest_run",
        side_effect=OgdParseError("stac broken"),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    issue_reg = ir.async_get(hass)
    assert issue_reg.async_get_issue(DOMAIN, "parse_error_forecast") is not None

    # A successful refresh clears it.
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert issue_reg.async_get_issue(DOMAIN, "parse_error_forecast") is None


async def test_forecast_connection_error_creates_no_repair_issue(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A forecast OgdConnectionError fails the update without a repair issue.

    Transient connection errors rely on the coordinator's built-in back-off;
    only structural parse errors post a repair issue (see coordinator.py).
    """
    await _setup(hass, config_entry)

    coordinator = config_entry.runtime_data.forecast_coordinator

    with patch(
        "custom_components.meteoswiss_weather.coordinator.latest_run",
        side_effect=OgdConnectionError("stac unreachable"),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    issue_reg = ir.async_get(hass)
    assert issue_reg.async_get_issue(DOMAIN, "parse_error_forecast") is None


async def test_forecast_daily_parse_error_creates_repair_issue(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A parse error while fetching the daily files creates the repair issue."""
    await _setup(hass, config_entry)

    coordinator = config_entry.runtime_data.forecast_coordinator
    # Clear the cached run so the daily-download branch runs again.
    coordinator.last_run = None

    with patch.object(
        coordinator._backend, "fetch_daily", side_effect=OgdParseError("bad daily")
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    issue_reg = ir.async_get(hass)
    assert issue_reg.async_get_issue(DOMAIN, "parse_error_forecast") is not None


async def test_forecast_daily_connection_error_creates_no_repair_issue(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A connection error while fetching the daily files posts no repair issue."""
    await _setup(hass, config_entry)

    coordinator = config_entry.runtime_data.forecast_coordinator
    coordinator.last_run = None

    with patch.object(
        coordinator._backend,
        "fetch_daily",
        side_effect=OgdConnectionError("daily unreachable"),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    issue_reg = ir.async_get(hass)
    assert issue_reg.async_get_issue(DOMAIN, "parse_error_forecast") is None


async def test_forecast_hourly_parse_error_creates_repair_issue(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A parse error in the lazy hourly fetch creates the repair issue (#54)."""
    from datetime import UTC, datetime

    await _setup(hass, config_entry)

    # The lazy provider is where hourly I/O happens now; enable it and drive it.
    provider = config_entry.runtime_data.forecast_coordinator.hourly_provider
    provider._enabled = True

    with patch.object(
        provider._backend, "fetch_hourly", side_effect=OgdParseError("bad hourly")
    ):
        await provider.async_get_hourly(datetime(2026, 8, 27, 2, 0, tzinfo=UTC))
        await hass.async_block_till_done()

    issue_reg = ir.async_get(hass)
    assert issue_reg.async_get_issue(DOMAIN, "parse_error_forecast") is not None


async def test_forecast_hourly_connection_error_creates_no_repair_issue(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A connection error in the lazy hourly fetch posts no repair issue (#54)."""
    from datetime import UTC, datetime

    await _setup(hass, config_entry)

    provider = config_entry.runtime_data.forecast_coordinator.hourly_provider
    provider._enabled = True

    with patch.object(
        provider._backend,
        "fetch_hourly",
        side_effect=OgdConnectionError("hourly unreachable"),
    ):
        # The provider swallows the transient error, keeping last-good (None).
        assert (
            await provider.async_get_hourly(datetime(2026, 8, 27, 2, 0, tzinfo=UTC))
            is None
        )
        await hass.async_block_till_done()

    issue_reg = ir.async_get(hass)
    assert issue_reg.async_get_issue(DOMAIN, "parse_error_forecast") is None
