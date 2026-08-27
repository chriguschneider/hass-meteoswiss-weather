"""Tests for the pure-Python OGD forecast client (issue #5).

HA-free (ADR-0001): mock aiohttp with ``aioresponses`` and replay trimmed
real fixtures. Never hits the network. Covers point resolution, STAC
latest-complete-run selection, and the order-independent daily parser.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, date, datetime
from pathlib import Path

import aiohttp
import aiohttp.resolver
import pytest
from aioresponses import aioresponses

from custom_components.meteoswiss_weather.ogd import (
    BulkCsvBackend,
    ForecastPoint,
    OgdParseError,
    fetch_points,
    latest_run,
    nearest_point,
    parse_daily,
    parse_hourly,
    points_for_postal_code,
)
from custom_components.meteoswiss_weather.ogd.const import (
    COLLECTION_FORECAST,
    DAILY_REQUIRED_PARAMS,
    HOURLY_REQUIRED_PARAMS,
    META_POINT_URL,
    stac_items_url,
)

FIXTURES = Path(__file__).parent / "fixtures"
ITEMS_URL = stac_items_url(COLLECTION_FORECAST)
RUN_TS = "202608270200"
ASSET_BASE = (
    "https://data.geo.admin.ch/ch.meteoschweiz.ogd-local-forecasting/20260827-ch"
)

# Köniz (3098) city centre; point 309800 is the postal-code centre there.
KONIZ_LAT, KONIZ_LON = 46.9245, 7.4147


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _fixture_text(name: str) -> str:
    return _fixture_bytes(name).decode("iso-8859-1")


def _asset_url(param: str) -> str:
    return f"{ASSET_BASE}/vnut12.lssw.{RUN_TS}.{param}.csv"


@pytest.fixture
async def session():
    """A ClientSession with a thread-free resolver (see test_ogd_stations)."""
    connector = aiohttp.TCPConnector(resolver=aiohttp.resolver.ThreadedResolver())
    async with aiohttp.ClientSession(connector=connector) as client:
        yield client


def _load_points() -> list[ForecastPoint]:
    import csv
    import io

    text = _fixture_text("ogd-local-forecasting_meta_point.csv")
    out: list[ForecastPoint] = []
    for r in csv.DictReader(io.StringIO(text), delimiter=";"):
        out.append(
            ForecastPoint(
                point_id=int(r["point_id"]),
                point_type_id=int(r["point_type_id"]),
                postal_code=r["postal_code"],
                name=r["point_name"],
                lat=float(r["point_coordinates_wgs84_lat"]),
                lon=float(r["point_coordinates_wgs84_lon"]),
                height_masl=float(r["point_height_masl"]),
            )
        )
    return out


# --- point metadata ---------------------------------------------------------


async def test_fetch_points_parses_the_fixture(session) -> None:
    with aioresponses() as mock:
        mock.get(META_POINT_URL, status=200,
                 body=_fixture_bytes("ogd-local-forecasting_meta_point.csv"))
        points = await fetch_points(session)

    by_id = {(p.point_id, p.point_type_id): p for p in points}
    koeniz = by_id[(309800, 2)]
    # Latin-1 decoding: the umlaut survives the round-trip.
    assert koeniz.name == "Köniz"
    assert koeniz.postal_code == "3098"
    assert koeniz.height_masl == 595.0
    assert koeniz.lat == pytest.approx(46.9245)
    # The station point (type 1) and mountain point (type 3) are parsed too.
    assert (1, 1) in by_id
    assert by_id[(5000, 3)].name == "Jungfraujoch"


async def test_fetch_points_missing_header_raises_parse_error(session) -> None:
    with aioresponses() as mock:
        mock.get(META_POINT_URL, status=200, body=b"not;a;point;file\n1;2;3;4\n")
        with pytest.raises(OgdParseError):
            await fetch_points(session)


def test_points_for_postal_code_orders_n00_first() -> None:
    points = _load_points()
    matches = points_for_postal_code(points, 3098)
    assert [p.point_id for p in matches] == [309800, 309801]
    # A string postal code resolves the same way.
    assert points_for_postal_code(points, "3098") == matches
    # Only type-2 postal-code centres are returned.
    assert all(p.point_type_id == 2 for p in matches)


def test_points_for_postal_code_unknown_is_empty() -> None:
    assert points_for_postal_code(_load_points(), 9999) == []


def test_nearest_point_picks_the_koeniz_centre() -> None:
    points = _load_points()
    near = nearest_point(points, KONIZ_LAT, KONIZ_LON)
    assert near.point_id == 309800
    assert near.point_type_id == 2


def test_nearest_point_respects_point_type() -> None:
    points = _load_points()
    # Restricted to mountain points, only Jungfraujoch qualifies.
    assert nearest_point(points, KONIZ_LAT, KONIZ_LON, point_type=3).point_id == 5000


def test_nearest_point_no_candidate_raises() -> None:
    with pytest.raises(OgdParseError):
        nearest_point(_load_points(), KONIZ_LAT, KONIZ_LON, point_type=9)


# --- STAC latest-complete-run selection ------------------------------------


async def test_latest_run_skips_the_incomplete_newer_run(session) -> None:
    with aioresponses() as mock:
        mock.get(ITEMS_URL, status=200,
                 body=_fixture_bytes("ogd-local-forecasting_items.json"))
        run = await latest_run(session, COLLECTION_FORECAST, DAILY_REQUIRED_PARAMS)

    # 03:00 is newer but lacks jp2000d0, so the complete 02:00 run wins.
    assert run.timestamp == datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    assert run.asset_url("jp2000d0").endswith(
        "vnut12.lssw.202608270200.jp2000d0.csv"
    )


async def test_latest_run_no_complete_run_raises(session) -> None:
    document = {
        "type": "FeatureCollection",
        "features": [
            {"assets": {
                "vnut12.lssw.202608270300.tre200px.csv": {"href": "x"},
            }},
        ],
    }
    with aioresponses() as mock:
        mock.get(ITEMS_URL, status=200, body=json.dumps(document))
        with pytest.raises(OgdParseError):
            await latest_run(session, COLLECTION_FORECAST, DAILY_REQUIRED_PARAMS)


async def test_latest_run_follows_pagination(session) -> None:
    page2 = f"{ITEMS_URL}?cursor=2"
    doc1 = {
        "type": "FeatureCollection",
        "features": [
            {"assets": {
                f"vnut12.lssw.{RUN_TS}.tre200px.csv": {"href": _asset_url("tre200px")},
                f"vnut12.lssw.{RUN_TS}.tre200pn.csv": {"href": _asset_url("tre200pn")},
            }},
        ],
        "links": [{"rel": "next", "href": page2}],
    }
    doc2 = {
        "type": "FeatureCollection",
        "features": [
            {"assets": {
                f"vnut12.lssw.{RUN_TS}.rka150p0.csv": {"href": _asset_url("rka150p0")},
                f"vnut12.lssw.{RUN_TS}.jp2000d0.csv": {"href": _asset_url("jp2000d0")},
            }},
        ],
    }
    with aioresponses() as mock:
        mock.get(ITEMS_URL, status=200, body=json.dumps(doc1))
        mock.get(page2, status=200, body=json.dumps(doc2))
        run = await latest_run(session, COLLECTION_FORECAST, DAILY_REQUIRED_PARAMS)

    # The run is only complete once assets from both pages are merged.
    assert run.timestamp == datetime(2026, 8, 27, 2, 0, tzinfo=UTC)


# --- daily parser -----------------------------------------------------------


def _daily_texts() -> dict[str, str]:
    return {
        param: _fixture_text(f"vnut12.lssw.{RUN_TS}.{param}.csv")
        for param in DAILY_REQUIRED_PARAMS
    }


def _koeniz_point() -> ForecastPoint:
    return next(
        p for p in _load_points() if (p.point_id, p.point_type_id) == (309800, 2)
    )


def test_parse_daily_nine_days_for_309800() -> None:
    daily = parse_daily(_daily_texts(), _koeniz_point())
    assert len(daily) == 9
    first = daily[0]
    # Values are the real 309800;2 (Köniz) rows from run 2026-08-27 02:00 UTC.
    assert first.date == date(2026, 8, 27)
    assert first.temp_max == 29.3
    assert first.temp_min == 16.9
    assert first.precipitation == 0.0
    assert first.symbol == 2
    # Every day carries the four modelled fields.
    assert all(
        d.temp_max is not None and d.temp_min is not None
        and d.precipitation is not None and d.symbol is not None
        for d in daily
    )
    # Sorted ascending by date, spanning nine consecutive days.
    assert [d.date for d in daily] == sorted(d.date for d in daily)
    assert daily[-1].date == date(2026, 9, 4)
    assert daily[-1].symbol == 1


def test_parse_daily_postal_code_point_has_all_measurements() -> None:
    """Issue #34 regression: a postal-code point must get real temperatures.

    The daily ``d``-variant files (``tre200dx``/``tre200dn``/``rka150d0``)
    carry station rows only, so parsing the default postal-code centre against
    them yields ``temp_max=temp_min=precipitation=None`` and just a symbol.
    Parsing the real ``p``-variant fixtures for point ``309800;2`` must give a
    non-``None`` value for all three on every one of the nine days. This test
    fails on ``master`` (old codes + fabricated fixtures could hide it); it can
    only pass because the fixtures are trimmed real files.
    """
    point = _koeniz_point()
    assert point.point_type_id == 2  # a postal-code centre, the config default
    daily = parse_daily(_daily_texts(), point)
    assert len(daily) == 9
    for day in daily:
        assert day.temp_max is not None, day.date
        assert day.temp_min is not None, day.date
        assert day.precipitation is not None, day.date
        assert day.symbol is not None, day.date


def test_parse_daily_is_order_independent() -> None:
    """Shuffle every file's data rows; the merged result must not change."""
    rng = random.Random(1234)
    shuffled: dict[str, str] = {}
    for param, text in _daily_texts().items():
        lines = text.splitlines()
        header, body = lines[0], lines[1:]
        rng.shuffle(body)
        shuffled[param] = "\n".join([header, *body]) + "\n"

    point = _koeniz_point()
    assert parse_daily(shuffled, point) == parse_daily(_daily_texts(), point)


