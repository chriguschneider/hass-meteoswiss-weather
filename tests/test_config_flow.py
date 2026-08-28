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

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meteoswiss_weather.const import (
    CONF_HISTORY_ACTION,
    CONF_HOURLY_CLOUD_LAYERS,
    CONF_HOURLY_FORECAST,
    CONF_HOURLY_HORIZON_DAYS,
    CONF_HOURLY_TEMP_PERCENTILES,
    CONF_POINT_ID,
    CONF_POINT_NAME,
    CONF_POINT_TYPE_ID,
    CONF_POLLEN,
    CONF_POLLEN_STATION,
    CONF_POSTAL_CODE,
    CONF_STATION_ABBR,
    CONF_STATION_NAME,
    DEFAULT_HOURLY_HORIZON_DAYS,
    DOMAIN,
    HISTORY_BACKFILL,
    HISTORY_DISCARD,
    HISTORY_KEEP,
    HOURLY_HORIZON_FULL_RUN,
)
from custom_components.meteoswiss_weather.ogd import (
    ForecastPoint,
    OgdError,
    PollenStation,
    Station,
)

# ---------------------------------------------------------------------------
# Reference data (mirrors the fixture CSVs)
# ---------------------------------------------------------------------------

_POINTS: list[ForecastPoint] = [
    ForecastPoint(309800, 2, "3098", "Köniz", 46.9245, 7.4147, 595.0),
    ForecastPoint(309801, 2, "3098", "Schliern b. Köniz", 46.91, 7.4, 620.0),
    ForecastPoint(800100, 2, "8001", "Zürich", 47.3769, 8.5417, 408.0),
    ForecastPoint(100300, 2, "1003", "Lausanne", 46.5197, 6.6323, 495.0),
    ForecastPoint(1, 1, "", "Bern / Zollikofen", 46.990765, 7.464061, 552.0),
    # Mountain points (type 3) — mirrors the trimmed fixture.
    ForecastPoint(5000, 3, "", "Jungfraujoch", 46.5475, 7.9855, 3571.0),
    ForecastPoint(5001, 3, "", "Säntis", 47.2491, 9.3413, 2502.0),
    ForecastPoint(5002, 3, "", "Titlis", 46.7723, 8.4277, 3238.0),
    ForecastPoint(5003, 3, "", "Gorner Glacier", 46.0825, 7.7935, 3089.0),
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

# Flow-internal constants (must match config_flow.py).
_CONF_MODE = "forecast_mode"
_MODE_POSTAL_CODE = "postal_code"
_MODE_MOUNTAIN = "mountain"


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
    """Full flow for postal code 3098 (two forecast points) via postal-code mode."""
    hass.config.latitude = _BERN_LAT
    hass.config.longitude = _BERN_LON

    # Step 1: mode selection form appears.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    # Select postal-code mode.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={_CONF_MODE: _MODE_POSTAL_CODE}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "postal_code"

    # Submit postal code 3098 → two points → point-selection step.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POSTAL_CODE: 3098}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "point"

    # Step: pick the primary point (309800 Köniz).
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POINT_ID: 309800}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "station"

    # Final step: accept the nearest station (BER — Bern is closest to Köniz).
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

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={_CONF_MODE: _MODE_POSTAL_CODE}
    )
    assert result["step_id"] == "postal_code"

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
        result["flow_id"], user_input={_CONF_MODE: _MODE_POSTAL_CODE}
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
        result["flow_id"], user_input={_CONF_MODE: _MODE_POSTAL_CODE}
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
        result["flow_id"], user_input={_CONF_MODE: _MODE_POSTAL_CODE}
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
        result["flow_id"], user_input={_CONF_MODE: _MODE_POSTAL_CODE}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POSTAL_CODE: 9999}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "postal_code"
    assert result["errors"] == {"base": "unknown_postal_code"}


# ---------------------------------------------------------------------------
# Mountain path: end-to-end setup flow
# ---------------------------------------------------------------------------


