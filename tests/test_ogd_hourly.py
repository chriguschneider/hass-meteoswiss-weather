"""Tests for the per-file HTTP-Range hourly strategies (issue #50).

Pure and HA-free (ADR-0001): the strategies run against an in-memory
:class:`_MemReader` with real byte-range semantics, so binary search, layout
detection and the horizon prefix are exercised without any network. A couple of
end-to-end tests drive :func:`fetch_hourly_file` with the reader monkeypatched
in, covering the fallback and the aiohttp seam.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.meteoswiss_weather.ogd import (
    FileLayout,
    ForecastPoint,
    classify_layout,
    horizon_end_utc,
    parse_hourly,
)
from custom_components.meteoswiss_weather.ogd import hourly as H
from custom_components.meteoswiss_weather.ogd.const import HOURLY_HORIZON_FULL_RUN

# A small but varied point set; ids are distinct across types so an id-sorted
# file still isolates one point's block. Sorting by id mixes the types
# (1,1,1,3,3,2,2,2), which is exactly the jww003i0 layout.
_POINTS = [
    (1, 1),
    (2, 1),
    (3, 1),
    (5000, 3),
    (6000, 3),
    (309800, 2),
    (309801, 2),
    (800100, 2),
]
_TARGET = ForecastPoint(309800, 2, "3098", "Köniz", 46.9, 7.4, 595.0)
_HEADER = "point_id;point_type_id;Date;tre200h0"
_H0 = datetime(2026, 8, 26, 21, 0, tzinfo=UTC)  # files start at 21:00 UTC
_HOURS = 120  # five days of hourly steps


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M")


def _rows() -> list[tuple[int, int, datetime, float]]:
    out = []
    for h in range(_HOURS):
        when = _H0 + timedelta(hours=h)
        for pid, ptype in _POINTS:
            out.append((pid, ptype, when, float(h)))
    return out


def _render(rows: list[tuple[int, int, datetime, float]]) -> bytes:
    lines = [_HEADER]
    lines += [f"{pid};{ptype};{_stamp(when)};{val}" for pid, ptype, when, val in rows]
    return ("\n".join(lines) + "\n").encode("iso-8859-1")


def _date_major() -> bytes:
    return _render(sorted(_rows(), key=lambda r: (r[2], r[1], r[0])))


def _point_major_type() -> bytes:
    return _render(sorted(_rows(), key=lambda r: (r[1], r[0], r[2])))


def _point_major_id() -> bytes:
    return _render(sorted(_rows(), key=lambda r: (r[0], r[2])))


def _shuffled() -> bytes:
    rng = random.Random(7)
    rows = _rows()
    rng.shuffle(rows)
    return _render(rows)


class _MemReader:
    """A :class:`RangeReader` over an in-memory buffer with real slicing."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.reads = 0

    async def size(self) -> int:
        return len(self._data)

    async def read(self, start: int, length: int) -> bytes:
        self.reads += 1
        return self._data[start : start + length]

    async def read_all(self) -> bytes:
        self.reads += 1
        return self._data


# --- layout classification --------------------------------------------------


async def test_classify_date_major() -> None:
    assert await classify_layout(_MemReader(_date_major())) is FileLayout.DATE_MAJOR


async def test_classify_point_major_type() -> None:
    layout = await classify_layout(_MemReader(_point_major_type()))
    assert layout is FileLayout.POINT_MAJOR_TYPE


async def test_classify_point_major_id() -> None:
    layout = await classify_layout(_MemReader(_point_major_id()))
    assert layout is FileLayout.POINT_MAJOR_ID


async def test_classify_shuffled_is_fallback() -> None:
    assert await classify_layout(_MemReader(_shuffled())) is FileLayout.FALLBACK


# --- point-major block fetch ------------------------------------------------


def _block_rows(text: str) -> list[list[str]]:
    lines = [ln for ln in text.splitlines() if ln]
    assert lines[0] == _HEADER
    return [ln.split(";") for ln in lines[1:]]


@pytest.mark.parametrize("layout_fn", [_point_major_type, _point_major_id])
async def test_fetch_point_major_returns_only_the_point(layout_fn) -> None:
    reader = _MemReader(layout_fn())
    layout = await classify_layout(reader)
    text, block_start = await H._fetch_point_major(reader, layout, _TARGET, None)

    rows = _block_rows(text)
    # Exactly the target point's rows, all hours, nothing else.
    assert len(rows) == _HOURS
    assert all(r[0] == "309800" and r[1] == "2" for r in rows)
    assert block_start is not None
    # A tiny fraction of the file was read (binary search + one block), not all.
    assert reader.reads < 60


async def test_fetch_point_major_binary_search_matches_linear() -> None:
    """The block found by binary search equals a brute-force filter."""
    data = _point_major_type()
    reader = _MemReader(data)
    layout = await classify_layout(reader)
    text, _ = await H._fetch_point_major(reader, layout, _TARGET, None)

    expected = [
        ln
        for ln in data.decode("iso-8859-1").splitlines()
        if ln.startswith("309800;2;")
    ]
    assert [ln for ln in text.splitlines() if ln.startswith("309800;2;")] == expected