def test_parse_daily_discriminates_on_point_type_id() -> None:
    """A row with the right id but wrong type must be ignored."""
    text = (
        "point_id;point_type_id;Date;tre200px\n"
        "309800;1;202608270000;99.0\n"   # same id, station type -> ignored
        "309800;2;202608270000;21.5\n"
    )
    daily = parse_daily({"tre200px": text}, _koeniz_point())
    assert len(daily) == 1
    assert daily[0].temp_max == 21.5


def test_parse_daily_leaves_missing_parameter_none() -> None:
    """A day present only in some files keeps the absent fields at None."""
    texts = {
        "tre200px": (
            "point_id;point_type_id;Date;tre200px\n309800;2;202608270000;21.5\n"
        ),
    }
    daily = parse_daily(texts, _koeniz_point())
    assert len(daily) == 1
    assert daily[0].temp_max == 21.5
    assert daily[0].temp_min is None
    assert daily[0].symbol is None


# --- backend ----------------------------------------------------------------


async def test_bulk_backend_fetch_daily(session) -> None:
    with aioresponses() as mock:
        mock.get(ITEMS_URL, status=200,
                 body=_fixture_bytes("ogd-local-forecasting_items.json"))
        for param in DAILY_REQUIRED_PARAMS:
            mock.get(_asset_url(param), status=200,
                     body=_fixture_bytes(f"vnut12.lssw.{RUN_TS}.{param}.csv"))
        backend = BulkCsvBackend(session)
        daily = await backend.fetch_daily(_koeniz_point())

    assert len(daily) == 9
    assert daily[0].temp_max == 29.3
    assert daily[0].symbol == 2


