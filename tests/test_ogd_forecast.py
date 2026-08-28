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
    aggregate_daily_wind,
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
    DAILY_WIND_PARAMS,
    HOURLY_PRECIP_PROBABILITY,
    HOURLY_RADIATION,
    HOURLY_REQUIRED_PARAMS,
    HOURLY_ZERO_DEGREE,
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
        # Wind block files — fixture is date-major so guardrail fires, wind = None.
        for param in DAILY_WIND_PARAMS:
            mock.get(_asset_url(param), status=200,
                     body=_fixture_bytes(f"vnut12.lssw.{RUN_TS}.{param}.csv"))
        backend = BulkCsvBackend(session)
        daily = await backend.fetch_daily(_koeniz_point())

    assert len(daily) == 9
    assert daily[0].temp_max == 29.3
    assert daily[0].symbol == 2
    # The fixture wind files are date-major → guardrail fires → daily wind is None.
    assert daily[0].native_wind_speed is None


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


# --- B7/B8/B10 additions (issue #55) ----------------------------------------


def test_parse_hourly_b7_precipitation_probability() -> None:
    """B7: rp0003i0 maps to precipitation_probability on HourlyForecast.

    The fixture sets 0% for most hours and 30% at hour 12 (rolling 3-hour
    window ending at Date, per docs/ogd.md §E4 rp0003i0 semantics).
    """
    text = _fixture_text(f"vnut12.lssw.{RUN_TS}.{HOURLY_PRECIP_PROBABILITY}.csv")
    hourly = parse_hourly({HOURLY_PRECIP_PROBABILITY: text}, _koeniz_point())
    assert len(hourly) == 24
    # Hour 0: 0% probability
    assert hourly[0].precipitation_probability == 0.0
    # Hour 12: 30% probability (the distinctive value set in the fixture)
    noon = next(h for h in hourly if h.time.hour == 12)
    assert noon.precipitation_probability == 30.0
    # Other hourly fields not in this file stay None.
    assert all(h.temperature is None for h in hourly)
    assert all(h.symbol is None for h in hourly)


def test_parse_hourly_b8_zero_degree_level() -> None:
    """B8: zprfr0hs maps to zero_degree_level on HourlyForecast.

    The fixture uses 2500 + h*5 for point 309800;2, so hour 0 = 2500 m.
    """
    text = _fixture_text(f"vnut12.lssw.{RUN_TS}.{HOURLY_ZERO_DEGREE}.csv")
    hourly = parse_hourly({HOURLY_ZERO_DEGREE: text}, _koeniz_point())
    assert len(hourly) == 24
    assert hourly[0].zero_degree_level == 2500.0
    # All 24 hours have a value.
    assert all(h.zero_degree_level is not None for h in hourly)


def test_parse_hourly_b10_radiation() -> None:
    """B10: gre000h0 maps to radiation on HourlyForecast.

    The fixture sets 0 W/m² at night and a positive value at hour 10 (daytime).
    """
    text = _fixture_text(f"vnut12.lssw.{RUN_TS}.{HOURLY_RADIATION}.csv")
    hourly = parse_hourly({HOURLY_RADIATION: text}, _koeniz_point())
    assert len(hourly) == 24
    # Hour 0 (UTC midnight = nighttime in CEST): 0 W/m²
    assert hourly[0].radiation == 0.0
    # Hour 10 (UTC): positive radiation (peak of the day in the fixture)
    h10 = next(h for h in hourly if h.time.hour == 10)
    assert h10.radiation is not None and h10.radiation > 0


def test_parse_hourly_b7_b8_b10_merged_with_full_set() -> None:
    """All three new fields survive the multi-file merge with the minimum set."""
    hourly = parse_hourly(_hourly_texts(), _koeniz_point())
    assert len(hourly) == 24

    # B7: precipitation probability from fixture
    assert hourly[0].precipitation_probability == 0.0
    noon = next(h for h in hourly if h.time.hour == 12)
    assert noon.precipitation_probability == 30.0

    # B8: zero-degree level from fixture (hour 0 = 2500 m)
    assert hourly[0].zero_degree_level == 2500.0

    # B10: radiation from fixture (hour 0 = 0 W/m², hour 10 > 0)
    assert hourly[0].radiation == 0.0
    h10 = next(h for h in hourly if h.time.hour == 10)
    assert h10.radiation is not None and h10.radiation > 0


# --- daily wind aggregation (issue #60) ------------------------------------


def _wind_texts() -> dict[str, str]:
    """The three wind fixture files as decoded text (same encoding as forecast)."""
    return {
        param: _fixture_text(f"vnut12.lssw.{RUN_TS}.{param}.csv")
        for param in DAILY_WIND_PARAMS
    }


