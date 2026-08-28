"""Tests for the pure-Python OGD station client (issue #4).

No Home Assistant here: the ``ogd`` package is HA-free (ADR-0001), so these
mock aiohttp with ``aioresponses`` and replay trimmed real fixtures. Never
hits the network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import aiohttp.resolver
import pytest
from aioresponses import CallbackResult, aioresponses

from custom_components.meteoswiss_weather.ogd import (
    OgdConnectionError,
    OgdParseError,
    fetch_current,
    fetch_datainventory,
    fetch_stations,
    get_text,
    nearest_stations,
)
from custom_components.meteoswiss_weather.ogd.const import (
    META_DATAINVENTORY_URL,
    META_STATIONS_URL,
    station_now_url,
)

FIXTURES = Path(__file__).parent / "fixtures"
BER_URL = station_now_url("ber")

# Bern city centre; BER (Bern / Zollikofen) is the closest station.
BERN_LAT, BERN_LON = 46.9218, 7.4143


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


async def test_fetch_stations_parses_the_fixture(session) -> None:
    with aioresponses() as mock:
        mock.get(META_STATIONS_URL, status=200,
                 body=_fixture_bytes("ogd-smn_meta_stations.csv"))
        stations = await fetch_stations(session)

    by_abbr = {s.abbr: s for s in stations}
    assert "BER" in by_abbr
    assert "ABO" in by_abbr
    # The precipitation-only station is parsed like any other point.
    assert "RAG" in by_abbr
    ber = by_abbr["BER"]
    assert ber.name == "Bern / Zollikofen"
    assert ber.canton == "BE"
    assert ber.height_masl == 552.0
    assert ber.lat == pytest.approx(46.990765)
    assert ber.lon == pytest.approx(7.464061)


async def test_fetch_stations_missing_header_raises_parse_error(session) -> None:
    with aioresponses() as mock:
        mock.get(META_STATIONS_URL, status=200, body=b"not;a;station;file\n1;2;3;4\n")
        with pytest.raises(OgdParseError):
            await fetch_stations(session)


def _load_stations() -> list:
    import csv
    import io

    text = _fixture_bytes("ogd-smn_meta_stations.csv").decode("cp1252")
    from custom_components.meteoswiss_weather.ogd.models import Station

    rows = csv.DictReader(io.StringIO(text), delimiter=";")
    out = []
    for r in rows:
        out.append(
            Station(
                abbr=r["station_abbr"],
                name=r["station_name"],
                canton=r["station_canton"],
                lat=float(r["station_coordinates_wgs84_lat"]),
                lon=float(r["station_coordinates_wgs84_lon"]),
                height_masl=float(r["station_height_masl"]),
            )
        )
    return out


def test_nearest_stations_puts_ber_first() -> None:
    stations = _load_stations()
    nearest = nearest_stations(stations, BERN_LAT, BERN_LON)
    assert nearest[0].abbr == "BER"
    assert len(nearest) == 3


def test_nearest_stations_respects_limit() -> None:
    stations = _load_stations()
    assert len(nearest_stations(stations, BERN_LAT, BERN_LON, limit=1)) == 1
    assert len(nearest_stations(stations, BERN_LAT, BERN_LON, limit=5)) == 5


async def test_fetch_current_happy_path(session) -> None:
    with aioresponses() as mock:
        mock.get(BER_URL, status=200, body=_fixture_bytes("ogd-smn_ber_t_now.csv"))
        obs = await fetch_current(session, "ber")

    # The last row (00:50) has no temperature, so 00:40 is the latest.
    assert obs.station_abbr == "BER"
    assert obs.timestamp == datetime(2026, 8, 26, 0, 40, tzinfo=UTC)
    assert obs.temperature == 19.5
    assert obs.humidity == 88.0
    assert obs.dew_point == 17.2
    assert obs.pressure_qfe == 951.5
    assert obs.pressure_qff == 1014.8
    assert obs.wind_speed_kmh == 4.0
    assert obs.wind_bearing == 245.0
    assert obs.gust_kmh == 5.5
    assert obs.precipitation_10min == 0.2
    assert obs.sunshine_10min == 0.0
    assert obs.global_radiation == 12.0


async def test_fetch_current_404_raises_connection_error(session) -> None:
    with aioresponses() as mock:
        mock.get(BER_URL, status=404)
        with pytest.raises(OgdConnectionError):
            await fetch_current(session, "ber")


async def test_fetch_current_garbage_raises_parse_error(session) -> None:
    with aioresponses() as mock:
        mock.get(BER_URL, status=200, body=b"\xff\xfe not a csv at all")
        with pytest.raises(OgdParseError):
            await fetch_current(session, "ber")


async def test_get_text_304_returns_cached_body(session) -> None:
    body = _fixture_bytes("ogd-smn_ber_t_now.csv")
    last_modified = "Wed, 26 Aug 2026 00:41:00 GMT"
    seen: dict[str, str | None] = {}

    def first(url, **kwargs):
        return CallbackResult(
            status=200, body=body,
            headers={"ETag": '"v1"', "Last-Modified": last_modified},
        )

    def revalidate(url, **kwargs):
        headers = kwargs.get("headers") or {}
        seen["if_none_match"] = headers.get("If-None-Match")
        seen["if_modified_since"] = headers.get("If-Modified-Since")
        return CallbackResult(status=304)

    with aioresponses() as mock:
        mock.get(BER_URL, callback=first)
        mock.get(BER_URL, callback=revalidate)
        cache = await get_text(session, BER_URL, encoding="cp1252")
        assert cache.etag == '"v1"'
        assert cache.last_modified == last_modified
        first_body = cache.body

        same = await get_text(session, BER_URL, cache=cache, encoding="cp1252")

    # The revalidation sent the conditional headers and reused the body.
    assert seen["if_none_match"] == '"v1"'
    assert seen["if_modified_since"] == last_modified
    assert same is cache
    assert same.body == first_body


async def test_get_text_200_updates_cache_in_place(session) -> None:
    with aioresponses() as mock:
        mock.get(BER_URL, status=200, body=b"old",
                 headers={"ETag": '"v1"'})
        mock.get(BER_URL, status=200, body=b"new",
                 headers={"ETag": '"v2"'})
        cache = await get_text(session, BER_URL, encoding="cp1252")
        assert cache.body == "old"
        refreshed = await get_text(session, BER_URL, cache=cache, encoding="cp1252")

    assert refreshed is cache
    assert cache.body == "new"
    assert cache.etag == '"v2"'


# ---------------------------------------------------------------------------
# Data inventory (issue #46)
# ---------------------------------------------------------------------------


async def test_fetch_datainventory_full_station(session) -> None:
    """BER in the fixture has all 11 sensor parameter codes."""
    with aioresponses() as mock:
        mock.get(
            META_DATAINVENTORY_URL,
            status=200,
            body=_fixture_bytes("ogd-smn_meta_datainventory.csv"),
        )
        inventory = await fetch_datainventory(session)

    assert "BER" in inventory
    ber_params = inventory["BER"]
    # All 11 parameter codes that sensor.py maps to observation fields.
    expected = {
        "tre200s0", "ure200s0", "tde200s0", "pp0qffs0", "prestas0",
        "fu3010z0", "dkl010z0", "fu3010z1", "rre150z0", "sre000z0", "gre000z0",
    }
    assert expected == ber_params


async def test_fetch_datainventory_reduced_station(session) -> None:
    """RAG in the fixture has only precipitation."""
    with aioresponses() as mock:
        mock.get(
            META_DATAINVENTORY_URL,
            status=200,
            body=_fixture_bytes("ogd-smn_meta_datainventory.csv"),
        )
        inventory = await fetch_datainventory(session)

    assert "RAG" in inventory
    assert inventory["RAG"] == frozenset({"rre150z0"})


async def test_fetch_datainventory_missing_header_raises_parse_error(session) -> None:
    with aioresponses() as mock:
        mock.get(META_DATAINVENTORY_URL, status=200, body=b"bad;header\n1;2\n")
        with pytest.raises(OgdParseError):
            await fetch_datainventory(session)


async def test_fetch_datainventory_station_not_in_inventory_absent(session) -> None:
    """A station absent from the inventory is not in the returned dict."""
    with aioresponses() as mock:
        mock.get(
            META_DATAINVENTORY_URL,
            status=200,
            body=b"station_abbr;parameter;measurement_since\nBER;tre200s0;1981-10-01\n",
        )
        inventory = await fetch_datainventory(session)

    assert "ZZZ" not in inventory
