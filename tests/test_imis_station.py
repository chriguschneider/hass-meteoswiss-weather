"""Tests for the SLF IMIS snow station (ADR-0009 / ADR-0011).

Three concerns are covered:

- the setup flow creates a snow-station entry (its own entry type, ADR-0011),
  stores the correct data shape, aborts on duplicates, and never calls
  ``_load_metadata`` (no MeteoSwiss request on the snow-station path);
- the station step of a point entry no longer offers an IMIS field;
- with an IMIS station configured, the snow sensors read from the SLF upstream
  (measured snow height and modelled new snow), carry the SLF attribution, and
  the MeteoSwiss entities are unaffected — including when the SLF fetch fails.

Upstream responses are replayed from ``tests/fixtures`` via the ``mock_ogd``
and ``mock_ogd_imis`` fixtures (conftest.py); no test hits the network.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun import freeze_time
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.meteoswiss_weather.const import (
    ATTRIBUTION_SLF,
    CONF_ENTRY_TYPE,
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
    ENTRY_TYPE_SNOW_STATION,
)
from custom_components.meteoswiss_weather.ogd import ForecastPoint, Station
from custom_components.meteoswiss_weather.slf import (
    ImisMeasurement,
    ImisNoDataError,
    ImisStation,
)
from custom_components.meteoswiss_weather.slf.const import (
    DAILY_SNOW_URL,
    measurements_url,
)

# The newest row in ``slf-imis_sth2_measurements.json`` (HS 1.0 cm at this time);
# the measured ``snow_height`` sensor surfaces this as its ``measured_at``.
_STH2_MEASURED_AT = "2026-10-19T06:00:00+00:00"

_FLOW = "custom_components.meteoswiss_weather.config_flow"

_CONF_MODE = "forecast_mode"
_MODE_POSTAL_CODE = "postal_code"
_MODE_SNOW_STATION = "snow_station"

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
# IMIS stations used in config-flow tests. The nearest SNOW_FLAT to Bern
# (46.948, 7.447) in distance order: STH2, JAU2, SHE2, SCH2, CSL2, DAV2.
# JAU1 is WIND (excluded); BOG4 is SNOW_SLOPE (excluded).
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

# Daily-snow codes matching the fixture (SCH2 is absent — currently no data).
_DAILY_SNOW_CODES = {"BOG4", "CSL2", "DAV2", "JAU2", "SHE2", "STH2"}

_BERN_LAT = 46.948
_BERN_LON = 7.447


def _snow_entry_patch():
    """Context managers that stub the SLF data fetch for the snow_station step."""
    return (
        patch(f"{_FLOW}.async_get_clientsession", return_value=MagicMock()),
        patch(
            f"{_FLOW}.fetch_imis_stations",
            new=AsyncMock(return_value=_IMIS_STATIONS),
        ),
        patch(
            f"{_FLOW}.fetch_imis_daily_snow_codes",
            new=AsyncMock(return_value=_DAILY_SNOW_CODES),
        ),
    )


# ---------------------------------------------------------------------------
# Snow-station entry type: setup flow (ADR-0011)
# ---------------------------------------------------------------------------


async def test_snow_station_setup_happy_path(hass: HomeAssistant) -> None:
    """Full snow-station setup: mode → snow_station step → CREATE_ENTRY."""
    hass.config.latitude = _BERN_LAT
    hass.config.longitude = _BERN_LON

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "user"

    # slice 2 handles snow entries; patch setup here to isolate the flow test.
    with (
        patch(
            "custom_components.meteoswiss_weather.async_setup_entry",
            return_value=True,
        ),
        _snow_entry_patch()[0],
        _snow_entry_patch()[1],
        _snow_entry_patch()[2],
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={_CONF_MODE: _MODE_SNOW_STATION}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "snow_station"

        # The nearest station (STH2) is pre-selected; submit it.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_IMIS_STATION_CODE: "STH2"}
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Vorderstocken"
    data = result["data"]
    assert data[CONF_ENTRY_TYPE] == ENTRY_TYPE_SNOW_STATION
    assert data[CONF_IMIS_STATION_CODE] == "STH2"
    assert data[CONF_IMIS_STATION_NAME] == "Vorderstocken"
    assert data[CONF_IMIS_STATION_ELEVATION] == 1791.0

    # Unique id is "imis-<code>".
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].unique_id == "imis-STH2"


async def test_snow_station_list_sorted_by_distance(hass: HomeAssistant) -> None:
    """The snow_station step lists SNOW_FLAT stations sorted by distance."""
    hass.config.latitude = _BERN_LAT
    hass.config.longitude = _BERN_LON

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with _snow_entry_patch()[0], _snow_entry_patch()[1], _snow_entry_patch()[2]:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={_CONF_MODE: _MODE_SNOW_STATION}
        )

    assert result["step_id"] == "snow_station"
    options = result["data_schema"].schema[CONF_IMIS_STATION_CODE].config["options"]
    codes = [o["value"] for o in options]
    # JAU1 (WIND) and BOG4 (SNOW_SLOPE) must not appear.
    assert "JAU1" not in codes
    assert "BOG4" not in codes
    # All five SNOW_FLAT stations are within limit=10; all must be listed.
    for code in ("STH2", "JAU2", "SHE2", "SCH2", "CSL2", "DAV2"):
        assert code in codes
    # Options are sorted by distance; STH2 is closest to Bern so it's first.
    assert codes[0] == "STH2"
    # Labels carry elevation and distance.
    sth2_label = next(o["label"] for o in options if o["value"] == "STH2")
    assert "1791 m" in sth2_label
    assert "km" in sth2_label


async def test_snow_station_no_data_label(hass: HomeAssistant) -> None:
    """SCH2 absent from daily-snow codes → its label ends with 'currently no data'."""
    hass.config.latitude = _BERN_LAT
    hass.config.longitude = _BERN_LON

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with _snow_entry_patch()[0], _snow_entry_patch()[1], _snow_entry_patch()[2]:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={_CONF_MODE: _MODE_SNOW_STATION}
        )

    options = {
        o["value"]: o["label"]
        for o in result["data_schema"].schema[CONF_IMIS_STATION_CODE].config["options"]
    }
    # SCH2 is absent from _DAILY_SNOW_CODES → labelled "currently no data".
    assert "currently no data" in options["SCH2"]
    # Stations that have data must not carry the note.
    for code in ("STH2", "JAU2", "SHE2", "CSL2", "DAV2"):
        assert "currently no data" not in options[code]


async def test_snow_station_duplicate_aborts(hass: HomeAssistant) -> None:
    """Setting up the same snow-station code twice aborts as already_configured."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="imis-STH2",
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_SNOW_STATION,
            CONF_IMIS_STATION_CODE: "STH2",
            CONF_IMIS_STATION_NAME: "Vorderstocken",
            CONF_IMIS_STATION_ELEVATION: 1791.0,
        },
        title="Vorderstocken",
    )
    existing.add_to_hass(hass)

    hass.config.latitude = _BERN_LAT
    hass.config.longitude = _BERN_LON

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with _snow_entry_patch()[0], _snow_entry_patch()[1], _snow_entry_patch()[2]:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={_CONF_MODE: _MODE_SNOW_STATION}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_IMIS_STATION_CODE: "STH2"}
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_snow_station_no_metadata_request(hass: HomeAssistant) -> None:
    """The snow-station path never calls _load_metadata (no MeteoSwiss request)."""
    hass.config.latitude = _BERN_LAT
    hass.config.longitude = _BERN_LON

    load_meta_mock = AsyncMock(return_value=True)

    with (
        patch(
            "custom_components.meteoswiss_weather.async_setup_entry",
            return_value=True,
        ),
        patch(
            f"{_FLOW}.MeteoSwissWeatherConfigFlow._load_metadata", load_meta_mock
        ),
        _snow_entry_patch()[0],
        _snow_entry_patch()[1],
        _snow_entry_patch()[2],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={_CONF_MODE: _MODE_SNOW_STATION}
        )
        await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_IMIS_STATION_CODE: "STH2"}
        )

    load_meta_mock.assert_not_called()