def _point_major_wind_text(param: str, hours: int = 24) -> str:
    """Synthetic point-major CSV for one wind parameter.

    Two points sorted by point_id so classify_layout() returns POINT_MAJOR_ID.
    Point 1;1 carries values 10x the target; point 309800;2 starts at
    ``param_base`` and increments by 0.5 per hour.
    """
    # Value for point 309800;2: hour 0 = 5.0 (speed), 8.0 (gust), 180 (dir)
    param_base = {
        "fu3010h0": (5.0, 0.5),
        "fu3010h1": (8.0, 0.5),
        "dkl010h0": (180.0, 0.0),
    }.get(param, (0.0, 0.0))

    rows = [f"point_id;point_type_id;Date;{param}"]
    # Point 1;1 first (lower id): 24 rows
    for h in range(hours):
        stamp = datetime(2026, 8, 27, h, 0, tzinfo=UTC).strftime("%Y%m%d%H%M")
        rows.append(f"1;1;{stamp};{param_base[0] * 10 + h:.1f}")
    # Point 309800;2 second: 24 rows
    for h in range(hours):
        stamp = datetime(2026, 8, 27, h, 0, tzinfo=UTC).strftime("%Y%m%d%H%M")
        val = param_base[0] + h * param_base[1]
        rows.append(f"309800;2;{stamp};{val:.1f}")
    return "\n".join(rows) + "\n"


def test_aggregate_daily_wind_summer_utc_local_boundary() -> None:
    """Fixture data covers 2026-08-27 00:00–23:00 UTC; in CEST (UTC+2) this splits
    into local days 2026-08-27 (hours 0–21 UTC) and 2026-08-28 (hours 22–23 UTC).

    Wind speed for point 309800;2: hour n → 5.0 + n*0.5 km/h (monotone ↑).
    Gust: 8.0 + n*0.5.  Direction: always 180°.
    """
    result = aggregate_daily_wind(_wind_texts(), _koeniz_point())

    # Local day 2026-08-27: UTC hours 0–21 (22 hours in CEST)
    d27 = date(2026, 8, 27)
    assert d27 in result
    speed_27, gust_27, bearing_27 = result[d27]
    # Max speed at hour 21 UTC: 5.0 + 21*0.5 = 15.5
    assert speed_27 == pytest.approx(15.5)
    # Max gust at hour 21 UTC: 8.0 + 21*0.5 = 18.5
    assert gust_27 == pytest.approx(18.5)
    # Direction at the max-speed hour (21 UTC) = 180
    assert bearing_27 == pytest.approx(180.0)

    # Local day 2026-08-28: UTC hours 22–23 (2 hours in CEST)
    d28 = date(2026, 8, 28)
    assert d28 in result
    speed_28, gust_28, bearing_28 = result[d28]
    # Max speed at hour 23 UTC: 5.0 + 23*0.5 = 16.5
    assert speed_28 == pytest.approx(16.5)
    # Max gust at hour 23 UTC: 8.0 + 23*0.5 = 19.5
    assert gust_28 == pytest.approx(19.5)
    assert bearing_28 == pytest.approx(180.0)


def test_aggregate_daily_wind_winter_utc_local_boundary() -> None:
    """In CET (UTC+1), midnight is at 23:00 UTC the previous day.

    Two hours of speed data: 22:00 UTC (local day D+1: 23:00 CET → wrong day)
    and 23:00 UTC (local day D+1: 00:00 CET next day).
    Verifies the 23:00 UTC hour lands on the *next* local day in winter time.
    """
    # CET = UTC+1; 2026-01-14 23:00 UTC = 2026-01-15 00:00 CET
    text_speed = (
        "point_id;point_type_id;Date;fu3010h0\n"
        "309800;2;202601142200;10.0\n"  # 2026-01-14 23:00 CET → local day 2026-01-14
        "309800;2;202601142300;20.0\n"  # 2026-01-15 00:00 CET → local day 2026-01-15
    )
    texts = {"fu3010h0": text_speed}
    result = aggregate_daily_wind(texts, _koeniz_point())

    # Max speed on 2026-01-14 = 10.0 (only UTC 22:00 is on that local day)
    assert result[date(2026, 1, 14)][0] == pytest.approx(10.0)
    # Max speed on 2026-01-15 = 20.0 (UTC 23:00 = midnight CET)
    assert result[date(2026, 1, 15)][0] == pytest.approx(20.0)


def test_aggregate_daily_wind_no_gust_file() -> None:
    """Missing gust data yields gust=None for that day; speed and bearing unaffected."""
    texts = {k: v for k, v in _wind_texts().items() if k != "fu3010h1"}
    result = aggregate_daily_wind(texts, _koeniz_point())

    d27 = date(2026, 8, 27)
    speed, gust, bearing = result[d27]
    assert speed == pytest.approx(15.5)
    assert gust is None
    assert bearing == pytest.approx(180.0)


