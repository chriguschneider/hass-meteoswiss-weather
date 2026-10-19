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
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.meteoswiss_weather import (
    MeteoSwissRuntimeData,
    SnowStationRuntimeData,
)
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
    SLF_DATA_SERVICE_URL,
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
    # All SNOW_FLAT stations must be listed (no distance cap, issue #205).
    for code in ("STH2", "JAU2", "SHE2", "SCH2", "CSL2", "DAV2"):
        assert code in codes
    # Options are sorted by distance; STH2 is closest to Bern so it's first.
    assert codes[0] == "STH2"
    # Labels carry elevation and distance.
    sth2_label = next(o["label"] for o in options if o["value"] == "STH2")
    assert "1791 m" in sth2_label
    assert "km" in sth2_label


async def test_snow_station_far_station_selectable(hass: HomeAssistant) -> None:
    """A station far from the HA location is offered and can create an entry (#205).

    DAV2 (Davos area, ~190 km from Bern) is the most distant SNOW_FLAT station
    in the fixture.  Before the fix it would have been cut off by the 10-station
    cap; with the fix it must appear in the dropdown and can be submitted.
    """
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
    codes = [
        o["value"]
        for o in result["data_schema"].schema[CONF_IMIS_STATION_CODE].config["options"]
    ]
    assert "DAV2" in codes, "far station DAV2 must be in the dropdown"

    # Submitting DAV2 must create the entry, not raise StopIteration.
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
            result["flow_id"], user_input={CONF_IMIS_STATION_CODE: "DAV2"}
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_IMIS_STATION_CODE] == "DAV2"


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
# Runtime: snow-station entry builds the IMIS coordinator and sensors only
# (ADR-0011), and a point entry ignores leftover IMIS keys.
# ---------------------------------------------------------------------------


def _slf_calls(aioclient_mock: AiohttpClientMocker) -> int:
    """Number of requests to the SLF measurement API recorded so far."""
    return sum(
        1
        for _method, url, *_ in aioclient_mock.mock_calls
        if "measurement-api.slf.ch" in url.host
    )


def _ogd_calls(aioclient_mock: AiohttpClientMocker) -> int:
    """Number of requests to the MeteoSwiss open-data host recorded so far."""
    return sum(
        1
        for _method, url, *_ in aioclient_mock.mock_calls
        if "data.geo.admin.ch" in url.host
    )


def _snow_entity_id(hass: HomeAssistant, key: str, code: str = "STH2") -> str | None:
    """Resolve a snow-station sensor's entity id from its ``imis-<code>_<key>``."""
    registry = er.async_get(hass)
    return registry.async_get_entity_id("sensor", DOMAIN, f"imis-{code}_{key}")


def _point_entity_id(hass: HomeAssistant, key: str) -> str | None:
    """Resolve a point entry's sensor entity id from its ``2-309800_<key>``."""
    registry = er.async_get(hass)
    return registry.async_get_entity_id("sensor", DOMAIN, f"2-309800_{key}")


def _point_entry_with_leftover_imis() -> MockConfigEntry:
    """A point (postal-code) entry still carrying v0.5.0–v0.6.2 IMIS keys.

    ADR-0011 says such keys are ignored: no IMIS coordinator is built and no SLF
    request is made for a point entry.
    """
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
            # Leftover keys from before ADR-0011 — must be ignored.
            CONF_IMIS_STATION_CODE: "STH2",
            CONF_IMIS_STATION_NAME: "Vorderstocken",
            CONF_IMIS_STATION_ELEVATION: 1791.0,
        },
        title="Köniz",
    )