async def test_fetch_point_major_absent_point_returns_header_only() -> None:
    reader = _MemReader(_point_major_type())
    layout = await classify_layout(reader)
    missing = ForecastPoint(999999, 2, "9999", "Nowhere", 47.0, 8.0, None)
    text, block_start = await H._fetch_point_major(reader, layout, missing, None)
    assert _block_rows(text) == []
    assert block_start is None


async def test_cached_offset_hit_and_miss() -> None:
    data = _point_major_type()
    layout = FileLayout.POINT_MAJOR_TYPE

    # First fetch discovers and returns the block start.
    r1 = _MemReader(data)
    text1, start = await H._fetch_point_major(r1, layout, _TARGET, None)
    assert start is not None

    # Cached-offset HIT: reusing the correct start still returns the same block
    # and skips the binary search (fewer reads than a cold search).
    r2 = _MemReader(data)
    text2, start2 = await H._fetch_point_major(r2, layout, _TARGET, start)
    assert text2 == text1
    assert start2 == start
    assert r2.reads < r1.reads

    # Cached-offset MISS: a stale offset is rejected and a fresh search recovers.
    r3 = _MemReader(data)
    text3, start3 = await H._fetch_point_major(r3, layout, _TARGET, start + 3)
    assert text3 == text1
    assert start3 == start


# --- date-major horizon prefix ----------------------------------------------


async def test_date_major_full_run_returns_everything() -> None:
    reader = _MemReader(_date_major())
    text = await H._fetch_date_major(reader, None)
    hourly = parse_hourly({"tre200h0": text}, _TARGET, None)
    assert len(hourly) == _HOURS


async def test_date_major_horizon_prefix_trims_to_cutoff() -> None:
    reader = _MemReader(_date_major())
    horizon = _H0 + timedelta(hours=30)  # keep the first 30 hour blocks
    text = await H._fetch_date_major(reader, horizon)
    hourly = parse_hourly({"tre200h0": text}, _TARGET, horizon)
    assert [h.time for h in hourly] == [
        _H0 + timedelta(hours=i) for i in range(30)
    ]


async def test_date_major_extends_prefix_when_budget_too_small(monkeypatch) -> None:
    """A too-small initial budget is doubled until the horizon is covered."""
    # Force the first prefix to be far shorter than the wanted horizon.
    monkeypatch.setattr(H, "HOURLY_BYTES_PER_HOUR", 8)
    monkeypatch.setattr(H, "HOURLY_RANGE_SAFETY", 1.0)
    monkeypatch.setattr(H, "_HORIZON_MARGIN_HOURS", 0)

    reader = _MemReader(_date_major())
    horizon = _H0 + timedelta(hours=100)
    text = await H._fetch_date_major(reader, horizon)
    hourly = parse_hourly({"tre200h0": text}, _TARGET, horizon)
    # All 100 wanted hours survived despite the tiny starting budget.
    assert len(hourly) == 100
    assert reader.reads > 1  # it took more than one prefix read


async def test_date_major_truncated_last_row_is_skipped() -> None:
    """A Range that ends mid-row parses cleanly (the partial row is dropped)."""
    data = _date_major()
    truncated = data[: len(data) - 12]  # chop the final row mid-way
    reader = _MemReader(truncated)
    text = await H._fetch_date_major(reader, None)
    # Parsing must not raise and yields only whole rows.
    hourly = parse_hourly({"tre200h0": text}, _TARGET, None)
    assert hourly  # got some hours
    assert all(h.temperature is not None for h in hourly)


# --- horizon computation ----------------------------------------------------


def test_horizon_full_run_is_none() -> None:
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    assert horizon_end_utc(HOURLY_HORIZON_FULL_RUN, now) is None
    assert horizon_end_utc(None, now) is None


def test_horizon_summer_cest() -> None:
    # 12:00 UTC = 14:00 CEST on 1 Jul; day 0 = local midnight tomorrow.
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    assert horizon_end_utc(0, now) == datetime(2026, 7, 1, 22, 0, tzinfo=UTC)
    # Default 2 days = local midnight after today + 2 days.
    assert horizon_end_utc(2, now) == datetime(2026, 7, 3, 22, 0, tzinfo=UTC)


def test_horizon_winter_cet() -> None:
    now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)  # 13:00 CET
    assert horizon_end_utc(2, now) == datetime(2026, 1, 17, 23, 0, tzinfo=UTC)


def test_horizon_late_local_evening_uses_local_today() -> None:
    # 22:30 UTC on 1 Jul is already 00:30 local on 2 Jul: "today" is 2 Jul.
    now = datetime(2026, 7, 1, 22, 30, tzinfo=UTC)
    assert horizon_end_utc(0, now) == datetime(2026, 7, 2, 22, 0, tzinfo=UTC)


# --- end-to-end via fetch_hourly_file ---------------------------------------