def test_aggregate_daily_wind_bearing_at_max_speed_hour_not_gust_hour() -> None:
    """Direction is taken from the hour of max *speed*, not max *gust*.

    Speed peaks at hour 10 (180° bearing); gust peaks at hour 23 (90° bearing).
    """
    text_speed = (
        "point_id;point_type_id;Date;fu3010h0\n"
        "309800;2;202608270000;5.0\n"
        "309800;2;202608271000;15.0\n"  # max speed at hour 10
        "309800;2;202608272300;10.0\n"
    )
    text_gust = (
        "point_id;point_type_id;Date;fu3010h1\n"
        "309800;2;202608270000;6.0\n"
        "309800;2;202608271000;14.0\n"
        "309800;2;202608272300;25.0\n"  # max gust at hour 23
    )
    text_dir = (
        "point_id;point_type_id;Date;dkl010h0\n"
        "309800;2;202608270000;270\n"
        "309800;2;202608271000;180\n"  # bearing at max-speed hour
        "309800;2;202608272300;90\n"
    )
    texts = {"fu3010h0": text_speed, "fu3010h1": text_gust, "dkl010h0": text_dir}
    result = aggregate_daily_wind(texts, _koeniz_point())

    # All three hours are on the same local day (CEST: UTC+2, so 23:00 UTC =
    # 01:00 CEST next day → actually 23:00 UTC falls on 2026-08-28 in CEST)
    # Let's just check the day that has hour 10 UTC:
    d27 = date(2026, 8, 27)
    speed, gust, bearing = result[d27]
    assert speed == pytest.approx(15.0)
    assert gust == pytest.approx(14.0)   # max gust on 2026-08-27 (not hour 23)
    assert bearing == pytest.approx(180.0)  # direction at max-speed hour (hour 10)


def test_aggregate_daily_wind_empty_input() -> None:
    """An empty text dict returns an empty result, not an error."""
    assert aggregate_daily_wind({}, _koeniz_point()) == {}


# --- backend daily wind (point-major fixture) ----------------------------------


async def test_bulk_backend_fetch_daily_with_point_major_wind(session) -> None:
    """When wind files are point-major, daily forecast carries wind fields."""
    with aioresponses() as mock:
        mock.get(ITEMS_URL, status=200,
                 body=_fixture_bytes("ogd-local-forecasting_items.json"))
        for param in DAILY_REQUIRED_PARAMS:
            mock.get(_asset_url(param), status=200,
                     body=_fixture_bytes(f"vnut12.lssw.{RUN_TS}.{param}.csv"))
        for param in DAILY_WIND_PARAMS:
            mock.get(_asset_url(param), status=200,
                     body=_point_major_wind_text(param).encode("iso-8859-1"))
        backend = BulkCsvBackend(session)
        daily = await backend.fetch_daily(_koeniz_point())

    assert len(daily) == 9
    assert daily[0].temp_max == 29.3   # existing field unchanged
    # Wind fields come from the point-major blocks; first local day is 2026-08-27.
    # In CEST the fixture hours 0–21 UTC fall on 2026-08-27: max speed = 15.5 km/h.
    assert daily[0].native_wind_speed == pytest.approx(15.5)
    assert daily[0].native_wind_gust_speed == pytest.approx(18.5)
    assert daily[0].wind_bearing == pytest.approx(180.0)


async def test_bulk_backend_fetch_daily_wind_not_point_major_returns_none(
    session,
) -> None:
    """Non-point-major wind file → guardrail fires, all daily wind fields = None.

    No full download is attempted: the backend returns None and logs a warning.
    The date-major fixture files trigger this path automatically.
    """
    with aioresponses() as mock:
        mock.get(ITEMS_URL, status=200,
                 body=_fixture_bytes("ogd-local-forecasting_items.json"))
        for param in DAILY_REQUIRED_PARAMS:
            mock.get(_asset_url(param), status=200,
                     body=_fixture_bytes(f"vnut12.lssw.{RUN_TS}.{param}.csv"))
        # Date-major fixture → classify_layout returns DATE_MAJOR → guardrail.
        for param in DAILY_WIND_PARAMS:
            mock.get(_asset_url(param), status=200,
                     body=_fixture_bytes(f"vnut12.lssw.{RUN_TS}.{param}.csv"))
        backend = BulkCsvBackend(session)
        daily = await backend.fetch_daily(_koeniz_point())

    # Guardrail fires: wind fields are None for all nine days.
    assert all(d.native_wind_speed is None for d in daily)
    assert all(d.native_wind_gust_speed is None for d in daily)
    assert all(d.wind_bearing is None for d in daily)
    # Temperature still populated.
    assert daily[0].temp_max == 29.3


