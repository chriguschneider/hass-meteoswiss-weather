"""Tests for the pure-Python OGD pollen client (ADR-0005, issue #53).

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
from aioresponses import aioresponses

from custom_components.meteoswiss_weather.ogd import (
    OgdConnectionError,
    OgdParseError,
    PollenObservation,
    PollenStation,
    fetch_pollen_current,
    fetch_pollen_datainventory,
    fetch_pollen_parameters,
    fetch_pollen_stations,
    nearest_pollen_station,
)
from custom_components.meteoswiss_weather.ogd.const import (
    META_POLLEN_DATAINVENTORY_URL,
    META_POLLEN_PARAMETERS_URL,
    META_POLLEN_STATIONS_URL,
    pollen_now_url,
)

FIXTURES = Path(__file__).parent / "fixtures"
PBE_URL = pollen_now_url("pbe")

# Bern city centre; PBE (Bern pollen station) is the closest in the fixture.
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


# ---------------------------------------------------------------------------
# Station metadata
# ---------------------------------------------------------------------------


async def test_fetch_pollen_stations_parses_fixture(session) -> None:
    with aioresponses() as mock:
        mock.get(
            META_POLLEN_STATIONS_URL,
            status=200,
            body=_fixture_bytes("ogd-pollen_meta_stations.csv"),
        )
        stations = await fetch_pollen_stations(session)

    by_abbr = {s.abbr: s for s in stations}
    assert "PBE" in by_abbr
    assert "PBS" in by_abbr
    assert "PPY" in by_abbr
    pbe = by_abbr["PBE"]
    assert pbe.name == "Bern"
    assert pbe.canton == "BE"
    assert pbe.height_masl == 553.0
    assert pbe.lat == pytest.approx(46.990765)
    assert pbe.lon == pytest.approx(7.464061)


async def test_fetch_pollen_stations_missing_header_raises(session) -> None:
    with aioresponses() as mock:
        mock.get(
            META_POLLEN_STATIONS_URL, status=200, body=b"not;a;station;file\n1;2;3;4\n"
        )
        with pytest.raises(OgdParseError):
            await fetch_pollen_stations(session)


async def test_fetch_pollen_stations_returns_pollenstation_instances(session) -> None:
    with aioresponses() as mock:
        mock.get(
            META_POLLEN_STATIONS_URL,
            status=200,
            body=_fixture_bytes("ogd-pollen_meta_stations.csv"),
        )
        stations = await fetch_pollen_stations(session)

    assert all(isinstance(s, PollenStation) for s in stations)


# ---------------------------------------------------------------------------
# Nearest-station selection
# ---------------------------------------------------------------------------


def _load_pollen_stations() -> list[PollenStation]:
    import csv
    import io

    text = _fixture_bytes("ogd-pollen_meta_stations.csv").decode("cp1252")
    rows = csv.DictReader(io.StringIO(text), delimiter=";")
    out = []
    for r in rows:
        out.append(
            PollenStation(
                abbr=r["station_abbr"],
                name=r["station_name"],
                canton=r["station_canton"],
                lat=float(r["station_coordinates_wgs84_lat"]),
                lon=float(r["station_coordinates_wgs84_lon"]),
                height_masl=float(r["station_height_masl"]),
            )
        )
    return out


def test_nearest_pollen_station_puts_pbe_first() -> None:
    stations = _load_pollen_stations()
    nearest = nearest_pollen_station(stations, BERN_LAT, BERN_LON)
    assert nearest.abbr == "PBE"


def test_nearest_pollen_station_returns_single_closest() -> None:
    stations = _load_pollen_stations()
    result = nearest_pollen_station(stations, BERN_LAT, BERN_LON)
    assert isinstance(result, PollenStation)


# ---------------------------------------------------------------------------
# Parameter metadata
# ---------------------------------------------------------------------------


async def test_fetch_pollen_parameters_returns_english_names(session) -> None:
    with aioresponses() as mock:
        mock.get(
            META_POLLEN_PARAMETERS_URL,
            status=200,
            body=_fixture_bytes("ogd-pollen_meta_parameters.csv"),
        )
        params = await fetch_pollen_parameters(session)

    # The 7 hourly taxon codes must all be present with English descriptions.
    hourly_taxa = (
        "kabetuh0", "khpoach0", "kaalnuh0", "kacoryh0",
        "kafaguh0", "kafraxh0", "kaquerh0",
    )
    for code in hourly_taxa:
        assert code in params
        assert params[code]  # non-empty string

    assert "Birch" in params["kabetuh0"]
    assert "Grasses" in params["khpoach0"]
    assert "Alder" in params["kaalnuh0"]


async def test_fetch_pollen_parameters_missing_header_raises(session) -> None:
    with aioresponses() as mock:
        mock.get(META_POLLEN_PARAMETERS_URL, status=200, body=b"bad;header\n1;2\n")
        with pytest.raises(OgdParseError):
            await fetch_pollen_parameters(session)


# ---------------------------------------------------------------------------
# Data inventory
# ---------------------------------------------------------------------------


async def test_fetch_pollen_datainventory_full_station(session) -> None:
    """PBE in the fixture has all 7 hourly taxa."""
    with aioresponses() as mock:
        mock.get(
            META_POLLEN_DATAINVENTORY_URL,
            status=200,
            body=_fixture_bytes("ogd-pollen_meta_datainventory.csv"),
        )
        inventory = await fetch_pollen_datainventory(session)

    assert "PBE" in inventory
    expected = frozenset({
        "kaalnuh0", "kabetuh0", "kacoryh0", "kafaguh0",
        "kafraxh0", "kaquerh0", "khpoach0",
    })
    assert inventory["PBE"] == expected


async def test_fetch_pollen_datainventory_reduced_station(session) -> None:
    """PBS in the fixture has only 5 hourly taxa (no beech, no ash)."""
    with aioresponses() as mock:
        mock.get(
            META_POLLEN_DATAINVENTORY_URL,
            status=200,
            body=_fixture_bytes("ogd-pollen_meta_datainventory.csv"),
        )
        inventory = await fetch_pollen_datainventory(session)

    assert "PBS" in inventory
    assert "kafaguh0" not in inventory["PBS"]
    assert "kafraxh0" not in inventory["PBS"]
    assert "kabetuh0" in inventory["PBS"]


async def test_fetch_pollen_datainventory_excludes_ended_taxa(session) -> None:
    """PZH in the fixture has kaquerh0 ended; it must be excluded."""
    with aioresponses() as mock:
        mock.get(
            META_POLLEN_DATAINVENTORY_URL,
            status=200,
            body=_fixture_bytes("ogd-pollen_meta_datainventory.csv"),
        )
        inventory = await fetch_pollen_datainventory(session)

    assert "PZH" in inventory
    assert "kaquerh0" not in inventory["PZH"]
    assert "kabetuh0" in inventory["PZH"]


async def test_fetch_pollen_datainventory_missing_header_raises(session) -> None:
    with aioresponses() as mock:
        mock.get(
            META_POLLEN_DATAINVENTORY_URL, status=200, body=b"bad;header\n1;2\n"
        )
        with pytest.raises(OgdParseError):
            await fetch_pollen_datainventory(session)


# ---------------------------------------------------------------------------
# Current observations (_h_now.csv)
# ---------------------------------------------------------------------------


async def test_fetch_pollen_current_happy_path(session) -> None:
    with aioresponses() as mock:
        mock.get(
            PBE_URL, status=200, body=_fixture_bytes("ogd-pollen_pbe_h_now.csv")
        )
        obs = await fetch_pollen_current(session, "pbe")

    # The trailing 07:00 row has no values, so 06:00 is the latest complete row.
    assert obs.station_abbr == "PBE"
    assert obs.ts_utc == datetime(2026, 8, 28, 6, 0, tzinfo=UTC)
    assert isinstance(obs, PollenObservation)
    assert obs.values["kabetuh0"] == 3.0
    assert obs.values["khpoach0"] == 45.0
    assert obs.values["kaalnuh0"] == 0.0


async def test_fetch_pollen_current_taxon_cols_from_header(session) -> None:
    """Taxon columns are derived from the file header, not hard-coded."""
    with aioresponses() as mock:
        mock.get(
            PBE_URL, status=200, body=_fixture_bytes("ogd-pollen_pbe_h_now.csv")
        )
        obs = await fetch_pollen_current(session, "pbe")

    expected_taxa = {
        "kabetuh0", "khpoach0", "kaalnuh0", "kacoryh0",
        "kafaguh0", "kafraxh0", "kaquerh0",
    }
    assert set(obs.values.keys()) == expected_taxa


async def test_fetch_pollen_current_404_raises_connection_error(session) -> None:
    with aioresponses() as mock:
        mock.get(PBE_URL, status=404)
        with pytest.raises(OgdConnectionError):
            await fetch_pollen_current(session, "pbe")


async def test_fetch_pollen_current_missing_header_raises_parse_error(session) -> None:
    with aioresponses() as mock:
        mock.get(PBE_URL, status=200, body=b"not;a;pollen;file\n1;2;3;4\n")
        with pytest.raises(OgdParseError):
            await fetch_pollen_current(session, "pbe")


async def test_fetch_pollen_current_all_empty_rows_raises_parse_error(session) -> None:
    """A file with no complete measurement rows raises OgdParseError."""
    body = (
        b"station_abbr;reference_timestamp;kabetuh0;khpoach0\n"
        b"PBE;28.08.2026 06:00;;\n"
        b"PBE;28.08.2026 07:00;;\n"
    )
    with aioresponses() as mock:
        mock.get(PBE_URL, status=200, body=body)
        with pytest.raises(OgdParseError):
            await fetch_pollen_current(session, "pbe")


async def test_fetch_pollen_current_cp1252_station_name(session) -> None:
    """Non-ASCII characters in station names survive cp1252 decoding."""
    body = (
        "station_abbr;reference_timestamp;kabetuh0\n"
        "PZH;28.08.2026 06:00;5\n"
    ).encode("cp1252")
    with aioresponses() as mock:
        mock.get(pollen_now_url("pzh"), status=200, body=body)
        obs = await fetch_pollen_current(session, "pzh")

    assert obs.station_abbr == "PZH"
    assert obs.values["kabetuh0"] == 5.0