async def test_snow_station_builds_imis_only_zero_ogd(
    hass: HomeAssistant,
    mock_slf: AiohttpClientMocker,
) -> None:
    """A snow-station entry builds the IMIS coordinator and sensors, no OGD."""
    entry = _snow_station_entry("STH2")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = entry.runtime_data
    # Snow-station runtime shape: only the IMIS coordinator (ADR-0011).
    assert isinstance(runtime, SnowStationRuntimeData)
    assert runtime.imis_coordinator is not None
    # The SLF API was polled; data.geo.admin.ch was never touched.
    assert _slf_calls(mock_slf) >= 1
    assert _ogd_calls(mock_slf) == 0

    # No weather entity is created for a snow-station entry.
    assert not [
        s for s in hass.states.async_entity_ids() if s.startswith("weather.")
    ]

    # The sensors live on the station's own device (name = label, model = elevation).
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_device(identifiers={(DOMAIN, "imis-STH2")})
    assert device is not None
    assert device.name == "Vorderstocken"
    assert device.model == "IMIS snow station, 1791 m"
    assert device.manufacturer == "SLF"
    assert device.configuration_url == SLF_DATA_SERVICE_URL

    # The elevation sensor is static and always shows 1791.
    elev_id = _snow_entity_id(hass, "elevation")
    assert elev_id is not None
    assert elev_id == "sensor.vorderstocken_elevation"
    elev_state = hass.states.get(elev_id)
    assert elev_state is not None
    assert float(elev_state.state) == 1791.0
    assert elev_state.attributes["attribution"] == ATTRIBUTION_SLF

    # The snow-height sensor reads the measured HS (1.0 cm) with SLF attribution.
    snow_id = _snow_entity_id(hass, "snow_height")
    assert snow_id is not None
    assert snow_id == "sensor.vorderstocken_snow_height"
    snow_state = hass.states.get(snow_id)
    assert snow_state is not None
    assert float(snow_state.state) == 1.0
    assert snow_state.attributes["attribution"] == ATTRIBUTION_SLF
    assert snow_state.attributes.get("station") == "Vorderstocken"
    assert snow_state.attributes.get("elevation") == 1791.0

    # The modelled new-snow sensor reads HN_1D (0.0 cm).
    new_snow_id = _snow_entity_id(hass, "new_snow_24h")
    assert new_snow_id is not None
    new_snow_state = hass.states.get(new_snow_id)
    assert new_snow_state is not None
    assert float(new_snow_state.state) == 0.0
    assert new_snow_state.attributes["attribution"] == ATTRIBUTION_SLF


async def test_point_entry_ignores_leftover_imis_keys(
    hass: HomeAssistant,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A point entry builds no IMIS coordinator and removes stale snow sensors."""
    entry = _point_entry_with_leftover_imis()
    entry.add_to_hass(hass)

    # A stale snow sensor left on the point device by v0.5.0–v0.6.2.
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "2-309800_snow_height",
        config_entry=entry,
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = entry.runtime_data
    # Point entry runtime: no IMIS coordinator at all (ADR-0011).
    assert isinstance(runtime, MeteoSwissRuntimeData)
    assert not hasattr(runtime, "imis_coordinator")
    # No SLF request was made for a point entry.
    assert _slf_calls(mock_ogd) == 0
    # The stale snow sensor was removed by the registry cleanup.
    assert _point_entity_id(hass, "snow_height") is None
    # And no snow-station device was created.
    dev_reg = dr.async_get(hass)
    assert dev_reg.async_get_device(identifiers={(DOMAIN, "imis-STH2")}) is None


async def test_point_entry_makes_no_slf_request_or_repair_issue(
    hass: HomeAssistant,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A point entry (even with leftover keys) makes no SLF request, no issue."""
    entry = _point_entry_with_leftover_imis()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert isinstance(entry.runtime_data, MeteoSwissRuntimeData)
    assert _slf_calls(mock_ogd) == 0
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, _no_data_issue_id(entry)) is None


async def test_snow_station_unload_and_reload(
    hass: HomeAssistant,
    mock_slf: AiohttpClientMocker,
) -> None:
    """A snow-station entry unloads cleanly and reloads to the same state."""
    entry = _snow_station_entry("STH2")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    snow_id = _snow_entity_id(hass, "snow_height")
    assert snow_id is not None
    assert float(hass.states.get(snow_id).state) == 1.0

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get(snow_id).state == "unavailable"

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert isinstance(entry.runtime_data, SnowStationRuntimeData)
    assert float(hass.states.get(snow_id).state) == 1.0


