"""Integration-level tests for the MeteoSwiss Weather config and options flow.

Uses ``pytest-homeassistant-custom-component`` with the ``hass`` fixture.
The OGD metadata-load is patched at the ``_load_metadata`` boundary so that
``async_get_clientsession(hass)`` is never called in tests — this avoids the
pycares DNS-resolver background thread that aiohttp starts on session creation
and that PHCC's ``verify_cleanup`` fixture rejects.

Fixture data mirrors the real trimmed CSVs in tests/fixtures/ so that the
pure-OGD tests and these flow tests share the same reference data.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meteoswiss_weather.const import (
    CONF_HOURLY_FORECAST,
    CONF_HOURLY_HORIZON_DAYS,
    CONF_POINT_ID,
    CONF_POINT_NAME,
    CONF_POINT_TYPE_ID,
    CONF_POSTAL_CODE,
    CONF_STATION_ABBR,
    CONF_STATION_NAME,
    DEFAULT_HOURLY_HORIZON_DAYS,
    DOMAIN,
    HOURLY_HORIZON_FULL_RUN,
)
from custom_components.meteoswiss_weather.ogd import ForecastPoint, Station

# ---------------------------------------------------------------------------
# Reference data (mirrors the fixture CSVs)
# ---------------------------------------------------------------------------

_POINTS: list[ForecastPoint] = [
    ForecastPoint(309800, 2, "3098", "Köniz", 46.9245, 7.4147, 595.0),
    ForecastPoint(309801, 2, "3098", "Schliern b. Köniz", 46.91, 7.4, 620.0),
    ForecastPoint(800100, 2, "8001", "Zürich", 47.3769, 8.5417, 408.0),
    ForecastPoint(100300, 2, "1003", "Lausanne", 46.5197, 6.6323, 495.0),
    ForecastPoint(1, 1, "", "Bern / Zollikofen", 46.990765, 7.464061, 552.0),
]

_STATIONS: list[Station] = [
    Station("BER", "Bern / Zollikofen", "BE", 46.990765, 7.464061, 552.0),
    Station("ABO", "Adelboden", "BE", 46.491703, 7.560703, 1321.0),
    Station("PAY", "Payerne", "VD", 46.811798, 6.942381, 490.0),
    Station("SMA", "Zürich / Fluntern", "ZH", 47.377937, 8.565731, 555.0),
    Station("KLO", "Zürich / Kloten", "ZH", 47.479659, 8.535927, 426.0),
]

# Bern city-centre coordinates — nearest type-2 point is 3098 Köniz (309800).
_BERN_LAT = 46.948
_BERN_LON = 7.447

_FLOW = "custom_components.meteoswiss_weather.config_flow"


async def _mock_load_ok(self) -> bool:
    """Inject fixture data and signal success (no aiohttp session needed)."""
    self._all_points = _POINTS
    self._all_stations = _STATIONS
    return True


@pytest.fixture
def mock_ogd_functions():
    """Patch _load_metadata so no real aiohttp session is created."""
    with patch(f"{_FLOW}.MeteoSwissWeatherConfigFlow._load_metadata", _mock_load_ok):
        yield


# ---------------------------------------------------------------------------
# Happy path: two-point postal code goes through all three steps
# ---------------------------------------------------------------------------


async def test_happy_path_two_point_postal_code(
    hass: HomeAssistant, mock_ogd_functions
) -> None:
    """Full three-step flow for postal code 3098 (two forecast points)."""
    hass.config.latitude = _BERN_LAT
    hass.config.longitude = _BERN_LON

    # Step 1: user form appears, postal code pre-filled from HA location.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    # Submit postal code 3098 → two points → point-selection step.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POSTAL_CODE: 3098}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "point"

    # Step 2: pick the primary point (309800 Köniz).
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POINT_ID: 309800}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "station"

    # Step 3: accept the nearest station (BER — Bern is closest to Köniz).
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_STATION_ABBR: "BER"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Köniz"
    data = result["data"]
    assert data[CONF_POINT_ID] == 309800
    assert data[CONF_POINT_TYPE_ID] == 2
    assert data[CONF_POSTAL_CODE] == "3098"
    assert data[CONF_POINT_NAME] == "Köniz"
    assert data[CONF_STATION_ABBR] == "BER"
    assert data[CONF_STATION_NAME] == "Bern / Zollikofen"


# ---------------------------------------------------------------------------
# Single-point postal code: point step is skipped
# ---------------------------------------------------------------------------


async def test_single_point_postal_code_skips_point_step(
    hass: HomeAssistant, mock_ogd_functions
) -> None:
    """Postal code 8001 has one point — the point step must be skipped."""
    hass.config.latitude = 47.377
    hass.config.longitude = 8.542

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    # 8001 → one point → goes straight to station step.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POSTAL_CODE: 8001}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "station"

    # Nearest station to Zürich is SMA.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_STATION_ABBR: "SMA"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_POINT_ID] == 800100
    assert result["data"][CONF_POINT_TYPE_ID] == 2
    assert result["data"][CONF_STATION_ABBR] == "SMA"


# ---------------------------------------------------------------------------
# Radar cross-promotion hint (ADR-0003)
# ---------------------------------------------------------------------------


async def test_station_step_shows_radar_hint_when_not_installed(
    hass: HomeAssistant, mock_ogd_functions
) -> None:
    """The station step advertises the radar sibling when it is absent."""
    hass.config.latitude = 47.377
    hass.config.longitude = 8.542

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POSTAL_CODE: 8001}
    )
    assert result["step_id"] == "station"
    hint = result["description_placeholders"]["radar_hint"]
    assert "hass-meteoswiss-radar" in hint


async def test_station_step_hides_radar_hint_when_installed(
    hass: HomeAssistant, mock_ogd_functions
) -> None:
    """When the radar integration is loaded the hint placeholder is empty.

    It must still be present so the frontend never renders a literal
    ``{radar_hint}`` placeholder from the description string.
    """
    hass.config.latitude = 47.377
    hass.config.longitude = 8.542
    hass.config.components.add("meteoswiss_radar")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POSTAL_CODE: 8001}
    )
    assert result["step_id"] == "station"
    assert result["description_placeholders"] == {"radar_hint": ""}


# ---------------------------------------------------------------------------
# Duplicate-entry abort
# ---------------------------------------------------------------------------


async def test_duplicate_entry_aborts(
    hass: HomeAssistant, mock_ogd_functions
) -> None:
    """A second config entry for the same point is rejected as already_configured."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="2-309800",
        data={
            CONF_POINT_ID: 309800,
            CONF_POINT_TYPE_ID: 2,
            CONF_POSTAL_CODE: "3098",
            CONF_POINT_NAME: "Köniz",
            CONF_STATION_ABBR: "BER",
            CONF_STATION_NAME: "Bern / Zollikofen",
        },
        title="Köniz",
    )
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POSTAL_CODE: 3098}
    )
    # Point step: pick 309800 → unique_id matches → abort.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POINT_ID: 309800}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# cannot_connect: OGD metadata fetch failure