async def test_mountain_path_happy(
    hass: HomeAssistant, mock_ogd_functions
) -> None:
    """Full mountain-path flow: mode → mountain dropdown → station → entry."""
    # Use Bern coordinates — nearest mountain point is Jungfraujoch (5000).
    hass.config.latitude = _BERN_LAT
    hass.config.longitude = _BERN_LON

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "user"

    # Select mountain mode.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={_CONF_MODE: _MODE_MOUNTAIN}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "mountain"

    # The mountain dropdown must list all type-3 points from _POINTS (4 total),
    # sorted by name: Gorner Glacier, Jungfraujoch, Säntis, Titlis.
    options = result["data_schema"].schema[CONF_POINT_ID].config["options"]
    option_labels = [o["label"] for o in options]
    assert option_labels == [
        "Gorner Glacier (3089 m)",
        "Jungfraujoch (3571 m)",
        "Säntis (2502 m)",
        "Titlis (3238 m)",
    ]

    # Pick Jungfraujoch (point_id 5000); submit as string (SelectSelector value).
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POINT_ID: "5000"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "station"

    # The three nearest stations to Jungfraujoch are ABO, BER, PAY.
    # Accept whichever HA offers first (ABO is closest at ~1321 m altitude).
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_STATION_ABBR: "ABO"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Jungfraujoch"
    data = result["data"]
    assert data[CONF_POINT_ID] == 5000
    assert data[CONF_POINT_TYPE_ID] == 3
    assert data[CONF_POSTAL_CODE] == ""
    assert data[CONF_POINT_NAME] == "Jungfraujoch"
    assert data[CONF_STATION_ABBR] == "ABO"
    assert data[CONF_STATION_NAME] == "Adelboden"


async def test_mountain_unique_id(
    hass: HomeAssistant, mock_ogd_functions
) -> None:
    """Mountain entry unique_id is '3-<point_id>'."""
    hass.config.latitude = _BERN_LAT
    hass.config.longitude = _BERN_LON

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={_CONF_MODE: _MODE_MOUNTAIN}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POINT_ID: "5002"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_STATION_ABBR: "ABO"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    # HA stores the unique_id on the entry.
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].unique_id == "3-5002"


async def test_mountain_duplicate_aborts(
    hass: HomeAssistant, mock_ogd_functions
) -> None:
    """A second mountain entry for the same point is rejected."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="3-5000",
        data={
            CONF_POINT_ID: 5000,
            CONF_POINT_TYPE_ID: 3,
            CONF_POSTAL_CODE: "",
            CONF_POINT_NAME: "Jungfraujoch",
            CONF_STATION_ABBR: "ABO",
            CONF_STATION_NAME: "Adelboden",
        },
        title="Jungfraujoch",
    )
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={_CONF_MODE: _MODE_MOUNTAIN}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POINT_ID: "5000"}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_mountain_nearest_preselected(
    hass: HomeAssistant, mock_ogd_functions
) -> None:
    """Nearest mountain point to the HA location is the dropdown default."""
    # Säntis is closest to north-eastern Switzerland (Säntis lat/lon).
    hass.config.latitude = 47.25
    hass.config.longitude = 9.34

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={_CONF_MODE: _MODE_MOUNTAIN}
    )
    assert result["step_id"] == "mountain"
    # The schema default should be the point_id of Säntis (5001) as a string.
    default = result["data_schema"]({}).get(CONF_POINT_ID)
    assert default == "5001"


async def test_mountain_daily_fixture_carries_type3_rows(
    hass: HomeAssistant,
) -> None:
    """Smoke test: the daily fixture files contain rows for the mountain point."""
    import csv
    import io
    from pathlib import Path

    fixture_dir = Path(__file__).parent / "fixtures"
    for param in ("tre200px", "tre200pn", "rka150p0", "jp2000d0"):
        path = fixture_dir / f"vnut12.lssw.202608270200.{param}.csv"
        text = path.read_text()
        reader = csv.DictReader(io.StringIO(text), delimiter=";")
        type3_rows = [r for r in reader if r["point_type_id"] == "3"]
        assert type3_rows, f"{param} fixture has no type-3 rows"
        # The Jungfraujoch point must be present.
        jfj_rows = [r for r in type3_rows if r["point_id"] == "5000"]
        assert jfj_rows, f"{param} fixture has no Jungfraujoch (5000) rows"


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


async def test_options_flow_hourly_step_gated_additions(
    hass: HomeAssistant,
) -> None:
    """The hourly step carries the B9/B11 toggles and persists them (issue #69)."""
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

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_HOURLY_FORECAST: True}
    )
    assert result["step_id"] == "hourly"
    # Both gated toggles default off (ADR-0002 gating; the expensive path).
    schema_default = result["data_schema"]({})
    assert schema_default[CONF_HOURLY_CLOUD_LAYERS] is False
    assert schema_default[CONF_HOURLY_TEMP_PERCENTILES] is False

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_HOURLY_HORIZON_DAYS: 2,
            CONF_HOURLY_CLOUD_LAYERS: True,
            CONF_HOURLY_TEMP_PERCENTILES: False,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOURLY_CLOUD_LAYERS] is True
    assert result["data"][CONF_HOURLY_TEMP_PERCENTILES] is False