# --- hourly parser ----------------------------------------------------------


def _hourly_texts() -> dict[str, str]:
    return {
        param: _fixture_text(f"vnut12.lssw.{RUN_TS}.{param}.csv")
        for param in HOURLY_REQUIRED_PARAMS
    }


def test_parse_hourly_24h_for_309800() -> None:
    hourly = parse_hourly(_hourly_texts(), _koeniz_point())
    assert len(hourly) == 24

    first = hourly[0]
    # The first hour is the top of 2026-08-27 00:00 UTC (aware).
    assert first.time == datetime(2026, 8, 27, 0, 0, tzinfo=UTC)
    assert first.temperature == 10.0
    assert first.precipitation == 0.0
    assert first.symbol == 1
    assert first.wind_speed_kmh == 5.0
    assert first.gust_kmh == 8.0
    assert first.wind_bearing == 180

    # Every field of the modelled set is populated for every hour.
    assert all(
        h.temperature is not None
        and h.precipitation is not None
        and h.symbol is not None
        and h.wind_speed_kmh is not None
        and h.gust_kmh is not None
        and h.wind_bearing is not None
        for h in hourly
    )
    # Sorted ascending, 24 consecutive hours ending at 23:00.
    assert [h.time for h in hourly] == sorted(h.time for h in hourly)
    assert hourly[-1].time == datetime(2026, 8, 27, 23, 0, tzinfo=UTC)

    # Hour 12 carries the distinctive rainy symbol (7), proving per-hour
    # symbols survive the merge.
    noon = next(h for h in hourly if h.time.hour == 12)
    assert noon.symbol == 7
    assert noon.temperature == 16.0