async def test_snow_station_cannot_connect(hass: HomeAssistant) -> None:
    """An SLF list failure shows cannot_connect on the snow_station step."""
    from custom_components.meteoswiss_weather.slf import SlfError

    hass.config.latitude = _BERN_LAT
    hass.config.longitude = _BERN_LON

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with (
        patch(f"{_FLOW}.async_get_clientsession", return_value=MagicMock()),
        patch(
            f"{_FLOW}.fetch_imis_stations",
            new=AsyncMock(side_effect=SlfError("down")),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={_CONF_MODE: _MODE_SNOW_STATION}
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "snow_station"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_snow_station_nearest_preselected(hass: HomeAssistant) -> None:
    """The nearest snow station to the HA location is the dropdown default."""
    hass.config.latitude = _BERN_LAT
    hass.config.longitude = _BERN_LON

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with _snow_entry_patch()[0], _snow_entry_patch()[1], _snow_entry_patch()[2]:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={_CONF_MODE: _MODE_SNOW_STATION}
        )

    assert result["step_id"] == "snow_station"
    default = result["data_schema"]({})[CONF_IMIS_STATION_CODE]
    assert default == "STH2"


# ---------------------------------------------------------------------------
# Snow-station reconfigure (ADR-0011)
# ---------------------------------------------------------------------------


def _snow_station_entry(code: str = "STH2") -> MockConfigEntry:
    """A configured snow-station entry."""
    names = {"STH2": "Vorderstocken", "JAU2": "Fochsen", "SHE2": "Schibe"}
    elevations = {"STH2": 1791.0, "JAU2": 1707.0, "SHE2": 1852.0}
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"imis-{code}",
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_SNOW_STATION,
            CONF_IMIS_STATION_CODE: code,
            CONF_IMIS_STATION_NAME: names[code],
            CONF_IMIS_STATION_ELEVATION: elevations[code],
        },
        title=names[code],
    )