async def test_get_wind_texts_missing_wind_assets_returns_none(session) -> None:
    """A daily-complete run whose wind files have not published yet degrades to
    None, never a KeyError (issue #60).

    The daily run is chosen on DAILY_REQUIRED_PARAMS alone; during a run's
    publish window the small daily files land before the ~30 MB wind files, so
    ``run.assets`` can lack the wind params. That must not crash the default
    daily refresh — no request is even attempted for the absent files.
    """
    from custom_components.meteoswiss_weather.ogd.stac import Run

    run = Run(
        timestamp=datetime(2026, 8, 27, 3, 0, tzinfo=UTC),
        assets={param: _asset_url(param) for param in DAILY_REQUIRED_PARAMS},
    )
    backend = BulkCsvBackend(session)

    # No aioresponses mock registered: any HTTP attempt would raise, proving the
    # guardrail returns before touching the network.
    assert await backend._get_wind_texts(_koeniz_point(), run) is None
    # Sentinel cached so a repeat call for the same run short-circuits.
    assert backend._wind_run == run.timestamp
    assert backend._wind_texts == {}


async def test_bulk_backend_fetch_daily_wind_fetch_error_degrades_to_none(
    session,
) -> None:
    """A transient connection error on a wind block degrades wind to None and
    still returns the daily forecast (temperature/precipitation/symbol).

    Wind is a best-effort bonus on the default daily refresh; a wind-file hiccup
    must not fail the whole update and lose the small daily files that fetched
    fine (ADR-0002 revision 3 — the same "never crash the default daily refresh"
    contract as the missing-asset and non-point-major guardrails).
    """
    with aioresponses() as mock:
        mock.get(ITEMS_URL, status=200,
                 body=_fixture_bytes("ogd-local-forecasting_items.json"))
        for param in DAILY_REQUIRED_PARAMS:
            mock.get(_asset_url(param), status=200,
                     body=_fixture_bytes(f"vnut12.lssw.{RUN_TS}.{param}.csv"))
        # Every wind block probe returns HTTP 503 → OgdConnectionError.
        for param in DAILY_WIND_PARAMS:
            mock.get(_asset_url(param), status=503, repeat=True)
        backend = BulkCsvBackend(session)
        daily = await backend.fetch_daily(_koeniz_point())

    # Daily forecast is intact; wind fields degraded to None, not an exception.
    assert daily[0].temp_max == 29.3
    assert all(d.native_wind_speed is None for d in daily)
    assert all(d.native_wind_gust_speed is None for d in daily)
    assert all(d.wind_bearing is None for d in daily)
    # Sentinel cached so a repeat call for the same run short-circuits.
    assert backend._wind_texts == {}


async def test_bulk_backend_fetch_hourly_reuses_daily_wind_cache(session) -> None:
    """When fetch_daily() has cached point-major wind for the same run,
    fetch_hourly() does not download the wind files a second time.

    Each wind URL is registered once (aioresponses fails on a second call
    to an unregistered URL); the test proves exactly one fetch per file.
    """
    with aioresponses() as mock:
        # STAC endpoint queried twice (once by fetch_daily, once by fetch_hourly).
        mock.get(ITEMS_URL, status=200,
                 body=_fixture_bytes("ogd-local-forecasting_items.json"))
        mock.get(ITEMS_URL, status=200,
                 body=_fixture_bytes("ogd-local-forecasting_items.json"))

        for param in DAILY_REQUIRED_PARAMS:
            mock.get(_asset_url(param), status=200,
                     body=_fixture_bytes(f"vnut12.lssw.{RUN_TS}.{param}.csv"))

        # Wind files registered *once*: if fetch_hourly re-fetches them the test fails.
        for param in DAILY_WIND_PARAMS:
            mock.get(_asset_url(param), status=200,
                     body=_point_major_wind_text(param).encode("iso-8859-1"))

        # Non-wind hourly params registered once for the hourly fetch.
        non_wind = [p for p in HOURLY_REQUIRED_PARAMS if p not in DAILY_WIND_PARAMS]
        for param in non_wind:
            mock.get(_hourly_asset_url(param), status=200,
                     body=_fixture_bytes(f"vnut12.lssw.{RUN_TS}.{param}.csv"))

        backend = BulkCsvBackend(session)
        point = _koeniz_point()
        daily = await backend.fetch_daily(point)
        hourly = await backend.fetch_hourly(point)

    # Daily wind populated from the block fetch.
    assert daily[0].native_wind_speed == pytest.approx(15.5)
    # Hourly wind comes from the reused cache; full 24 hours returned.
    assert len(hourly) == 24
    assert hourly[0].wind_speed_kmh == pytest.approx(5.0)