def test_parse_hourly_is_order_independent() -> None:
    """Shuffle every file's data rows; the merged result must not change."""
    rng = random.Random(4321)
    shuffled: dict[str, str] = {}
    for param, text in _hourly_texts().items():
        lines = text.splitlines()
        header, body = lines[0], lines[1:]
        rng.shuffle(body)
        shuffled[param] = "\n".join([header, *body]) + "\n"

    point = _koeniz_point()
    assert parse_hourly(shuffled, point) == parse_hourly(_hourly_texts(), point)


def test_parse_hourly_discriminates_on_point_type_id() -> None:
    """A row with the right id but wrong type must be ignored."""
    text = (
        "point_id;point_type_id;Date;tre200h0\n"
        "309800;1;202608270000;99.0\n"   # same id, station type -> ignored
        "309800;2;202608270000;12.5\n"
    )
    hourly = parse_hourly({"tre200h0": text}, _koeniz_point())
    assert len(hourly) == 1
    assert hourly[0].temperature == 12.5


# --- backend (hourly) -------------------------------------------------------


def _hourly_asset_url(param: str) -> str:
    return f"{ASSET_BASE}/vnut12.lssw.{RUN_TS}.{param}.csv"


async def test_bulk_backend_fetch_hourly(session) -> None:
    with aioresponses() as mock:
        mock.get(ITEMS_URL, status=200,
                 body=_fixture_bytes("ogd-local-forecasting_items.json"))
        for param in HOURLY_REQUIRED_PARAMS:
            mock.get(_hourly_asset_url(param), status=200,
                     body=_fixture_bytes(f"vnut12.lssw.{RUN_TS}.{param}.csv"))
        backend = BulkCsvBackend(session)
        hourly = await backend.fetch_hourly(_koeniz_point())

    assert len(hourly) == 24
    assert hourly[0].temperature == 10.0
    assert hourly[0].wind_bearing == 180
