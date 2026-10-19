"""Tests for the pure-Python SLF IMIS client (ADR-0009, issue #173).

No Home Assistant here: the ``slf`` package is HA-free (ADR-0009), so
these mock aiohttp with ``aioresponses`` and replay the trimmed real
fixtures. Never hits the network.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, date, datetime
from pathlib import Path

import aiohttp
import aiohttp.resolver
import pytest
from aioresponses import aioresponses

from custom_components.meteoswiss_weather.ogd.http import CachedResponse
from custom_components.meteoswiss_weather.slf import (
    ImisDailySnow,
    ImisNoDataError,
    ImisStation,
    SlfConnectionError,
    SlfParseError,
    fetch_imis_daily_snow,
    fetch_imis_daily_snow_codes,
    fetch_imis_latest,
    fetch_imis_stations,
    nearest_snow_stations,
)
from custom_components.meteoswiss_weather.slf.const import (
    DAILY_SNOW_URL,
    SNOW_FLAT,
    STATIONS_URL,
    measurements_url,
)

FIXTURES = Path(__file__).parent / "fixtures"

# Köniz reference point (issue #173 test spec).
KONIZ_LAT, KONIZ_LON = 46.9245, 7.4147


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
async def session():
    """A ClientSession with a thread-free resolver.

    aiohttp's default aiodns resolver spins up a lingering pycares daemon
    thread that the pytest-homeassistant-custom-component cleanup check
    rejects. aioresponses never touches the connector, so a ThreadedResolver
    (which only creates threads when it actually resolves) leaves none.
    """
    connector = aiohttp.TCPConnector(resolver=aiohttp.resolver.ThreadedResolver())
    async with aiohttp.ClientSession(connector=connector) as client:
        yield client


# ---------------------------------------------------------------------------
# No Home Assistant imports in slf/
# ---------------------------------------------------------------------------


def test_slf_package_has_no_homeassistant_imports() -> None:
    """Every source file in slf/ must be free of ``homeassistant`` imports."""
    slf_dir = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "meteoswiss_weather"
        / "slf"
    )
    assert slf_dir.is_dir(), "slf/ package directory not found"
    violations: list[str] = []
    for py_file in sorted(slf_dir.glob("*.py")):
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("homeassistant"):
                        violations.append(f"{py_file.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith("homeassistant"):
                    violations.append(f"{py_file.name}: from {module} import ...")
    assert not violations, "HA imports found in slf/:\n" + "\n".join(violations)


# ---------------------------------------------------------------------------
# fetch_imis_stations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_stations_returns_all(session) -> None:
    """All eight entries in the fixture are returned."""
    with aioresponses() as mock:
        mock.get(STATIONS_URL, body=_fixture_bytes("slf-imis_stations.json"))
        stations = await fetch_imis_stations(session)
    assert len(stations) == 8


@pytest.mark.asyncio
async def test_fetch_stations_types(session) -> None:
    """Station types are parsed from the fixture."""
    with aioresponses() as mock:
        mock.get(STATIONS_URL, body=_fixture_bytes("slf-imis_stations.json"))
        stations = await fetch_imis_stations(session)
    types = {s.code: s.type for s in stations}
    assert types["STH2"] == "SNOW_FLAT"
    assert types["JAU1"] == "WIND"
    assert types["BOG4"] == "SNOW_SLOPE"


@pytest.mark.asyncio
async def test_fetch_stations_fields(session) -> None:
    """Station fields are mapped correctly."""
    with aioresponses() as mock:
        mock.get(STATIONS_URL, body=_fixture_bytes("slf-imis_stations.json"))
        stations = await fetch_imis_stations(session)
    sth2 = next(s for s in stations if s.code == "STH2")
    assert sth2.label == "Vorderstocken"
    assert sth2.canton == "BE"
    assert abs(sth2.lat - 46.6820486806) < 1e-6
    assert abs(sth2.lon - 7.5196488888) < 1e-6
    assert sth2.elevation == pytest.approx(1791.0)


@pytest.mark.asyncio
async def test_fetch_stations_connection_error(session) -> None:
    """A network error raises SlfConnectionError."""
    with aioresponses() as mock:
        mock.get(STATIONS_URL, exception=aiohttp.ClientConnectionError("down"))
        with pytest.raises(SlfConnectionError):
            await fetch_imis_stations(session)


# ---------------------------------------------------------------------------
# nearest_snow_stations
# ---------------------------------------------------------------------------


def _load_stations() -> list[ImisStation]:
    data = json.loads(_fixture("slf-imis_stations.json"))
    from custom_components.meteoswiss_weather.slf.client import _parse_station
    return [_parse_station(obj) for obj in data]


def test_nearest_snow_stations_filters_to_snow_flat() -> None:
    """Only SNOW_FLAT stations are returned."""
    stations = _load_stations()
    result = nearest_snow_stations(stations, KONIZ_LAT, KONIZ_LON, limit=10)
    assert all(s.type == SNOW_FLAT for s in result)


def test_nearest_snow_stations_order_and_codes() -> None:
    """Nearest three to Köniz are STH2, JAU2, SHE2 (issue #173 spec)."""
    stations = _load_stations()
    result = nearest_snow_stations(stations, KONIZ_LAT, KONIZ_LON, limit=3)
    assert [s.code for s in result] == ["STH2", "JAU2", "SHE2"]


def test_nearest_snow_stations_limit() -> None:
    """``limit`` caps the result list; None (default) returns all SNOW_FLAT."""
    stations = _load_stations()
    assert len(nearest_snow_stations(stations, KONIZ_LAT, KONIZ_LON, limit=1)) == 1
    assert len(nearest_snow_stations(stations, KONIZ_LAT, KONIZ_LON, limit=2)) == 2
    snow_flat_count = sum(1 for s in stations if s.type == SNOW_FLAT)
    assert len(nearest_snow_stations(stations, KONIZ_LAT, KONIZ_LON)) == snow_flat_count


def test_nearest_snow_stations_excludes_wind() -> None:
    """JAU1 (WIND) must not appear in results."""
    stations = _load_stations()
    result = nearest_snow_stations(stations, KONIZ_LAT, KONIZ_LON, limit=10)
    assert all(s.code != "JAU1" for s in result)


# ---------------------------------------------------------------------------
# fetch_imis_latest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_latest_returns_newest_row(session) -> None:
    """The last row in the fixture is returned."""
    url = measurements_url("STH2")
    with aioresponses() as mock:
        mock.get(url, body=_fixture_bytes("slf-imis_sth2_measurements.json"))
        measurement = await fetch_imis_latest(session, "STH2")
    assert measurement.time == datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
    assert measurement.snow_height_cm == pytest.approx(1.0)
    assert measurement.air_temperature == pytest.approx(5.118)
    assert measurement.wind_speed_ms == pytest.approx(0.221)
    assert measurement.gust_ms == pytest.approx(1.232)
    assert measurement.wind_bearing == pytest.approx(265.5)


@pytest.mark.asyncio
async def test_fetch_latest_new_fields_sth2(session) -> None:
    """All new ImisMeasurement fields are parsed from the newest STH2 row."""
    url = measurements_url("STH2")
    with aioresponses() as mock:
        mock.get(url, body=_fixture_bytes("slf-imis_sth2_measurements.json"))
        measurement = await fetch_imis_latest(session, "STH2")
    # Newest row: 2026-10-19T06:00:00Z
    assert measurement.humidity == pytest.approx(80.2)
    assert measurement.wind_bearing_sd == pytest.approx(35.79)
    assert measurement.snow_surface_temperature == pytest.approx(-0.621)
    assert measurement.snow_temperature_0cm == pytest.approx(6.659)
    assert measurement.snow_temperature_25cm == pytest.approx(0.294)
    assert measurement.snow_temperature_50cm == pytest.approx(2.808)
    assert measurement.snow_temperature_100cm == pytest.approx(3.86)
    assert measurement.reflected_shortwave_radiation == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_fetch_latest_wind_station_hs_none(session) -> None:
    """A WIND station returns HS=null → snow_height_cm is None."""
    url = measurements_url("JAU1")
    with aioresponses() as mock:
        mock.get(url, body=_fixture_bytes("slf-imis_jau1_measurements.json"))
        measurement = await fetch_imis_latest(session, "JAU1")
    assert measurement.snow_height_cm is None
    assert measurement.air_temperature == pytest.approx(6.614)


@pytest.mark.asyncio
async def test_fetch_latest_wind_station_snow_fields_none(session) -> None:
    """A WIND station returns null snow fields → snow temperature fields are None."""
    url = measurements_url("JAU1")
    with aioresponses() as mock:
        mock.get(url, body=_fixture_bytes("slf-imis_jau1_measurements.json"))
        measurement = await fetch_imis_latest(session, "JAU1")
    assert measurement.snow_surface_temperature is None
    assert measurement.snow_temperature_0cm is None
    assert measurement.snow_temperature_25cm is None
    assert measurement.snow_temperature_50cm is None
    assert measurement.snow_temperature_100cm is None
    assert measurement.reflected_shortwave_radiation is None


@pytest.mark.asyncio
async def test_fetch_latest_304_keeps_cached_body(session) -> None:
    """A 304 response leaves the cached body and parses from it."""
    url = measurements_url("STH2")
    body = _fixture_bytes("slf-imis_sth2_measurements.json")
    cached = CachedResponse(body=body.decode(), etag='"v1"')
    with aioresponses() as mock:
        mock.get(url, status=304, headers={"ETag": '"v1"'})
        measurement = await fetch_imis_latest(session, "STH2", cache=cached)
    assert measurement.time == datetime(2026, 10, 19, 6, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_fetch_latest_http_400_raises_connection_error(session) -> None:
    """HTTP 400 (unknown code) raises SlfConnectionError."""
    url = measurements_url("NOPE")
    with aioresponses() as mock:
        mock.get(url, status=400)
        with pytest.raises(SlfConnectionError):
            await fetch_imis_latest(session, "NOPE")


@pytest.mark.asyncio
async def test_fetch_latest_empty_list_raises_parse_error(session) -> None:
    """An empty measurement list raises SlfParseError."""
    url = measurements_url("STH2")
    with aioresponses() as mock:
        mock.get(url, body=b"[]")
        with pytest.raises(SlfParseError):
            await fetch_imis_latest(session, "STH2")


# ---------------------------------------------------------------------------
# fetch_imis_daily_snow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_daily_snow_present_station(session) -> None:
    """STH2 is in the fixture; its row is returned correctly."""
    with aioresponses() as mock:
        mock.get(DAILY_SNOW_URL, body=_fixture_bytes("slf-imis_daily-snow.json"))
        result = await fetch_imis_daily_snow(session, "STH2")
    assert isinstance(result, ImisDailySnow)
    assert result.date == date(2026, 10, 18)
    assert result.snow_height_cm == pytest.approx(0.0)
    assert result.new_snow_24h_cm == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_fetch_daily_snow_absent_station_returns_none(session) -> None:
    """SCH2 has no row in the fixture; None is returned."""
    with aioresponses() as mock:
        mock.get(DAILY_SNOW_URL, body=_fixture_bytes("slf-imis_daily-snow.json"))
        result = await fetch_imis_daily_snow(session, "SCH2")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_daily_snow_all_null_station(session) -> None:
    """BOG4 has a row but all-null values; an ImisDailySnow with None fields."""
    with aioresponses() as mock:
        mock.get(DAILY_SNOW_URL, body=_fixture_bytes("slf-imis_daily-snow.json"))
        result = await fetch_imis_daily_snow(session, "BOG4")
    assert isinstance(result, ImisDailySnow)
    assert result.snow_height_cm is None
    assert result.new_snow_24h_cm is None


@pytest.mark.asyncio
async def test_fetch_daily_snow_304_keeps_cached_body(session) -> None:
    """A 304 response leaves the cached body and parses from it."""
    body = _fixture_bytes("slf-imis_daily-snow.json")
    cached = CachedResponse(body=body.decode(), etag='"v1"')
    with aioresponses() as mock:
        mock.get(DAILY_SNOW_URL, status=304, headers={"ETag": '"v1"'})
        result = await fetch_imis_daily_snow(session, "STH2", cache=cached)
    assert isinstance(result, ImisDailySnow)
    assert result.date == date(2026, 10, 18)


# ---------------------------------------------------------------------------
# ImisNoDataError: 404 + NO_DATA body (ADR-0009 amendment, issue #186)
# ---------------------------------------------------------------------------

_NO_DATA_BODY = b'{"code":"NO_DATA","title":"Not found","status":404}'


@pytest.mark.asyncio
async def test_fetch_latest_404_no_data_body_raises_imis_no_data_error(
    session,
) -> None:
    """HTTP 404 with a NO_DATA body raises ImisNoDataError."""
    url = measurements_url("SCH2")
    with aioresponses() as mock:
        mock.get(url, status=404, body=_NO_DATA_BODY)
        with pytest.raises(ImisNoDataError):
            await fetch_imis_latest(session, "SCH2")


@pytest.mark.asyncio
async def test_fetch_latest_404_no_data_is_subclass_of_connection_error(
    session,
) -> None:
    """ImisNoDataError is caught by except SlfConnectionError (subclass)."""
    url = measurements_url("SCH2")
    with aioresponses() as mock:
        mock.get(url, status=404, body=_NO_DATA_BODY)
        with pytest.raises(SlfConnectionError):
            await fetch_imis_latest(session, "SCH2")


@pytest.mark.asyncio
async def test_fetch_latest_404_without_no_data_body_raises_connection_error(
    session,
) -> None:
    """HTTP 404 without NO_DATA body raises SlfConnectionError, not ImisNoDataError."""
    url = measurements_url("SCH2")
    with aioresponses() as mock:
        mock.get(url, status=404, body=b'{"error": "not found"}')
        exc = None
        try:
            await fetch_imis_latest(session, "SCH2")
        except SlfConnectionError as e:
            exc = e
        assert exc is not None
        assert not isinstance(exc, ImisNoDataError)


@pytest.mark.asyncio
async def test_fetch_latest_404_empty_body_raises_connection_error(
    session,
) -> None:
    """HTTP 404 with a non-JSON body raises SlfConnectionError (not ImisNoDataError)."""
    url = measurements_url("SCH2")
    with aioresponses() as mock:
        mock.get(url, status=404, body=b"Not Found")
        exc = None
        try:
            await fetch_imis_latest(session, "SCH2")
        except SlfConnectionError as e:
            exc = e
        assert exc is not None
        assert not isinstance(exc, ImisNoDataError)


# ---------------------------------------------------------------------------
# fetch_imis_daily_snow_codes (ADR-0009 amendment, issue #186)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_daily_snow_codes_returns_present_codes(session) -> None:
    """Returns the set of station codes with a row in the fixture."""
    with aioresponses() as mock:
        mock.get(DAILY_SNOW_URL, body=_fixture_bytes("slf-imis_daily-snow.json"))
        codes = await fetch_imis_daily_snow_codes(session)
    # BOG4, CSL2, DAV2, JAU2, SHE2, STH2 are in the fixture; SCH2 is absent.
    assert "STH2" in codes
    assert "CSL2" in codes
    assert "SCH2" not in codes


@pytest.mark.asyncio
async def test_fetch_daily_snow_codes_excludes_absent_station(session) -> None:
    """SCH2 has no row in the fixture so it is absent from the returned set."""
    with aioresponses() as mock:
        mock.get(DAILY_SNOW_URL, body=_fixture_bytes("slf-imis_daily-snow.json"))
        codes = await fetch_imis_daily_snow_codes(session)
    assert "SCH2" not in codes


@pytest.mark.asyncio
async def test_fetch_daily_snow_codes_connection_error(session) -> None:
    """A network error raises SlfConnectionError."""
    with aioresponses() as mock:
        mock.get(
            DAILY_SNOW_URL, exception=aiohttp.ClientConnectionError("down")
        )
        with pytest.raises(SlfConnectionError):
            await fetch_imis_daily_snow_codes(session)
