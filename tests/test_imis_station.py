"""Tests for the optional SLF IMIS snow station (IMIS-b, ADR-0009, issue #174).

Three concerns are covered, following ``test_precip_station.py`` (ADR-0006):

- the config/reconfigure flow offers the optional snow-station pick in the
  station step (the three nearest ``SNOW_FLAT`` stations, none by default),
  stores it, keeps it on a reconfigure round-trip, clears it on "none", and
  drops the field when the SLF station list is unavailable;
- with no IMIS station configured, **zero** requests reach
  ``measurement-api.slf.ch`` (call counts);
- with one configured, the snow sensors read from the SLF upstream (measured
  snow height and modelled new snow), carry the SLF attribution, and the
  MeteoSwiss entities are unaffected — including when the SLF fetch fails.

Upstream responses are replayed from ``tests/fixtures`` via the ``mock_ogd``
and ``mock_ogd_imis`` fixtures (conftest.py); no test hits the network. The
config-flow tests patch ``_load_metadata`` so no aiohttp session is created.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.meteoswiss_weather.const import (
    ATTRIBUTION_SLF,
    CONF_IMIS_STATION_CODE,
    CONF_IMIS_STATION_ELEVATION,
    CONF_IMIS_STATION_NAME,
    CONF_POINT_ID,
    CONF_POINT_NAME,
    CONF_POINT_TYPE_ID,
    CONF_POSTAL_CODE,
    CONF_STATION_ABBR,
    CONF_STATION_NAME,
    DOMAIN,
)
from custom_components.meteoswiss_weather.ogd import ForecastPoint, Station
from custom_components.meteoswiss_weather.slf import ImisStation
from custom_components.meteoswiss_weather.slf.const import (
    DAILY_SNOW_URL,
    measurements_url,
)

_FLOW = "custom_components.meteoswiss_weather.config_flow"

_CONF_MODE = "forecast_mode"
_MODE_POSTAL_CODE = "postal_code"

# Reference data mirroring the trimmed fixture CSVs / JSON.
_POINTS: list[ForecastPoint] = [
    ForecastPoint(309800, 2, "3098", "Köniz", 46.9245, 7.4147, 595.0),
    ForecastPoint(800100, 2, "8001", "Zürich", 47.3769, 8.5417, 408.0),
]
_STATIONS: list[Station] = [
    Station("BER", "Bern / Zollikofen", "BE", 46.990765, 7.464061, 552.0),
    Station("ABO", "Adelboden", "BE", 46.491703, 7.560703, 1321.0),
    Station("PAY", "Payerne", "VD", 46.811798, 6.942381, 490.0),
]
# IMIS stations mirroring ``slf-imis_stations.json``. The three nearest
# SNOW_FLAT to Köniz are STH2, JAU2, SHE2; JAU1 is a WIND station (excluded).
_IMIS_STATIONS: list[ImisStation] = [
    ImisStation("DAV2", "Bärentälli", 46.6988884749, 9.8194108372, 2558.0, "GR",
                "SNOW_FLAT"),
    ImisStation("SCH2", "Türliboden", 46.5769159849, 7.8347322559, 2332.0, "BE",
                "SNOW_FLAT"),
    ImisStation("JAU1", "Chörblispitz", 46.6296849558, 7.2689498572, 2103.0, "FR",
                "WIND"),
    ImisStation("SHE2", "Schibe", 46.7488254166, 7.8124493033, 1852.0, "BE",
                "SNOW_FLAT"),
    ImisStation("BOG4", "Bombögn Schnee", 46.307255, 8.490197, 2031.0, "TI",
                "SNOW_SLOPE"),
    ImisStation("JAU2", "Fochsen", 46.6330485439, 7.274541453, 1707.0, "FR",
                "SNOW_FLAT"),
    ImisStation("STH2", "Vorderstocken", 46.6820486806, 7.5196488888, 1791.0,
                "BE", "SNOW_FLAT"),
    ImisStation("CSL2", "Col du Chasseral", 47.1163154815, 7.0313366437, 1455.0,
                "BE", "SNOW_FLAT"),
]

_BERN_LAT = 46.948
_BERN_LON = 7.447


async def _mock_load_ok_imis(self) -> bool:
    """Inject fixture data including the IMIS station list (no aiohttp session)."""
    self._all_points = _POINTS
    self._all_stations = _STATIONS
    self._all_imis_stations = _IMIS_STATIONS
    return True


@pytest.fixture
def mock_flow_with_imis():
    """Patch ``_load_metadata`` so the flow has the IMIS station list loaded."""
    with patch(
        f"{_FLOW}.MeteoSwissWeatherConfigFlow._load_metadata", _mock_load_ok_imis
    ):
        yield


# ---------------------------------------------------------------------------
# Config flow: the station step offers and stores the IMIS pick
# ---------------------------------------------------------------------------


async def _advance_to_station_step(hass: HomeAssistant) -> dict:
    """Drive the setup flow (Köniz) up to the station step."""
    hass.config.latitude = _BERN_LAT
    hass.config.longitude = _BERN_LON
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={_CONF_MODE: _MODE_POSTAL_CODE}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POSTAL_CODE: 3098}
    )
    assert result["step_id"] == "station"
    return result


async def test_station_step_offers_three_nearest_snow_flat_none_default(
    hass: HomeAssistant, mock_flow_with_imis
) -> None:
    """The station step offers the three nearest SNOW_FLAT stations, none default."""
    result = await _advance_to_station_step(hass)

    container = result["data_schema"].schema[CONF_IMIS_STATION_CODE].container
    # The three nearest SNOW_FLAT stations to Köniz plus the "none" sentinel ("").
    assert set(container) == {"", "STH2", "JAU2", "SHE2"}
    # The label carries the station name, elevation and distance.
    assert container["STH2"].startswith("Vorderstocken (1791 m, ")
    assert container["STH2"].endswith(" km)")
    # Nothing is pre-selected — the feature is opt-in (ADR-0009).
    assert result["data_schema"]({})[CONF_IMIS_STATION_CODE] == ""


async def test_setup_stores_selected_imis_station(
    hass: HomeAssistant, mock_flow_with_imis
) -> None:
    """Picking an IMIS station stores its code, name and elevation on the entry."""
    result = await _advance_to_station_step(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_STATION_ABBR: "BER", CONF_IMIS_STATION_CODE: "STH2"},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_STATION_ABBR] == "BER"
    assert data[CONF_IMIS_STATION_CODE] == "STH2"
    assert data[CONF_IMIS_STATION_NAME] == "Vorderstocken"
    assert data[CONF_IMIS_STATION_ELEVATION] == 1791.0


async def test_setup_without_imis_leaves_it_empty(
    hass: HomeAssistant, mock_flow_with_imis
) -> None:
    """Leaving the pick on "none" stores empty IMIS keys (feature stays off)."""
    result = await _advance_to_station_step(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_STATION_ABBR: "BER", CONF_IMIS_STATION_CODE: ""},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_IMIS_STATION_CODE] == ""
    assert result["data"][CONF_IMIS_STATION_NAME] == ""
    assert result["data"][CONF_IMIS_STATION_ELEVATION] is None


def _imis_entry(
    code: str = "STH2",
    name: str = "Vorderstocken",
    elevation: float | None = 1791.0,
) -> MockConfigEntry:
    """A configured Köniz entry carrying an IMIS snow-station pick."""
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
            CONF_IMIS_STATION_CODE: code,
            CONF_IMIS_STATION_NAME: name,
            CONF_IMIS_STATION_ELEVATION: elevation,
        },
        title="Köniz",
    )


async def test_reconfigure_roundtrip_keeps_imis_pick(
    hass: HomeAssistant, mock_flow_with_imis
) -> None:
    """A reconfigure round-trip pre-selects and keeps the IMIS pick."""
    entry = _imis_entry()
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={_CONF_MODE: _MODE_POSTAL_CODE}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POSTAL_CODE: 3098}
    )
    # Single Köniz point in this reference set → straight to the station step.
    assert result["step_id"] == "station"
    # The currently configured IMIS station is pre-selected.
    assert result["data_schema"]({})[CONF_IMIS_STATION_CODE] == "STH2"

    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_STATION_ABBR: "BER", CONF_IMIS_STATION_CODE: "STH2"},
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_IMIS_STATION_CODE] == "STH2"
    assert entry.data[CONF_IMIS_STATION_NAME] == "Vorderstocken"
    assert entry.data[CONF_IMIS_STATION_ELEVATION] == 1791.0


async def test_reconfigure_can_clear_imis_pick(
    hass: HomeAssistant, mock_flow_with_imis
) -> None:
    """Choosing "none" on reconfigure clears the IMIS pick."""
    entry = _imis_entry()
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={_CONF_MODE: _MODE_POSTAL_CODE}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POSTAL_CODE: 3098}
    )
    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_STATION_ABBR: "BER", CONF_IMIS_STATION_CODE: ""},
        )

    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_IMIS_STATION_CODE] == ""
    assert entry.data[CONF_IMIS_STATION_ELEVATION] is None


async def test_station_step_hides_imis_when_list_unavailable(
    hass: HomeAssistant,
) -> None:
    """An SLF list failure drops the pick without failing the flow."""

    async def _load_ok_no_imis(self) -> bool:
        self._all_points = _POINTS
        self._all_stations = _STATIONS
        self._all_imis_stations = None
        return True

    with patch(
        f"{_FLOW}.MeteoSwissWeatherConfigFlow._load_metadata", _load_ok_no_imis
    ):
        result = await _advance_to_station_step(hass)
        # No IMIS field is shown, and the flow still completes.
        assert CONF_IMIS_STATION_CODE not in result["data_schema"].schema
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_STATION_ABBR: "BER"}
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_IMIS_STATION_CODE] == ""


# ---------------------------------------------------------------------------
# "Currently no data" label (ADR-0009 amendment, issue #186)
# ---------------------------------------------------------------------------

# Daily-snow codes matching the fixture (SCH2 is absent — currently no data).
_DAILY_SNOW_CODES_FIXTURE = {"BOG4", "CSL2", "DAV2", "JAU2", "SHE2", "STH2"}

# A synthetic ForecastPoint placed near SCH2 (lat=46.5769, lon=7.8347) so
# the nearest three SNOW_FLAT from _IMIS_STATIONS include SCH2.
_POINT_NEAR_SCH2 = ForecastPoint(999900, 2, "9999", "Testort", 46.60, 7.83, 1000.0)
_POINTS_WITH_SCH2 = [_POINT_NEAR_SCH2, *_POINTS]


async def _mock_load_imis_with_codes(self) -> bool:
    """Inject IMIS station list AND daily-snow codes (SCH2 absent)."""
    self._all_points = _POINTS_WITH_SCH2
    self._all_stations = _STATIONS
    self._all_imis_stations = _IMIS_STATIONS
    self._imis_daily_snow_codes = _DAILY_SNOW_CODES_FIXTURE
    return True


@pytest.fixture
def mock_flow_with_codes():
    """Patch ``_load_metadata`` to include daily-snow codes."""
    with patch(
        f"{_FLOW}.MeteoSwissWeatherConfigFlow._load_metadata",
        _mock_load_imis_with_codes,
    ):
        yield


async def _advance_to_station_step_9999(hass: HomeAssistant) -> dict:
    """Drive the setup flow to the station step via postal code 9999 (near SCH2)."""
    hass.config.latitude = 46.60
    hass.config.longitude = 7.83
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={_CONF_MODE: _MODE_POSTAL_CODE}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_POSTAL_CODE: 9999}
    )
    assert result["step_id"] == "station"
    return result


async def test_sch2_offered_with_no_data_note(
    hass: HomeAssistant, mock_flow_with_codes
) -> None:
    """A station absent from daily-snow is still offered, with 'currently no data'."""
    result = await _advance_to_station_step_9999(hass)

    container = result["data_schema"].schema[CONF_IMIS_STATION_CODE].container
    # SCH2 must be present in the picker (nearest three to Testort include SCH2).
    assert "SCH2" in container
    # Its label must carry the "currently no data" suffix.
    assert container["SCH2"].endswith(", currently no data")
    # A station that has data should not carry the note.
    for code, label in container.items():
        if code and code != "SCH2":
            assert ", currently no data" not in label
            assert code in _DAILY_SNOW_CODES_FIXTURE


async def test_daily_snow_failure_shows_plain_labels(
    hass: HomeAssistant,
) -> None:
    """When daily-snow fetch fails, labels are plain (no note); the flow completes."""

    async def _load_imis_no_codes(self) -> bool:
        self._all_points = _POINTS
        self._all_stations = _STATIONS
        self._all_imis_stations = _IMIS_STATIONS
        self._imis_daily_snow_codes = None  # simulate fetch failure
        return True

    with patch(
        f"{_FLOW}.MeteoSwissWeatherConfigFlow._load_metadata", _load_imis_no_codes
    ):
        result = await _advance_to_station_step(hass)

    container = result["data_schema"].schema[CONF_IMIS_STATION_CODE].container
    # All labels should be plain (no "currently no data" note).
    for code, label in container.items():
        if code:
            assert ", currently no data" not in label

    # The flow still completes successfully.
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_STATION_ABBR: "BER", CONF_IMIS_STATION_CODE: ""},
    )
    assert result2["type"] == FlowResultType.CREATE_ENTRY


# ---------------------------------------------------------------------------
# Runtime: request budget and sourcing
# ---------------------------------------------------------------------------


def _slf_calls(aioclient_mock: AiohttpClientMocker) -> int:
    """Number of requests to the SLF measurement API recorded so far."""
    return sum(
        1
        for _method, url, *_ in aioclient_mock.mock_calls
        if "measurement-api.slf.ch" in url.host
    )


def _entity_id(hass: HomeAssistant, key: str) -> str | None:
    """Resolve the snow sensor entity id from its unique id."""
    registry = er.async_get(hass)
    return registry.async_get_entity_id("sensor", DOMAIN, f"2-309800_{key}")


async def test_unset_makes_zero_slf_requests(
    hass: HomeAssistant,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """With no IMIS station configured, the SLF API is never hit."""
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
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.runtime_data.imis_coordinator is None
    assert _slf_calls(mock_ogd) == 0


async def test_set_sources_snow_from_imis_station(
    hass: HomeAssistant,
    mock_ogd_imis: AiohttpClientMocker,
) -> None:
    """Snow sensors come from the SLF station; MeteoSwiss sensors are unchanged."""
    entry = _imis_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = entry.runtime_data
    # The IMIS coordinator exists and polled the SLF API.
    assert runtime.imis_coordinator is not None
    assert _slf_calls(mock_ogd_imis) >= 1
    data = runtime.imis_coordinator.data
    assert data is not None
    # Newest row in the STH2 fixture has HS = 1.0 cm.
    assert data.measurement is not None
    assert data.measurement.snow_height_cm == 1.0
    # STH2's daily-snow row has HN_1D = 0.0 cm (SNOWPACK-modelled).
    assert data.daily_snow is not None
    assert data.daily_snow.new_snow_24h_cm == 0.0

    # The snow-height sensor reads the measured HS.
    snow_id = _entity_id(hass, "snow_height")
    assert snow_id is not None
    snow_state = hass.states.get(snow_id)
    assert snow_state is not None
    assert float(snow_state.state) == 1.0
    # Its attribution is the SLF one, and it names the station and elevation.
    assert snow_state.attributes["attribution"] == ATTRIBUTION_SLF
    assert snow_state.attributes.get("station") == "Vorderstocken"
    assert snow_state.attributes.get("elevation") == 1791.0

    # The modelled new-snow sensor reads HN_1D.
    new_snow_id = _entity_id(hass, "new_snow_24h")
    assert new_snow_id is not None
    new_snow_state = hass.states.get(new_snow_id)
    assert new_snow_state is not None
    assert float(new_snow_state.state) == 0.0
    assert new_snow_state.attributes["attribution"] == ATTRIBUTION_SLF

    # The MeteoSwiss temperature sensor is unaffected (still BER, 19.5 °C).
    temp_state = hass.states.get("sensor.koniz_temperature")
    assert temp_state is not None
    assert temp_state.state == "19.5"


async def test_slf_failure_leaves_meteoswiss_unaffected(
    hass: HomeAssistant,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A failing SLF fetch leaves the snow sensors unavailable, MeteoSwiss fine."""
    # Register the SLF endpoints to fail (500) on top of the healthy OGD mocks.
    mock_ogd.get(measurements_url("STH2"), status=500)
    mock_ogd.get(DAILY_SNOW_URL, status=500)

    entry = _imis_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = entry.runtime_data
    # The coordinator exists but its first refresh failed (non-fatal).
    assert runtime.imis_coordinator is not None
    assert runtime.imis_coordinator.last_update_success is False

    # The snow sensors are unavailable.
    snow_id = _entity_id(hass, "snow_height")
    assert snow_id is not None
    assert hass.states.get(snow_id).state == "unavailable"

    # The MeteoSwiss temperature sensor is unaffected.
    temp_state = hass.states.get("sensor.koniz_temperature")
    assert temp_state is not None
    assert temp_state.state == "19.5"