def _patch_reader(monkeypatch, data: bytes) -> None:
    """Make AiohttpRangeReader(...) ignore its args and serve ``data``."""

    def _factory(_session, _url):
        return _MemReader(data)

    monkeypatch.setattr(H, "AiohttpRangeReader", _factory)


async def test_fetch_hourly_file_point_major(monkeypatch) -> None:
    _patch_reader(monkeypatch, _point_major_type())
    result = await H.fetch_hourly_file(
        None, "http://x", _TARGET, horizon_end=None
    )
    assert result.layout is FileLayout.POINT_MAJOR_TYPE
    assert result.block_start is not None
    assert len(_block_rows(result.text)) == _HOURS


async def test_fetch_hourly_file_fallback_downloads_full(monkeypatch, caplog) -> None:
    _patch_reader(monkeypatch, _shuffled())
    result = await H.fetch_hourly_file(
        None, "http://x", _TARGET, horizon_end=None
    )
    assert result.layout is FileLayout.FALLBACK
    # The whole file came back; parsing still finds the point's rows.
    hourly = parse_hourly({"tre200h0": result.text}, _TARGET, None)
    assert len(hourly) == _HOURS


# --- horizon_start lower bound (issue #92) ----------------------------------


def _make_csv(header: str, rows: list[tuple]) -> str:
    """Build a minimal CSV string from a header and value tuples."""
    lines = [header]
    for row in rows:
        lines.append(";".join(str(v) for v in row))
    return "\n".join(lines) + "\n"


_CSV_HEADER = "point_id;point_type_id;Date;tre200h0"
_PID, _PTYPE = _TARGET.point_id, _TARGET.point_type_id


def _row(dt: datetime, val: float) -> tuple:
    return (_PID, _PTYPE, dt.strftime("%Y%m%d%H%M"), val)


def test_horizon_start_drops_past_hours() -> None:
    """Hours strictly before horizon_start are dropped; the running hour is kept."""
    h0 = datetime(2026, 8, 28, 19, 0, tzinfo=UTC)  # the "current" hour
    rows = [_row(h0 - timedelta(hours=2), 1.0),  # 17:00 — 2 h ago
            _row(h0 - timedelta(hours=1), 2.0),  # 18:00 — 1 h ago
            _row(h0, 3.0),                        # 19:00 — running hour
            _row(h0 + timedelta(hours=1), 4.0)]   # 20:00 — future
    text = _make_csv(_CSV_HEADER, rows)
    hourly = parse_hourly({"tre200h0": text}, _TARGET, horizon_start=h0)
    assert [h.time for h in hourly] == [h0, h0 + timedelta(hours=1)]


def test_horizon_start_keeps_running_hour() -> None:
    """The hour that equals horizon_start (the running hour) is kept."""
    h0 = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    text = _make_csv(_CSV_HEADER, [_row(h0, 22.2)])
    hourly = parse_hourly({"tre200h0": text}, _TARGET, horizon_start=h0)
    assert len(hourly) == 1
    assert hourly[0].time == h0


def test_full_run_still_trims_past() -> None:
    """horizon_start applies even when horizon_end is None (full-run mode)."""
    h0 = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    rows = [_row(h0 - timedelta(hours=23), 1.0),
            _row(h0, 2.0),
            _row(h0 + timedelta(hours=1), 3.0)]
    text = _make_csv(_CSV_HEADER, rows)
    # horizon_end=None is the full-run sentinel; the lower bound still fires.
    hourly = parse_hourly({"tre200h0": text}, _TARGET, horizon_end=None,
                          horizon_start=h0)
    assert [h.time for h in hourly] == [h0, h0 + timedelta(hours=1)]


def test_ragged_head_within_single_tier() -> None:
    """A parameter file that starts earlier than the others is trimmed.

    Simulates two point-major parameters: one starts at h0-1, the other at h0.
    With horizon_start=h0, only h0 onward survives from both files, so the
    first merged hour has both fields present rather than one ragged field.
    """
    from custom_components.meteoswiss_weather.ogd.const import (
        HOURLY_WIND_DIRECTION,
        HOURLY_WIND_SPEED,
    )

    h0 = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    # wind_direction file starts one hour early (ragged head).
    dir_csv = _make_csv(
        f"point_id;point_type_id;Date;{HOURLY_WIND_DIRECTION}",
        [_row(h0 - timedelta(hours=1), 219.0),
         _row(h0, 210.0)]
    )
    # wind_speed file starts at h0.
    spd_csv = _make_csv(
        f"point_id;point_type_id;Date;{HOURLY_WIND_SPEED}",
        [_row(h0, 12.0)]
    )
    hourly = parse_hourly(
        {HOURLY_WIND_DIRECTION: dir_csv, HOURLY_WIND_SPEED: spd_csv},
        _TARGET,
        horizon_start=h0,
    )
    assert len(hourly) == 1
    assert hourly[0].time == h0
    assert hourly[0].wind_bearing == 210.0
    assert hourly[0].wind_speed_kmh == 12.0