async def test_point_entry_unload_and_reload(
    hass: HomeAssistant,
    mock_ogd: AiohttpClientMocker,
) -> None:
    """A point entry unloads and reloads cleanly (unchanged behaviour)."""
    entry = _imis_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.koniz_temperature").state == "19.5"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get("sensor.koniz_temperature").state == "unavailable"

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert isinstance(entry.runtime_data, MeteoSwissRuntimeData)
    assert hass.states.get("sensor.koniz_temperature").state == "19.5"


def _imis_entry(
    code: str = "STH2",
    name: str = "Vorderstocken",
    elevation: float | None = 1791.0,
) -> MockConfigEntry:
    """A point (Köniz) entry carrying leftover IMIS keys (ignored, ADR-0011)."""
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


# ---------------------------------------------------------------------------
# Station outages on a snow-station entry: held value, placeholder, one warning
# per day, repair issue (ADR-0009 amendment, issue #187; ADR-0011 entry type)
# ---------------------------------------------------------------------------


_SNOW_HEIGHT = "sensor.vorderstocken_snow_height"
_NEW_SNOW = "sensor.vorderstocken_new_snow_24_h_modelled"
_NO_DATA_BODY = b'{"code": "NO_DATA"}'
_COORD_LATEST = (
    "custom_components.meteoswiss_weather.coordinator.fetch_imis_latest"
)


def _no_data_issue_id(entry: MockConfigEntry) -> str:
    """Repair issue id the coordinator posts for this entry's silent station."""
    return f"imis_no_data_{entry.entry_id}"