async def test_snow_station_reconfigure_switches_station(
    hass: HomeAssistant,
) -> None:
    """Reconfigure of a snow-station entry switches to a different station."""
    hass.config.latitude = _BERN_LAT
    hass.config.longitude = _BERN_LON

    entry = _snow_station_entry("STH2")
    entry.add_to_hass(hass)

    with _snow_entry_patch()[0], _snow_entry_patch()[1], _snow_entry_patch()[2]:
        result = await entry.start_reconfigure_flow(hass)
        # Reconfigure of a snow-station entry goes straight to snow_station step.
        assert result["step_id"] == "snow_station"
        # The currently configured station is pre-selected.
        default = result["data_schema"]({})[CONF_IMIS_STATION_CODE]
        assert default == "STH2"

        with patch.object(hass.config_entries, "async_schedule_reload"):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input={CONF_IMIS_STATION_CODE: "JAU2"}
            )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_IMIS_STATION_CODE] == "JAU2"
    assert entry.data[CONF_IMIS_STATION_NAME] == "Fochsen"
    assert entry.data[CONF_IMIS_STATION_ELEVATION] == 1707.0
    assert entry.unique_id == "imis-JAU2"


async def test_snow_station_reconfigure_duplicate_aborts(
    hass: HomeAssistant,
) -> None:
    """Reconfiguring a snow-station entry onto a code another entry owns aborts."""
    hass.config.latitude = _BERN_LAT
    hass.config.longitude = _BERN_LON

    other = _snow_station_entry("JAU2")
    other.add_to_hass(hass)
    entry = _snow_station_entry("STH2")
    entry.add_to_hass(hass)

    with _snow_entry_patch()[0], _snow_entry_patch()[1], _snow_entry_patch()[2]:
        result = await entry.start_reconfigure_flow(hass)
        assert result["step_id"] == "snow_station"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_IMIS_STATION_CODE: "JAU2"}
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Point entry: station step has no IMIS field (ADR-0011)
# ---------------------------------------------------------------------------


