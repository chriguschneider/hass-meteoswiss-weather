"""Tests for the optional precipitation station (A11b, ADR-0006, issue #70).

Three concerns are covered:

- the config/reconfigure flow offers the optional precipitation pick in the
  station step, stores it, and a reconfigure round-trip keeps it;
- with no precipitation station configured, **zero** requests reach the
  precipitation collection (call counts);
- with one configured, the precipitation sensor and the weather entity's
  current precipitation read from that station while temperature stays with
  the main station.

Upstream responses are replayed from ``tests/fixtures`` via the ``mock_ogd``
and ``mock_ogd_precip`` fixtures (conftest.py); no test hits the network. The
config-flow tests patch ``_load_metadata`` so no aiohttp session is created.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.meteoswiss_weather.const import (
    CONF_POINT_ID,
    CONF_POINT_NAME,
    CONF_POINT_TYPE_ID,
    CONF_POSTAL_CODE,
    CONF_PRECIP_STATION_ABBR,
    CONF_PRECIP_STATION_NAME,
    CONF_STATION_ABBR,
    CONF_STATION_NAME,
    DOMAIN,
)
from custom_components.meteoswiss_weather.ogd import ForecastPoint, Station

_FLOW = "custom_components.meteoswiss_weather.config_flow"

_CONF_MODE = "forecast_mode"
_MODE_POSTAL_CODE = "postal_code"

# Reference data mirroring the trimmed fixture CSVs.
_POINTS: list[ForecastPoint] = [
    ForecastPoint(309800, 2, "3098", "Köniz", 46.9245, 7.4147, 595.0),
    ForecastPoint(800100, 2, "8001", "Zürich", 47.3769, 8.5417, 408.0),
]
_STATIONS: list[Station] = [
    Station("BER", "Bern / Zollikofen", "BE", 46.990765, 7.464061, 552.0),
    Station("ABO", "Adelboden", "BE", 46.491703, 7.560703, 1321.0),
    Station("PAY", "Payerne", "VD", 46.811798, 6.942381, 490.0),
]
# Precipitation-only network (mirrors ogd-smn-precip_meta_stations.csv). The
# three nearest to Köniz are BEL, LAU, KIE; ABE is far south (Adelboden).
_PRECIP_STATIONS: list[Station] = [
    Station("BEL", "Belp", "BE", 46.897, 7.497, 482.0),
    Station("LAU", "Laupen", "BE", 46.899, 7.320, 480.0),
    Station("KIE", "Kiesen", "BE", 46.817, 7.567, 520.0),
    Station("ABE", "Adelboden (precip)", "BE", 46.490, 7.558, 1320.0),
]

_BERN_LAT = 46.948
_BERN_LON = 7.447


async def _mock_load_ok_precip(self) -> bool:
    """Inject fixture data including the precip network (no aiohttp session)."""
    self._all_points = _POINTS
    self._all_stations = _STATIONS
    self._all_precip_stations = _PRECIP_STATIONS
    return True


@pytest.fixture
def mock_flow_with_precip():
    """Patch ``_load_metadata`` so the flow has the precip station list loaded."""
    with patch(
        f"{_FLOW}.MeteoSwissWeatherConfigFlow._load_metadata", _mock_load_ok_precip
    ):
        yield


# ---------------------------------------------------------------------------
# Config flow: the station step offers and stores the precipitation pick
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


async def test_station_step_offers_three_nearest_precip_none_default(
    hass: HomeAssistant, mock_flow_with_precip
) -> None:
    """The station step offers the three nearest precip stations, none default."""
    result = await _advance_to_station_step(hass)

    container = result["data_schema"].schema[CONF_PRECIP_STATION_ABBR].container
    # The three nearest precip stations to Köniz plus the "none" sentinel ("").
    assert set(container) == {"", "BEL", "LAU", "KIE"}
    # Nothing is pre-selected — the feature is opt-in (ADR-0006).
    assert result["data_schema"]({})[CONF_PRECIP_STATION_ABBR] == ""


async def test_setup_stores_selected_precip_station(
    hass: HomeAssistant, mock_flow_with_precip
) -> None:
    """Picking a precip station stores its abbreviation and name on the entry."""
    result = await _advance_to_station_step(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_STATION_ABBR: "BER", CONF_PRECIP_STATION_ABBR: "BEL"},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_STATION_ABBR] == "BER"
    assert data[CONF_PRECIP_STATION_ABBR] == "BEL"
    assert data[CONF_PRECIP_STATION_NAME] == "Belp"


async def test_setup_without_precip_leaves_it_empty(
    hass: HomeAssistant, mock_flow_with_precip
) -> None:
    """Leaving the pick on "none" stores empty precip keys (feature stays off)."""
    result = await _advance_to_station_step(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_STATION_ABBR: "BER", CONF_PRECIP_STATION_ABBR: ""},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PRECIP_STATION_ABBR] == ""
    assert result["data"][CONF_PRECIP_STATION_NAME] == ""


def _precip_entry(
    precip_abbr: str = "BEL", precip_name: str = "Belp"
) -> MockConfigEntry:
    """A configured Köniz entry carrying a precipitation-station pick."""
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
            CONF_PRECIP_STATION_ABBR: precip_abbr,
            CONF_PRECIP_STATION_NAME: precip_name,
        },
        title="Köniz",
    )


async def test_reconfigure_roundtrip_keeps_precip_pick(
    hass: HomeAssistant, mock_flow_with_precip
) -> None:
    """A reconfigure round-trip pre-selects and keeps the precipitation pick."""
    entry = _precip_entry()
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
    # The currently configured precip station is pre-selected.
    assert result["data_schema"]({})[CONF_PRECIP_STATION_ABBR] == "BEL"

    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_STATION_ABBR: "BER", CONF_PRECIP_STATION_ABBR: "BEL"},
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PRECIP_STATION_ABBR] == "BEL"
    assert entry.data[CONF_PRECIP_STATION_NAME] == "Belp"


async def test_reconfigure_can_clear_precip_pick(
    hass: HomeAssistant, mock_flow_with_precip
) -> None:
    """Choosing "none" on reconfigure clears the precipitation pick."""
    entry = _precip_entry()
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
            user_input={CONF_STATION_ABBR: "BER", CONF_PRECIP_STATION_ABBR: ""},
        )

    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PRECIP_STATION_ABBR] == ""


async def test_station_step_hides_precip_when_metadata_unavailable(
    hass: HomeAssistant,
) -> None:
    """A precip-metadata failure drops the pick without failing the flow."""

    async def _load_ok_no_precip(self) -> bool:
        self._all_points = _POINTS
        self._all_stations = _STATIONS
        self._all_precip_stations = None
        return True

    with patch(
        f"{_FLOW}.MeteoSwissWeatherConfigFlow._load_metadata", _load_ok_no_precip
    ):
        result = await _advance_to_station_step(hass)
        # No precip field is shown, and the flow still completes.
        assert CONF_PRECIP_STATION_ABBR not in result["data_schema"].schema
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_STATION_ABBR: "BER"}
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PRECIP_STATION_ABBR] == ""


# ---------------------------------------------------------------------------
# Runtime: request budget and sourcing
# ---------------------------------------------------------------------------


def _precip_calls(aioclient_mock: AiohttpClientMocker) -> int:
    """Number of requests to the precipitation collection recorded so far."""
    return sum(
        1
        for _method, url, *_ in aioclient_mock.mock_calls
        if "ogd-smn-precip" in url.path
    )


async def test_unset_makes_zero_precip_requests(
    hass: HomeAssistant,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """With no precip station configured, the precip collection is never hit."""
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

    assert entry.runtime_data.precip_coordinator is None
    assert _precip_calls(mock_ogd) == 0


async def test_set_sources_precip_from_precip_station(
    hass: HomeAssistant,
    mock_ogd_precip: AiohttpClientMocker,
) -> None:
    """Precipitation comes from the precip station; temperature from the main one."""
    entry = _precip_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = entry.runtime_data
    # The second coordinator exists and polled the precip station.
    assert runtime.precip_coordinator is not None
    assert _precip_calls(mock_ogd_precip) >= 1
    precip_obs = runtime.precip_coordinator.data
    assert precip_obs is not None
    # Last non-empty rre150z0 in the BEL fixture is 0.5.
    assert precip_obs.precipitation_10min == 0.5

    # The precipitation sensor reads from BEL; temperature stays with BER.
    precip_state = hass.states.get("sensor.koniz_precipitation_10_min")
    assert precip_state is not None
    assert precip_state.state == "0.5"
    # Its attribution and station attribute name the precip station.
    assert "Belp" in precip_state.attributes["attribution"]
    assert precip_state.attributes.get("station") == "Belp"

    temp_state = hass.states.get("sensor.koniz_temperature")
    assert temp_state is not None
    # BER's last non-empty temperature row is 19.5 °C (not the precip station).
    assert temp_state.state == "19.5"


async def test_weather_current_precipitation_from_precip_station(
    hass: HomeAssistant,
    mock_ogd_precip: AiohttpClientMocker,
) -> None:
    """The weather entity's current_precipitation reads from the precip station."""
    entry = _precip_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    weather = hass.states.get("weather.koniz")
    assert weather is not None
    assert weather.attributes.get("current_precipitation") == 0.5
    assert weather.attributes.get("precipitation_station") == "Belp"
    # Temperature still comes from the main station (BER).
    assert weather.attributes.get("temperature") == 19.5