async def test_slf_failure_shows_placeholder(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A failing SLF fetch shows the marked placeholder, never unavailable."""
    aioclient_mock.get(measurements_url("STH2"), status=500)
    aioclient_mock.get(DAILY_SNOW_URL, status=500)

    entry = _snow_station_entry("STH2")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = entry.runtime_data
    assert isinstance(runtime, SnowStationRuntimeData)
    assert runtime.imis_coordinator.last_update_success is False

    snow_id = _snow_entity_id(hass, "snow_height")
    assert snow_id is not None
    snow_state = hass.states.get(snow_id)
    assert snow_state is not None
    assert float(snow_state.state) == 0.0
    assert snow_state.attributes["placeholder"] is True
    assert snow_state.attributes["measured_at"] is None
    assert snow_state.attributes["attribution"] == ATTRIBUTION_SLF
    assert snow_state.attributes.get("station") == "Vorderstocken"


async def test_first_setup_no_data_shows_marked_placeholder(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A station silent from the first poll shows a marked 0, never an ERROR."""
    aioclient_mock.get(measurements_url("STH2"), status=404, content=_NO_DATA_BODY)
    aioclient_mock.get(DAILY_SNOW_URL, content=b"[]")

    entry = _snow_station_entry("STH2")
    entry.add_to_hass(hass)
    with caplog.at_level(logging.DEBUG):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    runtime = entry.runtime_data
    assert isinstance(runtime, SnowStationRuntimeData)
    # NO_DATA is handled gracefully: the coordinator "succeeds".
    assert runtime.imis_coordinator.last_update_success is True

    for entity_id in (_SNOW_HEIGHT, _NEW_SNOW):
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        assert float(state.state) == 0.0
        assert state.attributes["placeholder"] is True
        assert state.attributes["measured_at"] is None
        assert state.attributes["attribution"] == ATTRIBUTION_SLF

    assert not [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and ("STH2" in r.getMessage())
    ]


async def test_held_value_and_one_warning_per_day(
    hass: HomeAssistant,
    mock_slf: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After a real value, an outage holds it and warns once a day, again next day."""
    day0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    day1 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

    entry = _snow_station_entry("STH2")
    entry.add_to_hass(hass)
    with freeze_time(day0):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    runtime = entry.runtime_data
    assert isinstance(runtime, SnowStationRuntimeData)
    coordinator = runtime.imis_coordinator
    first = hass.states.get(_SNOW_HEIGHT)
    assert first is not None
    assert float(first.state) == 1.0
    assert first.attributes["placeholder"] is False
    assert first.attributes["measured_at"] == _STH2_MEASURED_AT

    with patch(_COORD_LATEST, side_effect=ImisNoDataError("no data")):
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

            held = hass.states.get(_SNOW_HEIGHT)
            assert float(held.state) == 1.0
            assert held.attributes["placeholder"] is False
            assert held.attributes["measured_at"] == _STH2_MEASURED_AT

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
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A restored non-placeholder value is shown after restart while silent."""
    aioclient_mock.get(measurements_url("STH2"), status=404, content=_NO_DATA_BODY)
    aioclient_mock.get(DAILY_SNOW_URL, content=b"[]")

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

    entry = _snow_station_entry("STH2")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(_SNOW_HEIGHT)
    assert state is not None
    assert float(state.state) == 5.0
    assert state.attributes["placeholder"] is False
    assert state.attributes["measured_at"] == "2026-09-30T06:00:00+00:00"


async def test_no_data_creates_repair_issue_cleared_by_real_value(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """NO_DATA raises the repair issue immediately; a real value clears it."""
    aioclient_mock.get(measurements_url("STH2"), status=404, content=_NO_DATA_BODY)
    aioclient_mock.get(DAILY_SNOW_URL, content=b"[]")

    entry = _snow_station_entry("STH2")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = ir.async_get(hass)
    issue_id = _no_data_issue_id(entry)
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "imis_no_data"
    assert issue.translation_placeholders["code"] == "STH2"

    good = ImisMeasurement(
        time=datetime(2026, 10, 21, 6, 0, tzinfo=UTC), snow_height_cm=3.0
    )
    runtime = entry.runtime_data
    assert isinstance(runtime, SnowStationRuntimeData)
    coordinator = runtime.imis_coordinator
    with patch(_COORD_LATEST, return_value=good):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert registry.async_get_issue(DOMAIN, issue_id) is None
    state = hass.states.get(_SNOW_HEIGHT)
    assert float(state.state) == 3.0
    assert state.attributes["placeholder"] is False


# ---------------------------------------------------------------------------
# Every measured IMIS value as a sensor (ADR-0009 amendment, #196)
# ---------------------------------------------------------------------------

# Newest-row values of ``slf-imis_sth2_measurements.json`` (2026-10-19 06:00 UTC)
# and of STH2's ``slf-imis_daily-snow.json`` row, keyed by sensor ``key``.
_STH2_NEWEST: dict[str, float] = {
    "snow_height": 1.0,
    "temperature": 5.118,
    "humidity": 80.2,
    "wind_speed": 0.221,
    "gust_speed": 1.232,
    "wind_bearing": 265.5,
    "wind_bearing_sd": 35.79,
    "snow_surface_temperature": -0.621,
    "snow_temperature_0cm": 6.659,
    "snow_temperature_25cm": 0.294,
    "snow_temperature_50cm": 2.808,
    "snow_temperature_100cm": 3.86,
    "reflected_radiation": 0.0,
    # Daily snow row: HN_1D and modelled HS are both 0.0 in the fixture.
    "new_snow_24h": 0.0,
    "snow_height_modelled": 0.0,
}

# The three snow values that fall back to the 0 placeholder before any real
# value; every other sensor stays ``unknown`` (ADR-0009 amendment).
_PLACEHOLDER_ZERO_KEYS = {"snow_height", "new_snow_24h", "snow_height_modelled"}


async def test_every_imis_value_is_a_sensor(
    hass: HomeAssistant,
    mock_slf: AiohttpClientMocker,
) -> None:
    """Every IMIS value is an enabled sensor with the newest row's value."""
    entry = _snow_station_entry("STH2")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    for key, expected in _STH2_NEWEST.items():
        entity_id = _snow_entity_id(hass, key)
        assert entity_id is not None, key
        # Enabled by default: a state exists and the registry entry is enabled.
        reg_entry = registry.async_get(entity_id)
        assert reg_entry is not None and reg_entry.disabled_by is None, key
        state = hass.states.get(entity_id)
        assert state is not None, key
        assert float(state.state) == expected, key
        # Every IMIS sensor carries the SLF attribution and the station markers.
        assert state.attributes["attribution"] == ATTRIBUTION_SLF, key
        assert state.attributes.get("station") == "Vorderstocken", key
        assert state.attributes.get("elevation") == 1791.0, key
        assert state.attributes["placeholder"] is False, key

    # Wind keeps the SLF's native m/s (no km/h conversion, #196).
    for wind_key in ("wind_speed", "gust_speed"):
        state = hass.states.get(_snow_entity_id(hass, wind_key))
        assert state.attributes["unit_of_measurement"] == "m/s", wind_key


async def test_non_snow_values_unknown_before_first_value(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Before the first value: snow sensors show 0, the rest show unknown."""
    aioclient_mock.get(measurements_url("STH2"), status=404, content=_NO_DATA_BODY)
    aioclient_mock.get(DAILY_SNOW_URL, content=b"[]")

    entry = _snow_station_entry("STH2")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for key in _STH2_NEWEST:
        state = hass.states.get(_snow_entity_id(hass, key))
        assert state is not None, key
        if key in _PLACEHOLDER_ZERO_KEYS:
            assert float(state.state) == 0.0, key
            assert state.attributes["placeholder"] is True, key
            assert state.attributes["measured_at"] is None, key
        else:
            # A placeholder 0 would read as a real measurement, so stay unknown.
            assert state.state == "unknown", key
            assert state.attributes["placeholder"] is True, key
            assert state.attributes["measured_at"] is None, key


async def test_non_snow_value_held_through_outage(
    hass: HomeAssistant,
    mock_slf: AiohttpClientMocker,
) -> None:
    """A real temperature, then NO_DATA: the last real value is held."""
    entry = _snow_station_entry("STH2")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    temp_id = _snow_entity_id(hass, "temperature")
    first = hass.states.get(temp_id)
    assert float(first.state) == 5.118
    assert first.attributes["placeholder"] is False
    assert first.attributes["measured_at"] == _STH2_MEASURED_AT

    runtime = entry.runtime_data
    assert isinstance(runtime, SnowStationRuntimeData)
    with patch(_COORD_LATEST, side_effect=ImisNoDataError("no data")):
        await runtime.imis_coordinator.async_refresh()
        await hass.async_block_till_done()

    # The temperature holds its last real value, never dropping to unknown/0.
    held = hass.states.get(temp_id)
    assert float(held.state) == 5.118
    assert held.attributes["placeholder"] is False
    assert held.attributes["measured_at"] == _STH2_MEASURED_AT


# ---------------------------------------------------------------------------
# Elevation sensor (#206): static, always available, entity id without elevation
# ---------------------------------------------------------------------------


async def test_elevation_sensor_value_and_entity_id(
    hass: HomeAssistant,
    mock_slf: AiohttpClientMocker,
) -> None:
    """Elevation sensor shows 1791 for STH2; entity id has no elevation suffix."""
    entry = _snow_station_entry("STH2")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    elev_id = _snow_entity_id(hass, "elevation")
    assert elev_id == "sensor.vorderstocken_elevation"
    state = hass.states.get(elev_id)
    assert state is not None
    assert float(state.state) == 1791.0
    assert state.attributes["attribution"] == ATTRIBUTION_SLF
    # No placeholder/measured_at attributes on the static elevation sensor.
    assert "placeholder" not in state.attributes
    assert "measured_at" not in state.attributes


async def test_elevation_sensor_stays_available_during_no_data(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Elevation sensor stays at 1791 when the station delivers NO_DATA."""
    aioclient_mock.get(measurements_url("STH2"), status=404, content=_NO_DATA_BODY)
    aioclient_mock.get(DAILY_SNOW_URL, content=b"[]")

    entry = _snow_station_entry("STH2")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    elev_id = _snow_entity_id(hass, "elevation")
    assert elev_id is not None
    state = hass.states.get(elev_id)
    assert state is not None
    assert float(state.state) == 1791.0