# ---------------------------------------------------------------------------


async def test_cannot_connect_on_metadata_failure(
    hass: HomeAssistant,
) -> None:
    """_load_metadata returning False shows the cannot_connect error."""

    async def _mock_load_fail(self) -> bool:
        return False

    with patch(
        f"{_FLOW}.MeteoSwissWeatherConfigFlow._load_metadata", _mock_load_fail
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


# ---------------------------------------------------------------------------
# unknown_postal_code
# ---------------------------------------------------------------------------


async def test_unknown_postal_code_shows_error(
    hass: HomeAssistant, mock_ogd_functions
) -> None:
    """A valid PLZ with no forecast points shows unknown_postal_code."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POSTAL_CODE: 9999}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "unknown_postal_code"}


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


async def test_options_flow_stores_hourly_flag(
    hass: HomeAssistant,
) -> None:
    """The options flow persists the hourly_forecast toggle."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="2-309800",
        data={
            CONF_POINT_ID: 309800,
            CONF_POINT_TYPE_ID: 2,
            CONF_POSTAL_CODE: "3098",
            CONF_POINT_NAME: "Köniz",
            CONF_STATION_ABBR: "BER",
            CONF_STATION_NAME: "Bern / Zollikofen",
        },
        title="Köniz",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert not entry.options.get(CONF_HOURLY_FORECAST, False)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"

    # Enabling the hourly forecast leads to the horizon step (issue #50).
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_HOURLY_FORECAST: True}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "hourly"
    # The horizon defaults to two days ahead.
    schema_default = result["data_schema"]({})
    assert schema_default[CONF_HOURLY_HORIZON_DAYS] == DEFAULT_HOURLY_HORIZON_DAYS

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_HOURLY_HORIZON_DAYS: 4}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOURLY_FORECAST] is True
    assert result["data"][CONF_HOURLY_HORIZON_DAYS] == 4


async def test_options_flow_hourly_off_skips_horizon_step(
    hass: HomeAssistant,
) -> None:
    """Leaving the hourly forecast off finishes at the init step (no horizon)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="2-309800",
        data={
            CONF_POINT_ID: 309800,
            CONF_POINT_TYPE_ID: 2,
            CONF_POSTAL_CODE: "3098",
            CONF_POINT_NAME: "Köniz",
            CONF_STATION_ABBR: "BER",
            CONF_STATION_NAME: "Bern / Zollikofen",
        },
        title="Köniz",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_HOURLY_FORECAST: False}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOURLY_FORECAST] is False
    # The horizon key is still written (with its default) but the hourly path
    # never uses it while the toggle is off.
    assert result["data"][CONF_HOURLY_HORIZON_DAYS] == DEFAULT_HOURLY_HORIZON_DAYS


async def test_options_flow_full_run_horizon(hass: HomeAssistant) -> None:
    """The "full run" sentinel can be selected as the horizon."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="2-309800",
        data={
            CONF_POINT_ID: 309800,
            CONF_POINT_TYPE_ID: 2,
            CONF_POSTAL_CODE: "3098",
            CONF_POINT_NAME: "Köniz",
            CONF_STATION_ABBR: "BER",
            CONF_STATION_NAME: "Bern / Zollikofen",
        },
        title="Köniz",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_HOURLY_FORECAST: True}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_HOURLY_HORIZON_DAYS: HOURLY_HORIZON_FULL_RUN},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOURLY_HORIZON_DAYS] == HOURLY_HORIZON_FULL_RUN