async def test_options_flow_hourly_off_forces_gated_additions_off(
    hass: HomeAssistant,
) -> None:
    """Turning hourly off writes both gated toggles as False (issue #69)."""
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
        # Previously enabled; turning hourly off must not leave them dangling on.
        options={
            CONF_HOURLY_FORECAST: True,
            CONF_HOURLY_CLOUD_LAYERS: True,
            CONF_HOURLY_TEMP_PERCENTILES: True,
        },
        title="Köniz",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_HOURLY_FORECAST: False}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOURLY_CLOUD_LAYERS] is False
    assert result["data"][CONF_HOURLY_TEMP_PERCENTILES] is False


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


# ---------------------------------------------------------------------------
# Pollen option + station step (ADR-0005, #67)
# ---------------------------------------------------------------------------

# Pollen stations near Bern: PBE (Bern) is the closest to the Köniz point.
_POLLEN_STATIONS: list[PollenStation] = [
    PollenStation("PBE", "Bern", "BE", 46.9481, 7.4474, 553.0),
    PollenStation("PBS", "Basel", "BS", 47.5619, 7.5834, 277.0),
    PollenStation("PLU", "Luzern", "LU", 47.0642, 8.3018, 454.0),
    PollenStation("PZH", "Zürich", "ZH", 47.3781, 8.5658, 556.0),
]


def _koniz_entry() -> MockConfigEntry:
    """A configured entry for the Köniz point (nearest pollen station is PBE)."""
    return MockConfigEntry(
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


async def test_options_flow_pollen_on_shows_station_step_and_saves(
    hass: HomeAssistant,
) -> None:
    """Enabling pollen leads to the station step; the nearest is pre-selected."""
    entry = _koniz_entry()
    entry.add_to_hass(hass)

    with (
        patch(f"{_FLOW}.async_get_clientsession", return_value=object()),
        patch(
            f"{_FLOW}.fetch_pollen_stations",
            AsyncMock(return_value=_POLLEN_STATIONS),
        ),
        patch(f"{_FLOW}.fetch_points", AsyncMock(return_value=_POINTS)),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={CONF_HOURLY_FORECAST: False, CONF_POLLEN: True},
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "pollen_station"
        # The three nearest stations are offered; PBE (Bern) is nearest.
        options = result["data_schema"].schema[CONF_POLLEN_STATION].container
        assert set(options) == {"PBE", "PBS", "PLU"}
        assert result["data_schema"]({})[CONF_POLLEN_STATION] == "PBE"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_POLLEN_STATION: "PBE"}
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_POLLEN] is True
    assert result["data"][CONF_POLLEN_STATION] == "PBE"
    # Hourly stays off and its horizon default is preserved alongside pollen.
    assert result["data"][CONF_HOURLY_FORECAST] is False