async def test_station_step_has_no_imis_field(hass: HomeAssistant) -> None:
    """The station step for a point entry no longer offers an IMIS station field."""
    hass.config.latitude = _BERN_LAT
    hass.config.longitude = _BERN_LON

    async def _mock_load_ok(self) -> bool:
        self._all_points = _POINTS
        self._all_stations = _STATIONS
        return True

    with patch(
        f"{_FLOW}.MeteoSwissWeatherConfigFlow._load_metadata", _mock_load_ok
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={_CONF_MODE: "postal_code"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_POSTAL_CODE: 3098}
        )

    assert result["step_id"] == "station"
    assert CONF_IMIS_STATION_CODE not in result["data_schema"].schema


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


async def test_slf_failure_shows_placeholder_meteoswiss_unaffected(
    hass: HomeAssistant,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A failing SLF fetch shows the marked placeholder, never unavailable.

    With no value ever received (first setup, SLF down), the snow sensors show
    ``0`` with ``placeholder: true`` and ``measured_at: null`` rather than going
    unavailable (ADR-0009 amendment, #187). MeteoSwiss entities are untouched.
    """
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

    # The snow sensors show the marked placeholder, not "unavailable".
    snow_id = _entity_id(hass, "snow_height")
    assert snow_id is not None
    snow_state = hass.states.get(snow_id)
    assert snow_state is not None
    assert float(snow_state.state) == 0.0
    assert snow_state.attributes["placeholder"] is True
    assert snow_state.attributes["measured_at"] is None
    # The SLF attribution and station attributes are still present.
    assert snow_state.attributes["attribution"] == ATTRIBUTION_SLF
    assert snow_state.attributes.get("station") == "Vorderstocken"

    # The MeteoSwiss temperature sensor is unaffected.
    temp_state = hass.states.get("sensor.koniz_temperature")
    assert temp_state is not None
    assert temp_state.state == "19.5"


# ---------------------------------------------------------------------------
# Station outages: held value, placeholder, one warning per day, repair issue
# (ADR-0009 amendment, issue #187)
# ---------------------------------------------------------------------------


_SNOW_HEIGHT = "sensor.koniz_snow_height"
_NEW_SNOW = "sensor.koniz_new_snow_24_h_modelled"
_NO_DATA_BODY = b'{"code": "NO_DATA"}'
_COORD_LATEST = (
    "custom_components.meteoswiss_weather.coordinator.fetch_imis_latest"
)


def _no_data_issue_id(entry: MockConfigEntry) -> str:
    """Repair issue id the coordinator posts for this entry's silent station."""
    return f"imis_no_data_{entry.entry_id}"


async def test_first_setup_no_data_shows_marked_placeholder(
    hass: HomeAssistant,
    mock_ogd: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A station silent from the first poll shows a marked 0, never an ERROR."""
    # Measurement answers 404 NO_DATA; daily-snow has no row for the station.
    mock_ogd.get(measurements_url("STH2"), status=404, content=_NO_DATA_BODY)
    mock_ogd.get(DAILY_SNOW_URL, content=b"[]")

    entry = _imis_entry()
    entry.add_to_hass(hass)
    with caplog.at_level(logging.DEBUG):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # NO_DATA is handled gracefully: the coordinator "succeeds" (no UpdateFailed).
    coordinator = entry.runtime_data.imis_coordinator
    assert coordinator is not None
    assert coordinator.last_update_success is True

    # Both snow sensors show the marked placeholder.
    for entity_id in (_SNOW_HEIGHT, _NEW_SNOW):
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        assert float(state.state) == 0.0
        assert state.attributes["placeholder"] is True
        assert state.attributes["measured_at"] is None
        assert state.attributes["attribution"] == ATTRIBUTION_SLF

    # No ERROR-level log mentions the silent station.
    assert not [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and ("STH2" in r.getMessage())
    ]

    # The MeteoSwiss temperature sensor is unaffected.
    assert hass.states.get("sensor.koniz_temperature").state == "19.5"


async def test_held_value_and_one_warning_per_day(
    hass: HomeAssistant,
    mock_ogd_imis: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After a real value, an outage holds it and warns once a day, again next day."""
    day0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    day1 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

    entry = _imis_entry()
    entry.add_to_hass(hass)
    with freeze_time(day0):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.imis_coordinator
    assert coordinator is not None
    # Real value from the fixture, with its measured_at.
    first = hass.states.get(_SNOW_HEIGHT)
    assert float(first.state) == 1.0
    assert first.attributes["placeholder"] is False
    assert first.attributes["measured_at"] == _STH2_MEASURED_AT

    with patch(_COORD_LATEST, side_effect=ImisNoDataError("no data")):
        # Two polls on the same local day → exactly one warning.
        with freeze_time(day0):
            caplog.clear()
            with caplog.at_level(logging.WARNING):
                await coordinator.async_refresh()
                await hass.async_block_till_done()
                await coordinator.async_refresh()
                await hass.async_block_till_done()
            same_day = [
                r for r in caplog.records if "has delivered no data" in r.getMessage()
            ]
            assert len(same_day) == 1
            assert all(r.levelno == logging.WARNING for r in same_day)

            # The value is held: unchanged state and unchanged measured_at.
            held = hass.states.get(_SNOW_HEIGHT)
            assert float(held.state) == 1.0
            assert held.attributes["placeholder"] is False
            assert held.attributes["measured_at"] == _STH2_MEASURED_AT

        # The next local day warns again.
        with freeze_time(day1):
            caplog.clear()
            with caplog.at_level(logging.WARNING):
                await coordinator.async_refresh()
                await hass.async_block_till_done()
            next_day = [
                r for r in caplog.records if "has delivered no data" in r.getMessage()
            ]
            assert len(next_day) == 1


async def test_restored_real_value_held_while_silent(
    hass: HomeAssistant,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A restored non-placeholder value is shown after restart while silent."""
    mock_ogd.get(measurements_url("STH2"), status=404, content=_NO_DATA_BODY)
    mock_ogd.get(DAILY_SNOW_URL, content=b"[]")

    restored = State(
        _SNOW_HEIGHT,
        "5.0",
        {
            "station": "Vorderstocken",
            "elevation": 1791.0,
            "placeholder": False,
            "measured_at": "2026-09-30T06:00:00+00:00",
        },
    )
    extra = {"native_value": 5.0, "native_unit_of_measurement": "cm"}
    mock_restore_cache_with_extra_data(hass, [(restored, extra)])

    entry = _imis_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # The station is still silent, but the restored real value is shown.
    state = hass.states.get(_SNOW_HEIGHT)
    assert state is not None
    assert float(state.state) == 5.0
    assert state.attributes["placeholder"] is False
    assert state.attributes["measured_at"] == "2026-09-30T06:00:00+00:00"


async def test_no_data_creates_repair_issue_cleared_by_real_value(
    hass: HomeAssistant,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """NO_DATA raises the repair issue immediately; a real value clears it."""
    mock_ogd.get(measurements_url("STH2"), status=404, content=_NO_DATA_BODY)
    mock_ogd.get(DAILY_SNOW_URL, content=b"[]")

    entry = _imis_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = ir.async_get(hass)
    issue_id = _no_data_issue_id(entry)
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "imis_no_data"
    assert issue.translation_placeholders["code"] == "STH2"

    # A real measurement on the next poll clears the issue.
    good = ImisMeasurement(
        time=datetime(2026, 10, 21, 6, 0, tzinfo=UTC), snow_height_cm=3.0
    )
    coordinator = entry.runtime_data.imis_coordinator
    assert coordinator is not None
    with patch(_COORD_LATEST, return_value=good):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert registry.async_get_issue(DOMAIN, issue_id) is None
    state = hass.states.get(_SNOW_HEIGHT)
    assert float(state.state) == 3.0
    assert state.attributes["placeholder"] is False


async def test_unset_creates_no_repair_issue(
    hass: HomeAssistant,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """An entry without an IMIS station makes no SLF request and no repair issue."""
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
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, _no_data_issue_id(entry)) is None
