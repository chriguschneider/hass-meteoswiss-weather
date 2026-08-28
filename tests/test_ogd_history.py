"""Tests for the station hourly history client (ADR-0007, issue #51).

Pure and HA-free (ADR-0001): the STAC item and history CSV files are replayed
from ``tests/fixtures/`` via ``aioresponses``; the network is never hit.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import aiohttp.resolver
import pytest
from aioresponses import aioresponses

from custom_components.meteoswiss_weather.ogd import (
    HourlyHistoryRow,
    fetch_station_history,
    select_history_files,
)
from custom_components.meteoswiss_weather.ogd.const import station_stac_item_url
from custom_components.meteoswiss_weather.ogd.history import _parse_body
from custom_components.meteoswiss_weather.ogd.models import OgdParseError

FIXTURES = Path(__file__).parent / "fixtures"

BER_STAC_URL = station_stac_item_url("ber")
BER_RECENT_URL = (
    "https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/ber/ogd-smn_ber_h_recent.csv"
)
BER_HIST_2020_URL = (
    "https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/ber/"
    "ogd-smn_ber_h_historical_2020-2029.csv"
)
BER_HIST_2010_URL = (
    "https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/ber/"
    "ogd-smn_ber_h_historical_2010-2019.csv"
)


@pytest.fixture
async def session():
    """A ClientSession with a thread-free resolver (aioresponses-compatible)."""
    connector = aiohttp.TCPConnector(resolver=aiohttp.resolver.ThreadedResolver())
    async with aiohttp.ClientSession(connector=connector) as client:
        yield client


# ---------------------------------------------------------------------------
# select_history_files: range → file selection
# ---------------------------------------------------------------------------

_ASSETS = {
    "ogd-smn_ber_h_recent.csv": BER_RECENT_URL,
    "ogd-smn_ber_h_historical_2020-2029.csv": BER_HIST_2020_URL,
    "ogd-smn_ber_h_historical_2010-2019.csv": BER_HIST_2010_URL,
    "ogd-smn_ber_h_historical_2000-2009.csv": (
        "https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/ber/"
        "ogd-smn_ber_h_historical_2000-2009.csv"
    ),
}

# All tests use _current_year=2026 so they are independent of when CI runs.
_YEAR = 2026


def test_select_recent_only():
    """Range entirely within the current year → only the recent file."""
    urls = select_history_files(
        _ASSETS,
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 8, 27, tzinfo=UTC),
        _current_year=_YEAR,
    )
    assert urls == [BER_RECENT_URL]


def test_select_recent_plus_one_decade():
    """Range spanning 2025-2026 → 2020-2029 decade + recent, in order."""
    urls = select_history_files(
        _ASSETS,
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2026, 8, 27, tzinfo=UTC),
        _current_year=_YEAR,
    )
    assert BER_HIST_2020_URL in urls
    assert BER_RECENT_URL in urls
    assert BER_HIST_2010_URL not in urls
    # Chronological: decade before recent
    assert urls.index(BER_HIST_2020_URL) < urls.index(BER_RECENT_URL)


def test_select_two_decades():
    """Range 2015-2024 spans two historical decades; recent not included."""
    urls = select_history_files(
        _ASSETS,
        datetime(2015, 1, 1, tzinfo=UTC),
        datetime(2024, 12, 31, tzinfo=UTC),
        _current_year=_YEAR,
    )
    assert BER_HIST_2010_URL in urls
    assert BER_HIST_2020_URL in urls
    assert BER_RECENT_URL not in urls
    # 2000-2009 ends at 2009 < 2015 start → not included
    assert (
        "https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/ber/"
        "ogd-smn_ber_h_historical_2000-2009.csv"
    ) not in urls
    # Chronological order
    assert urls.index(BER_HIST_2010_URL) < urls.index(BER_HIST_2020_URL)


def test_select_range_before_any_asset():
    """A range entirely before the oldest asset → empty list."""
    assets = {"ogd-smn_ber_h_recent.csv": BER_RECENT_URL}
    urls = select_history_files(
        assets,
        datetime(1990, 1, 1, tzinfo=UTC),
        datetime(1990, 12, 31, tzinfo=UTC),
        _current_year=_YEAR,
    )
    assert urls == []


def test_select_current_decade_not_included_for_current_year_only_range():
    """The 2020-2029 decade ends effectively at 2025; a 2026-only range skips it."""
    assets = {
        "ogd-smn_ber_h_recent.csv": BER_RECENT_URL,
        "ogd-smn_ber_h_historical_2020-2029.csv": BER_HIST_2020_URL,
    }
    urls = select_history_files(
        assets,
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 8, 27, tzinfo=UTC),
        _current_year=_YEAR,
    )
    assert BER_HIST_2020_URL not in urls
    assert BER_RECENT_URL in urls


# ---------------------------------------------------------------------------
# _parse_body: streaming CSV parse
# ---------------------------------------------------------------------------

_RECENT_BYTES = (FIXTURES / "ogd-smn_ber_h_recent.csv").read_bytes()
_HIST_BYTES = (FIXTURES / "ogd-smn_ber_h_historical_2020-2029.csv").read_bytes()


def _decode(raw: bytes) -> str:
    return raw.decode("cp1252")


def test_parse_recent_row_count_and_timestamps():
    """Four rows in the fixture; all within a wide window."""
    rows = _parse_body(
        _decode(_RECENT_BYTES),
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 12, 31, tzinfo=UTC),
    )
    assert len(rows) == 4
    assert rows[0].ts_utc == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert rows[-1].ts_utc == datetime(2026, 8, 27, 23, 0, tzinfo=UTC)


def test_parse_filters_to_range():
    """Rows outside [start, end] are dropped."""
    rows = _parse_body(
        _decode(_RECENT_BYTES),
        datetime(2026, 8, 27, 22, 0, tzinfo=UTC),
        datetime(2026, 8, 27, 22, 0, tzinfo=UTC),
    )
    assert len(rows) == 1
    assert rows[0].ts_utc == datetime(2026, 8, 27, 22, 0, tzinfo=UTC)


def test_parse_temperature_fields():
    """First recent row has expected temperature mean/min/max."""
    rows = _parse_body(
        _decode(_RECENT_BYTES),
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.temp_mean == pytest.approx(2.3)
    assert row.temp_min == pytest.approx(1.8)
    assert row.temp_max == pytest.approx(2.5)
    assert row.humidity == pytest.approx(80.5)
    assert row.dew_point == pytest.approx(-0.5)
    assert row.pressure_qff == pytest.approx(1016.5)
    assert row.wind_speed_kmh == pytest.approx(1.2)
    assert row.gust_kmh == pytest.approx(2.4)
    assert row.precipitation_sum == pytest.approx(0.1)
    assert row.global_radiation == pytest.approx(12.0)


def test_parse_none_for_empty_field():
    """The 23:00 row has an empty precipitation_sum cell → None."""
    rows = _parse_body(
        _decode(_RECENT_BYTES),
        datetime(2026, 8, 27, 23, 0, tzinfo=UTC),
        datetime(2026, 8, 27, 23, 0, tzinfo=UTC),
    )
    assert len(rows) == 1
    assert rows[0].precipitation_sum is None


def test_parse_historical_first_and_last():
    """Historical fixture: first row 2020-01-01 00:00, last 2025-12-31 23:00."""
    rows = _parse_body(
        _decode(_HIST_BYTES),
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2025, 12, 31, 23, 0, tzinfo=UTC),
    )
    assert len(rows) == 4
    assert rows[0].ts_utc == datetime(2020, 1, 1, 0, 0, tzinfo=UTC)
    assert rows[-1].ts_utc == datetime(2025, 12, 31, 23, 0, tzinfo=UTC)


def test_parse_cp1252_header_decoded():
    """cp1252 body with the real header is parsed without error."""
    rows = _parse_body(
        _decode(_RECENT_BYTES),
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 12, 31, tzinfo=UTC),
    )
    # If encoding were wrong the DictReader fieldnames would be garbled and
    # the timestamp column would not be found (raises OgdParseError) or rows
    # would be empty. Four rows means cp1252 decoding worked.
    assert len(rows) == 4


def test_parse_missing_header_raises():
    with pytest.raises(OgdParseError, match="missing its header"):
        _parse_body("not;a;valid;header\n1;2;3;4\n",
                    datetime(2026, 1, 1, tzinfo=UTC),
                    datetime(2026, 12, 31, tzinfo=UTC))


# ---------------------------------------------------------------------------
# fetch_station_history: end-to-end with mocked HTTP
# ---------------------------------------------------------------------------

async def test_fetch_station_history_recent_only(session, freezer) -> None:
    """Range in 2026 → STAC item + recent file fetched; rows returned."""
    # Pin the clock: file selection depends on the current year (see the
    # _current_year kwarg used by the select_* tests), and fetch_station_history
    # reads it from datetime.now(); freeze so this stays deterministic past 2026.
    freezer.move_to("2026-08-28")
    stac_body = (FIXTURES / "ogd-smn_ber_stac_item.json").read_bytes()
    with aioresponses() as mock:
        mock.get(BER_STAC_URL, status=200, body=stac_body)
        mock.get(BER_RECENT_URL, status=200, body=_RECENT_BYTES,
                 headers={"Content-Type": "text/csv"})
        rows = await fetch_station_history(
            session,
            "ber",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 12, 31, tzinfo=UTC),
        )

    assert len(rows) == 4
    assert all(isinstance(r, HourlyHistoryRow) for r in rows)
    assert rows[0].ts_utc == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


async def test_fetch_station_history_recent_plus_decade(session, freezer) -> None:
    """Range spanning 2025-2026 → STAC + decade + recent fetched; rows combined."""
    freezer.move_to("2026-08-28")  # keep file selection deterministic past 2026
    stac_body = (FIXTURES / "ogd-smn_ber_stac_item.json").read_bytes()
    with aioresponses() as mock:
        mock.get(BER_STAC_URL, status=200, body=stac_body)
        mock.get(BER_HIST_2020_URL, status=200, body=_HIST_BYTES,
                 headers={"Content-Type": "text/csv"})
        mock.get(BER_RECENT_URL, status=200, body=_RECENT_BYTES,
                 headers={"Content-Type": "text/csv"})
        rows = await fetch_station_history(
            session,
            "ber",
            datetime(2025, 1, 1, tzinfo=UTC),
            datetime(2026, 12, 31, tzinfo=UTC),
        )

    # 2 rows from historical (31.12.2025) + 4 rows from recent (all 2026)
    assert len(rows) == 6
    # Chronological: historical rows come first (files fetched oldest-first)
    assert rows[0].ts_utc < rows[-1].ts_utc


async def test_fetch_station_history_no_matching_files(session) -> None:
    """Range before any available asset → empty list, only STAC fetched."""
    stac_body = json.dumps({
        "type": "Feature",
        "id": "ber",
        "assets": {
            "ogd-smn_ber_h_recent.csv": {"href": BER_RECENT_URL},
        },
    }).encode()
    with aioresponses() as mock:
        mock.get(BER_STAC_URL, status=200, body=stac_body)
        rows = await fetch_station_history(
            session,
            "ber",
            datetime(1985, 1, 1, tzinfo=UTC),
            datetime(1985, 12, 31, tzinfo=UTC),
        )
    assert rows == []