async def test_options_flow_hourly_and_pollen_both_on(
    hass: HomeAssistant,
) -> None:
    """Both toggles on: hourly-horizon step then the pollen-station step."""
    entry = _koniz_entry()
    entry.add_to_hass(hass)

    with (
        patch(f"{_FLOW}.async_get_clientsession", return_value=object()),
        patch(
            f"{_FLOW}.fetch_pollen_stations",
            AsyncMock(return_value=_POLLEN_STATIONS),
        ),
        patch(f"{_FLOW}.fetch_points", AsyncMock(return_value=_POINTS)),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={CONF_HOURLY_FORECAST: True, CONF_POLLEN: True},
        )
        assert result["step_id"] == "hourly"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_HOURLY_HORIZON_DAYS: 4}
        )
        assert result["step_id"] == "pollen_station"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_POLLEN_STATION: "PBS"}
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOURLY_FORECAST] is True
    assert result["data"][CONF_HOURLY_HORIZON_DAYS] == 4
    assert result["data"][CONF_POLLEN] is True
    assert result["data"][CONF_POLLEN_STATION] == "PBS"


async def test_options_flow_pollen_station_cannot_connect(
    hass: HomeAssistant,
) -> None:
    """A metadata fetch error shows the station step with a cannot_connect error."""
    entry = _koniz_entry()
    entry.add_to_hass(hass)

    with (
        patch(f"{_FLOW}.async_get_clientsession", return_value=object()),
        patch(
            f"{_FLOW}.fetch_pollen_stations",
            AsyncMock(side_effect=OgdError("boom")),
        ),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={CONF_HOURLY_FORECAST: False, CONF_POLLEN: True},
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "pollen_station"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_options_flow_pollen_station_retry_after_error(
    hass: HomeAssistant,
) -> None:
    """Resubmitting the empty error form retries the fetch without crashing.

    Regression guard: the empty error form carries no station field, so its
    resubmission must re-run the fetch rather than read a missing key.
    """
    entry = _koniz_entry()
    entry.add_to_hass(hass)

    # First metadata fetch fails, the retry succeeds.
    stations_mock = AsyncMock(side_effect=[OgdError("boom"), _POLLEN_STATIONS])

    with (
        patch(f"{_FLOW}.async_get_clientsession", return_value=object()),
        patch(f"{_FLOW}.fetch_pollen_stations", stations_mock),
        patch(f"{_FLOW}.fetch_points", AsyncMock(return_value=_POINTS)),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={CONF_HOURLY_FORECAST: False, CONF_POLLEN: True},
        )
        assert result["step_id"] == "pollen_station"
        assert result["errors"] == {"base": "cannot_connect"}

        # Resubmit the empty error form: the fetch is retried and now succeeds,
        # so the real station selector is shown (no KeyError).
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "pollen_station"
        assert not result["errors"]
        options = result["data_schema"].schema[CONF_POLLEN_STATION].container
        assert set(options) == {"PBE", "PBS", "PLU"}


# ---------------------------------------------------------------------------
# Reconfigure flow (A9, #52)
# ---------------------------------------------------------------------------


def _configured_entry(
    *,
    point_id: int = 800100,
    point_type_id: int = 2,
    postal_code: str = "8001",
    point_name: str = "Zürich",
    station_abbr: str = "SMA",
    station_name: str = "Zürich / Fluntern",
) -> MockConfigEntry:
    """A ready MockConfigEntry mirroring what the setup flow would create."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{point_type_id}-{point_id}",
        data={
            CONF_POINT_ID: point_id,
            CONF_POINT_TYPE_ID: point_type_id,
            CONF_POSTAL_CODE: postal_code,
            CONF_POINT_NAME: point_name,
            CONF_STATION_ABBR: station_abbr,
            CONF_STATION_NAME: station_name,
        },
        title=point_name,
    )


def _history_options(result: dict) -> list[str]:
    """The station-history choices offered by the history-step form."""
    for marker, selector in result["data_schema"].schema.items():
        if str(marker) == CONF_HISTORY_ACTION:
            return list(selector.config["options"])
    raise AssertionError("history_action not in schema")


async def test_reconfigure_station_change_keep(
    hass: HomeAssistant, mock_ogd_functions
) -> None:
    """A station change offers keep/discard; keep updates data and logs the switch."""
    entry = _configured_entry()
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"

    # Select postal-code mode (entry is type 2).
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={_CONF_MODE: _MODE_POSTAL_CODE}
    )
    assert result["step_id"] == "postal_code"
    # Postal code is pre-filled from the entry.
    assert result["data_schema"]({})[CONF_POSTAL_CODE] == 8001

    # 8001 has a single point → straight to the station step.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POSTAL_CODE: 8001}
    )
    assert result["step_id"] == "station"
    # The currently configured station is pre-selected.
    assert result["data_schema"]({})[CONF_STATION_ABBR] == "SMA"

    # Switch to KLO → the history-choice step appears.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_STATION_ABBR: "KLO"}
    )
    assert result["step_id"] == "history"
    assert _history_options(result) == [HISTORY_KEEP, HISTORY_DISCARD]
    assert result["description_placeholders"] == {
        "old_station": "Zürich / Fluntern",
        "new_station": "Zürich / Kloten",
    }

    with (
        patch(f"{_FLOW}.async_log_station_switch") as log_switch,
        patch.object(hass.config_entries, "async_schedule_reload") as reload_mock,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_HISTORY_ACTION: HISTORY_KEEP}
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    # Same entry, station updated in place, reload requested, no duplicate.
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert entry.data[CONF_STATION_ABBR] == "KLO"
    assert entry.data[CONF_STATION_NAME] == "Zürich / Kloten"
    reload_mock.assert_called_once_with(entry.entry_id)
    log_switch.assert_called_once()
    assert log_switch.call_args.args[2] == "Zürich / Fluntern"
    assert log_switch.call_args.args[3] == "Zürich / Kloten"


async def test_reconfigure_station_change_discard(
    hass: HomeAssistant, mock_ogd_functions
) -> None:
    """The discard choice calls the purge/clear helper before updating the entry."""
    entry = _configured_entry()
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={_CONF_MODE: _MODE_POSTAL_CODE}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POSTAL_CODE: 8001}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_STATION_ABBR: "KLO"}
    )
    assert result["step_id"] == "history"

    with (
        patch(
            f"{_FLOW}.async_discard_station_history", new=AsyncMock()
        ) as discard_mock,
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_HISTORY_ACTION: HISTORY_DISCARD}
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    discard_mock.assert_awaited_once()
    assert discard_mock.await_args.args[1] is entry
    assert entry.data[CONF_STATION_ABBR] == "KLO"


async def test_reconfigure_point_only_change_skips_history(
    hass: HomeAssistant, mock_ogd_functions
) -> None:
    """Changing only the forecast point never shows the history step."""
    entry = _configured_entry(
        point_id=309800,
        postal_code="3098",
        point_name="Köniz",
        station_abbr="BER",
        station_name="Bern / Zollikofen",
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={_CONF_MODE: _MODE_POSTAL_CODE}
    )
    # 3098 has two points → the point step, pre-selecting the current one.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POSTAL_CODE: 3098}
    )
    assert result["step_id"] == "point"
    assert result["data_schema"]({})[CONF_POINT_ID] == 309800

    # Pick the other point; BER stays the nearest/kept station.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POINT_ID: 309801}
    )
    assert result["step_id"] == "station"

    with patch.object(hass.config_entries, "async_schedule_reload") as reload_mock:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_STATION_ABBR: "BER"}
        )

    # No history step: the station is unchanged.
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_POINT_ID] == 309801
    assert entry.data[CONF_POINT_NAME] == "Schliern b. Köniz"
    assert entry.data[CONF_STATION_ABBR] == "BER"
    assert entry.unique_id == "2-309801"
    reload_mock.assert_called_once_with(entry.entry_id)


async def test_reconfigure_onto_existing_point_aborts(
    hass: HomeAssistant, mock_ogd_functions
) -> None:
    """Reconfiguring onto a point another entry already owns aborts as duplicate."""
    other = _configured_entry(
        point_id=309801,
        point_type_id=2,
        postal_code="3098",
        point_name="Schliern b. Köniz",
        station_abbr="BER",
    )
    other.add_to_hass(hass)
    entry = _configured_entry(
        point_id=309800,
        postal_code="3098",
        point_name="Köniz",
        station_abbr="BER",
        station_name="Bern / Zollikofen",
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={_CONF_MODE: _MODE_POSTAL_CODE}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POSTAL_CODE: 3098}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POINT_ID: 309801}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_STATION_ABBR: "BER"}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure_backfill_hidden_without_b12(
    hass: HomeAssistant, mock_ogd_functions
) -> None:
    """The backfill choice is hidden until the recorder-import layer lands."""
    entry = _configured_entry()
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={_CONF_MODE: _MODE_POSTAL_CODE}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POSTAL_CODE: 8001}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_STATION_ABBR: "KLO"}
    )
    assert result["step_id"] == "history"
    assert HISTORY_BACKFILL not in _history_options(result)


async def test_reconfigure_backfill_shown_when_available(
    hass: HomeAssistant, mock_ogd_functions
) -> None:
    """When the machinery is present the backfill choice is offered."""
    entry = _configured_entry()
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={_CONF_MODE: _MODE_POSTAL_CODE}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POSTAL_CODE: 8001}
    )
    with patch(f"{_FLOW}.BACKFILL_AVAILABLE", True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_STATION_ABBR: "KLO"}
        )
    assert result["step_id"] == "history"
    assert _history_options(result) == [
        HISTORY_KEEP,
        HISTORY_DISCARD,
        HISTORY_BACKFILL,
    ]


async def test_reconfigure_cannot_connect(hass: HomeAssistant) -> None:
    """A metadata failure on reconfigure shows cannot_connect on its own step."""
    entry = _configured_entry()
    entry.add_to_hass(hass)

    async def _mock_load_fail(self) -> bool:
        return False

    with patch(
        f"{_FLOW}.MeteoSwissWeatherConfigFlow._load_metadata", _mock_load_fail
    ):
        result = await entry.start_reconfigure_flow(hass)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reconfigure_mode_preselects_mountain_for_mountain_entry(
    hass: HomeAssistant, mock_ogd_functions
) -> None:
    """Reconfiguring a mountain entry pre-selects mountain mode."""
    entry = _configured_entry(
        point_id=5000,
        point_type_id=3,
        postal_code="",
        point_name="Jungfraujoch",
        station_abbr="ABO",
        station_name="Adelboden",
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    # The schema default should be mountain mode.
    default_mode = result["data_schema"]({}).get("forecast_mode")
    assert default_mode == _MODE_MOUNTAIN


async def test_reconfigure_mountain_to_postal_code(
    hass: HomeAssistant, mock_ogd_functions
) -> None:
    """A mountain entry can be reconfigured to a postal-code entry."""
    # Start with SMA so selecting SMA on the new postal-code path is a
    # station-unchanged reconfigure (skips the history step).
    entry = _configured_entry(
        point_id=5000,
        point_type_id=3,
        postal_code="",
        point_name="Jungfraujoch",
        station_abbr="SMA",
        station_name="Zürich / Fluntern",
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    # Switch to postal-code mode.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={_CONF_MODE: _MODE_POSTAL_CODE}
    )
    assert result["step_id"] == "postal_code"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POSTAL_CODE: 8001}
    )
    assert result["step_id"] == "station"

    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_STATION_ABBR: "SMA"}
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_POINT_TYPE_ID] == 2
    assert entry.data[CONF_POINT_ID] == 800100
    assert entry.unique_id == "2-800100"


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
