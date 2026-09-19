"""Tests for the per-file HTTP-Range hourly strategies (issue #50).

Pure and HA-free (ADR-0001): the strategies run against an in-memory
:class:`_MemReader` with real byte-range semantics, so binary search, layout
detection and the horizon prefix are exercised without any network. The
escalation ladder (:func:`fetch_series`) is driven through the same reader.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta

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


# --- the escalation ladder (ADR-0008 section 4) -----------------------------

_BIG_HOURS = 60
_BIG_POINTS = [(pid, 1) for pid in range(1, 301)] + [
    (pid, 2) for pid in range(309700, 310000)
]


def _big_date_major(*, drop: tuple[int, int, int] | None = None) -> bytes:
    """A date-major file big enough that a 2 KB window needs a real prediction.

    600 points per hour block (~13 KB) over 60 hours, with values of varying
    width so the blocks differ in size and a position extrapolated from the file
    start drifts — the property measured on the live ``tre200h0``. Within a
    block the points keep one fixed (unsorted) order, like upstream. ``drop``
    removes one ``(point_id, point_type_id, hour)`` row.
    """
    order = _BIG_POINTS[::2] + _BIG_POINTS[1::2]  # fixed, but not sorted
    lines = [_HEADER]
    for h in range(_BIG_HOURS):
        when = _H0 + timedelta(hours=h)
        for pid, ptype in order:
            if drop == (pid, ptype, h):
                continue
            value = ((pid * 7 + h * 131) % 20000) / 10  # "0.7" … "1999.9"
            lines.append(f"{pid};{ptype};{_stamp(when)};{value}")
    return ("\n".join(lines) + "\n").encode("iso-8859-1")


def _counting(data: bytes, cap: int | None = 96) -> H._CountingReader:
    return H._CountingReader(_MemReader(data), cap)


async def _series(
    data: bytes, *, start=None, end=None, cap=96, hint=None, utc_day=None,
    step=timedelta(hours=1),
):
    return await H._fetch_series(
        _counting(data, cap),
        _TARGET,
        window_start=start,
        window_end=end,
        hint=hint,
        utc_day=utc_day,
        step=step,
    )


def _series_lines(result) -> list[str]:
    return [line for line in result.text.split("\n")[1:] if line]


def _series_hours(result) -> list[str]:
    return [line.split(";")[2] for line in _series_lines(result)]


async def test_ladder_row_addresses_a_date_major_window() -> None:
    """The window is served row by row: every hour, only the point, few bytes."""
    data = _big_date_major()
    start, end = _H0 + timedelta(hours=10), _H0 + timedelta(hours=40)
    result = await _series(data, start=start, end=end)

    assert result.layout is FileLayout.DATE_MAJOR
    assert result.level == 1
    assert not result.whole_run
    assert _series_hours(result) == [
        _stamp(start + timedelta(hours=h)) for h in range(30)
    ]
    assert all(
        line.startswith(f"{_TARGET.point_id};{_TARGET.point_type_id};")
        for line in _series_lines(result)
    )
    assert result.requests <= 96
    assert result.bytes < len(data) / 4
    # The parser reads the text like any other file.
    assert len(parse_hourly({"tre200h0": result.text}, _TARGET, None)) == 30


async def test_ladder_geometry_hint_saves_the_learning_scan() -> None:
    data = _big_date_major()
    start, end = _H0 + timedelta(hours=10), _H0 + timedelta(hours=20)
    cold = await _series(data, start=start, end=end)
    warm = await _series(data, start=start, end=end, hint=cold.hint)

    assert cold.hint is not None and cold.hint.geometry is not None
    assert _series_hours(warm) == _series_hours(cold)
    assert warm.level == 0
    assert warm.requests < cold.requests


async def test_ladder_wrong_geometry_hint_is_detected_not_trusted() -> None:
    """A stale hint costs wider reads, never a wrong or missing row."""
    data = _big_date_major()
    start, end = _H0 + timedelta(hours=5), _H0 + timedelta(hours=15)
    good = await _series(data, start=start, end=end)
    bad_hint = H.FileHint(
        layout=FileLayout.DATE_MAJOR,
        header=good.hint.header,
        first_stamp=good.hint.first_stamp,
        last_stamp=good.hint.last_stamp,
        geometry=H.RowGeometry(
            block_bytes=good.hint.geometry.block_bytes * 0.8,
            row_offset=17,
            anchor_stamp=_stamp(start),
            anchor_offset=123,
        ),
    )
    result = await _series(data, start=start, end=end, hint=bad_hint)

    assert _series_lines(result) == _series_lines(good)
    assert result.level <= 2  # a fresh look, not a prefix or the whole file


async def test_ladder_climbs_to_the_full_file_when_a_row_is_missing() -> None:
    """Addressing cannot prove hour 20, the prefix cannot either, so the whole
    file is read and what upstream has is returned (quality first)."""
    data = _big_date_major(drop=(_TARGET.point_id, _TARGET.point_type_id, 20))
    start, end = _H0 + timedelta(hours=10), _H0 + timedelta(hours=30)
    result = await _series(data, start=start, end=end)

    assert result.level == 4
    assert result.whole_run
    hours = _series_hours(result)
    assert len(hours) == _BIG_HOURS - 1
    assert _stamp(_H0 + timedelta(hours=20)) not in hours


async def test_ladder_skips_addressing_beyond_the_request_cap() -> None:
    """A window that needs more requests than the cap goes straight to the
    prefix, so saving bytes never becomes a request storm."""
    data = _big_date_major()
    start, end = _H0 + timedelta(hours=2), _H0 + timedelta(hours=32)
    result = await _series(data, start=start, end=end, cap=20)

    assert result.level == 3
    assert not result.whole_run
    hours = _series_hours(result)
    assert _stamp(start) in hours and _stamp(end - timedelta(hours=1)) in hours


async def test_ladder_whole_run_within_the_cap_is_row_addressed() -> None:
    """A whole-run demand on a date-major file with few blocks is served by row
    addressing, not the full file (issue #122): decide by the block count."""
    result = await _series(_big_date_major())  # 60 blocks, cap 60+overhead < 96
    assert result.layout is FileLayout.DATE_MAJOR
    assert result.level <= 2
    assert result.whole_run
    assert _series_hours(result) == [
        _stamp(_H0 + timedelta(hours=h)) for h in range(_BIG_HOURS)
    ]
    assert all(
        line.startswith(f"{_TARGET.point_id};{_TARGET.point_type_id};")
        for line in _series_lines(result)
    )
    assert result.bytes < len(_big_date_major()) / 4


async def test_ladder_whole_run_beyond_the_cap_is_the_full_file() -> None:
    """When the block count would overrun the request cap, a whole-run demand
    climbs to the full file rather than storming the origin (ADR-0008)."""
    result = await _series(_big_date_major(), cap=20)  # 60 blocks > cap
    assert result.level == 4
    assert result.whole_run
    assert len(_series_hours(result)) == _BIG_HOURS


async def test_ladder_window_outside_the_file_is_empty_without_escalating() -> None:
    data = _big_date_major()
    start = _H0 + timedelta(days=30)
    result = await _series(data, start=start, end=start + timedelta(hours=48))

    assert not result.has_rows
    assert result.level < 3
    assert result.bytes < len(data) / 4


async def test_ladder_point_major_block_is_whole_run_and_hint_is_level_zero() -> None:
    data = _point_major_type()
    start, end = _H0 + timedelta(hours=3), _H0 + timedelta(hours=9)
    cold = await _series(data, start=start, end=end)
    warm = await _series(data, start=start, end=end, hint=cold.hint)

    assert cold.level == 1 and warm.level == 0
    assert cold.whole_run and warm.whole_run
    assert len(_series_hours(cold)) == _HOURS
    assert warm.requests < cold.requests


async def test_ladder_unrecognised_layout_reads_the_full_file() -> None:
    result = await _series(_shuffled())
    assert result.layout is FileLayout.FALLBACK
    assert result.level == 4
    assert len(_series_hours(result)) == _HOURS


async def test_ladder_absent_point_is_proven_by_the_full_file() -> None:
    rows = [r for r in _rows() if (r[0], r[1]) != (_TARGET.point_id, 2)]
    data = _render(sorted(rows, key=lambda r: (r[2], r[1], r[0])))
    result = await _series(
        data, start=_H0 + timedelta(hours=1), end=_H0 + timedelta(hours=5)
    )
    assert result.level == 4
    assert not result.has_rows


# --- the per-file hint, remembered per UTC day (issue #121) ------------------

# The file's UTC day: _big_date_major starts at 21:00 the previous day.
_DAY = date(2026, 8, 27)


async def test_warm_date_major_window_is_about_one_request_per_hour() -> None:
    """A same-day hint skips classification and the probes: a warm 30 h window
    stays within ``hours + 4`` requests (ADR-0008 acceptance, issue #121)."""
    data = _big_date_major()
    hours = 30
    start, end = _H0 + timedelta(hours=10), _H0 + timedelta(hours=10 + hours)
    cold = await _series(data, start=start, end=end, utc_day=_DAY)
    warm = await _series(data, start=start, end=end, hint=cold.hint, utc_day=_DAY)

    assert _series_hours(warm) == _series_hours(cold)
    assert warm.level == 0
    assert warm.requests <= hours + 4


async def test_warm_point_major_fetch_is_at_most_three_requests() -> None:
    """A same-day point-major hint verifies the block with a couple of probes."""
    data = _point_major_type()
    start, end = _H0 + timedelta(hours=3), _H0 + timedelta(hours=9)
    cold = await _series(data, start=start, end=end, utc_day=_DAY)
    warm = await _series(data, start=start, end=end, hint=cold.hint, utc_day=_DAY)

    assert warm.level == 0
    assert _series_hours(warm) == _series_hours(cold)
    assert warm.requests <= 3


async def test_hint_from_another_utc_day_costs_a_fresh_look() -> None:
    """Byte offsets are only stable within a UTC day: a hint from another day is
    dropped and the file is classified afresh, never read as a prefix or whole."""
    data = _big_date_major()
    start, end = _H0 + timedelta(hours=10), _H0 + timedelta(hours=25)
    good = await _series(data, start=start, end=end, utc_day=_DAY)
    stale = await _series(
        data, start=start, end=end, hint=good.hint, utc_day=date(2026, 8, 28)
    )

    assert _series_lines(stale) == _series_lines(good)
    assert stale.level <= 1  # a fresh classification, not a prefix or the file
    assert stale.bytes < len(data) / 4


async def test_hint_for_another_layout_costs_a_fresh_look() -> None:
    """A hint that names the wrong layout for the file is detected by the rows it
    fails to yield and retried with a fresh classification (never wrong data)."""
    data = _big_date_major()  # date-major
    start, end = _H0 + timedelta(hours=10), _H0 + timedelta(hours=25)
    good = await _series(data, start=start, end=end, utc_day=_DAY)
    wrong = H.FileHint(
        layout=FileLayout.POINT_MAJOR_TYPE,
        utc_day=_DAY,
        header=good.hint.header,
        block_start=123,
    )
    result = await _series(data, start=start, end=end, hint=wrong, utc_day=_DAY)

    assert _series_lines(result) == _series_lines(good)
    assert result.level <= 1
    assert result.bytes < len(data) / 4


# --- the daily files: nine day blocks at a one-day step (issue #122) ---------
#
# The daily p-variants are date-major with nine day blocks, ``Date`` stamped
# ``YYYYMMDD0000`` (one per local day), and the point sits at the same row index
# in every block (docs/ogd.md §E4 "Row order"). Row addressing must step by one
# day, not one hour, and serve a whole-run demand from the nine blocks rather
# than downloading the ~1.3 MB file.

_DAY0 = datetime(2026, 8, 27, 0, 0, tzinfo=UTC)  # first day block stamp
_DAILY_DAYS = 9
# ~4000 points per day block so each block is ~110 KB — larger than the geometry
# learn chunk, like the real ~148 KB day blocks — and the whole file is ~1 MB,
# far more than the point's nine rows. Values of varying width so the blocks
# differ in size and a naive extrapolation from the file start drifts, as
# measured upstream (docs/ogd.md §E4).
_DAILY_POINTS = (
    [(pid, 1) for pid in range(1, 2001)]
    + [(309800, 2), (309801, 2)]
    + [(pid, 2) for pid in range(310000, 311998)]
)


def _daily_date_major(*, drop: tuple[int, int, int] | None = None) -> bytes:
    """A date-major daily file: nine day blocks, all points per block.

    ``drop`` removes one ``(point_id, point_type_id, day_index)`` row so a
    missing day can be exercised.
    """
    lines = ["point_id;point_type_id;Date;tre200px"]
    for d in range(_DAILY_DAYS):
        when = _DAY0 + timedelta(days=d)
        for pid, ptype in _DAILY_POINTS:
            if drop == (pid, ptype, d):
                continue
            value = ((pid * 7 + d * 131) % 4000) / 10  # "0.7" … "399.9"
            lines.append(f"{pid};{ptype};{_stamp(when)};{value}")
    return ("\n".join(lines) + "\n").encode("iso-8859-1")


_ONE_DAY = timedelta(days=1)


async def test_daily_whole_run_is_row_addressed_all_nine_days() -> None:
    """The nine day blocks are addressed at a one-day step: every day, only the
    point's rows, far fewer bytes than the whole file (issue #122 acceptance)."""
    data = _daily_date_major()
    result = await _series(data, step=_ONE_DAY)

    assert result.layout is FileLayout.DATE_MAJOR
    assert result.level <= 2
    assert result.whole_run
    # All nine days, in order, stamped YYYYMMDD0000.
    assert _series_hours(result) == [
        _stamp(_DAY0 + timedelta(days=d)) for d in range(_DAILY_DAYS)
    ]
    # Only the target point's rows.
    assert all(
        line.startswith(f"{_TARGET.point_id};{_TARGET.point_type_id};")
        for line in _series_lines(result)
    )
    # Bytes far below the whole file.
    assert result.bytes < len(data) / 4
    # The daily parser reads the returned text unchanged.
    from custom_components.meteoswiss_weather.ogd.forecast import parse_daily

    daily = parse_daily({"tre200px": result.text}, _TARGET)
    assert len(daily) == _DAILY_DAYS


async def test_daily_missing_day_climbs_to_the_full_file() -> None:
    """When a day cannot be addressed, the ladder reads the whole file and
    returns what upstream has (quality first, ADR-0008)."""
    data = _daily_date_major(drop=(_TARGET.point_id, _TARGET.point_type_id, 4))
    result = await _series(data, step=_ONE_DAY)

    assert result.level == 4
    assert result.whole_run
    days = _series_hours(result)
    assert len(days) == _DAILY_DAYS - 1
    assert _stamp(_DAY0 + timedelta(days=4)) not in days


async def test_daily_warm_hint_saves_requests() -> None:
    """A same-day hint skips classification and geometry learning for the daily
    file, exactly as for the hourly files (issue #121)."""
    data = _daily_date_major()
    day = date(2026, 8, 26)  # the file's UTC day (first block is 2026-08-27)
    cold = await _series(data, step=_ONE_DAY, utc_day=day)
    warm = await _series(data, step=_ONE_DAY, hint=cold.hint, utc_day=day)

    assert cold.hint is not None and cold.hint.geometry is not None
    assert _series_hours(warm) == _series_hours(cold)
    assert warm.level == 0
    assert warm.requests < cold.requests


async def test_daily_hourly_step_would_miss_the_day_blocks() -> None:
    """Guard: addressing a daily file with the default one-hour step cannot
    prove the nine daily blocks and would climb, so the one-day step matters."""
    data = _daily_date_major()
    hourly_step = await _series(data, step=timedelta(hours=1))
    day_step = await _series(data, step=_ONE_DAY)

    # The one-hour step treats the run as ~193 h and blows the request cap, so it
    # falls back to the whole file; the one-day step addresses the nine blocks.
    assert hourly_step.level == 4
    assert day_step.level <= 2
    # Both still return the same nine day rows.
    assert _series_hours(hourly_step) == _series_hours(day_step)
