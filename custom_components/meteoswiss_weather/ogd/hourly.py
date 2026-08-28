"""Per-file HTTP-Range strategies for the hourly bulk forecast (issue #50).

The hourly local-forecast files are ~30 MB and hold every one of the ~5,600
points (docs/ogd.md §E4). Downloading the full set every refresh is the whole
traffic budget (ADR-0002). Measured on 2026-08-28, the files have **two**
layouts, and each admits a cheaper Range fetch:

- **date-major** files (`tre200h0`, the `treq*`/`npro*` group) are sorted by
  `Date`, so the earliest hours of all points lead the file — a prefix
  ``Range: bytes=0-<budget>`` covers the wanted horizon;
- **point-major** files (symbol, precipitation, wind, gust, direction, …) are
  sorted so one point's ~220 rows form a contiguous ~5 KB block — a binary
  search over byte offsets with tiny Range probes locates it, then one Range
  GET fetches it.

The layout is **detected at runtime** (byte-offset probes), never hard-coded;
anything unrecognised falls back to the full download. All of this is pure
Python over a small :class:`RangeReader` seam (ADR-0001), so the strategies are
unit-tested against an in-memory reader with no network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Protocol
from zoneinfo import ZoneInfo

import aiohttp

from .const import (
    FORECAST_ENCODING,
    FORECAST_TIMEZONE,
    HOURLY_BLOCK_CHUNK_BYTES,
    HOURLY_BYTES_PER_HOUR,
    HOURLY_HORIZON_FULL_RUN,
    HOURLY_RANGE_SAFETY,
    HOURLY_ROW_PROBE_BYTES,
)
from .http import get_bytes
from .models import FileLayout, ForecastPoint

_LOGGER = logging.getLogger(__name__)

# Number of evenly spaced byte offsets sampled to classify a file's layout.
# More than the three the issue floats: the tiny probes are cheap and extra
# samples make the monotonicity verdict robust on a shuffled/unexpected file.
_LAYOUT_PROBES = 9

# Extra hours added to the date-major prefix budget so the horizon is reached
# even when the per-hour block runs large; the prefix is extended if not.
_HORIZON_MARGIN_HOURS = 6


# ---------------------------------------------------------------------------
# The range-reader seam
# ---------------------------------------------------------------------------


class RangeReader(Protocol):
    """Random byte access to one upstream file (a Range GET per read)."""

    async def size(self) -> int: ...

    async def read(self, start: int, length: int) -> bytes: ...

    async def read_all(self) -> bytes: ...


class AiohttpRangeReader:
    """A :class:`RangeReader` backed by conditional HTTP Range requests.

    Learns the object size from the first probe's ``Content-Range``. A server
    that ignores ``Range`` and answers 200 with the whole body is handled
    transparently: the full body is cached and later reads slice it locally, so
    the strategies still work (they simply stop saving traffic).
    """

    def __init__(self, session: aiohttp.ClientSession, url: str) -> None:
        self._session = session
        self._url = url
        self._size: int | None = None
        self._full: bytes | None = None

    async def _prime(self) -> None:
        resp = await get_bytes(
            self._session, self._url, start=0, end=HOURLY_ROW_PROBE_BYTES - 1
        )
        if resp.status == 200:
            self._full = resp.body
            self._size = len(resp.body)
        else:
            self._size = (
                resp.total_size if resp.total_size is not None else len(resp.body)
            )

    async def size(self) -> int:
        if self._size is None:
            await self._prime()
        assert self._size is not None
        return self._size

    async def read(self, start: int, length: int) -> bytes:
        if length <= 0 or start < 0:
            return b""
        if self._full is not None:
            return self._full[start : start + length]
        resp = await get_bytes(
            self._session, self._url, start=start, end=start + length - 1
        )
        if resp.status == 200:
            # The origin ignored the Range; cache the full body once.
            self._full = resp.body
            self._size = len(resp.body)
            return self._full[start : start + length]
        return resp.body

    async def read_all(self) -> bytes:
        if self._full is None:
            resp = await get_bytes(self._session, self._url)
            self._full = resp.body
            self._size = len(resp.body)
        return self._full


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Row:
    """A parsed data row plus the byte offset it starts at."""

    start: int
    point_id: int
    point_type_id: int
    date: str  # the raw YYYYMMDDHHMM stamp; lexicographic == chronological


def _parse_row(start: int, line: bytes) -> _Row | None:
    """Parse ``id;type;Date;value`` into a :class:`_Row`.

    Returns ``None`` for the header row or any line whose id/type are not
    integers, so a probe that lands in the header is simply "no row here".
    """
    parts = line.split(b";", 3)
    if len(parts) < 3:
        return None
    try:
        point_id = int(parts[0])
        point_type_id = int(parts[1])
    except ValueError:
        return None
    return _Row(start, point_id, point_type_id, parts[2].decode("ascii", "ignore"))


async def _read_row_after(reader: RangeReader, offset: int) -> _Row | None:
    """First complete data row that starts after the newline at/after ``offset``.

    Monotonic in ``offset`` (the basis for the binary search) and always
    returns a whole row: it reads from ``offset`` until it has seen the row's
    opening and closing newline. ``None`` past the last row.
    """
    size = await reader.size()
    if offset >= size:
        return None
    data = b""
    pos = offset
    first_nl = -1
    while True:
        piece = await reader.read(pos, HOURLY_ROW_PROBE_BYTES)
        if not piece:
            break
        data += piece
        pos += len(piece)
        if first_nl == -1:
            first_nl = data.find(b"\n")
        if first_nl != -1 and data.find(b"\n", first_nl + 1) != -1:
            break
        if pos >= size:
            break
    if first_nl == -1:
        return None
    row_start_local = first_nl + 1
    second_nl = data.find(b"\n", row_start_local)
    if second_nl == -1:
        if pos < size:
            return None  # row longer than we read and not at EOF
        line = data[row_start_local:]
    else:
        line = data[row_start_local:second_nl]
    return _parse_row(offset + row_start_local, line)


async def _read_row_before(reader: RangeReader, offset: int) -> _Row | None:
    """The last complete data row that ends at/before ``offset``.

    Used to confirm a cached block start really is the point's *first* row.
    ``None`` when the window did not reach a row boundary (caller re-searches).
    """
    if offset <= 0:
        return None
    w0 = max(0, offset - HOURLY_ROW_PROBE_BYTES)
    data = await reader.read(w0, offset - w0)
    data = data.rstrip(b"\n")
    nl = data.rfind(b"\n")
    if nl == -1:
        if w0 == 0:
            return _parse_row(0, data)  # header or the very first row
        return None
    return _parse_row(w0 + nl + 1, data[nl + 1 :])


async def _read_header(reader: RangeReader) -> bytes:
    """The file's header line, including its trailing newline."""
    pos = 0
    data = b""
    size = await reader.size()
    while True:
        piece = await reader.read(pos, HOURLY_ROW_PROBE_BYTES)
        if not piece:
            break
        data += piece
        pos += len(piece)
        nl = data.find(b"\n")
        if nl != -1:
            return data[: nl + 1]
        if pos >= size:
            break
    return data


# ---------------------------------------------------------------------------
# Layout classification
# ---------------------------------------------------------------------------


def _nondecreasing(values: list) -> bool:
    return all(a <= b for a, b in zip(values, values[1:], strict=False))


def _increasing(values: list) -> bool:
    return all(a < b for a, b in zip(values, values[1:], strict=False))


async def classify_layout(reader: RangeReader) -> FileLayout:
    """Detect a file's row order from evenly spaced byte-offset probes.

    Date-major files show a non-decreasing ``Date`` across the file; the two
    point-major variants show a non-decreasing ``(type, id)`` or ``id`` key;
    anything else (e.g. a shuffled file) is :data:`FileLayout.FALLBACK`, which
    the caller downloads in full.
    """
    size = await reader.size()
    if size == 0:
        return FileLayout.FALLBACK
    offsets = [size * i // _LAYOUT_PROBES for i in range(_LAYOUT_PROBES)]
    rows: list[_Row] = []
    for off in offsets:
        row = await _read_row_after(reader, off)
        if row is not None and (not rows or row.start != rows[-1].start):
            rows.append(row)
    if len(rows) < 3:
        return FileLayout.FALLBACK

    dates = [r.date for r in rows]
    type_keys = [(r.point_type_id, r.point_id) for r in rows]
    ids = [r.point_id for r in rows]

    # Date-major files have widely spaced probes in different hour blocks, so
    # the dates strictly increase; requiring strict monotonicity (not merely
    # non-decreasing) makes a false date-major verdict on a point-major file —
    # the one dangerous misclassification, since it would drop the point's later
    # hours — vanishingly unlikely.
    if _increasing(dates):
        return FileLayout.DATE_MAJOR
    if _nondecreasing(type_keys) and len(set(type_keys)) > 1:
        return FileLayout.POINT_MAJOR_TYPE
    if _nondecreasing(ids) and len(set(ids)) > 1:
        return FileLayout.POINT_MAJOR_ID
    return FileLayout.FALLBACK


def _row_key(layout: FileLayout, row: _Row) -> tuple:
    if layout is FileLayout.POINT_MAJOR_TYPE:
        return (row.point_type_id, row.point_id)
    return (row.point_id,)


def _target_key(layout: FileLayout, point: ForecastPoint) -> tuple:
    if layout is FileLayout.POINT_MAJOR_TYPE:
        return (point.point_type_id, point.point_id)
    return (point.point_id,)


# ---------------------------------------------------------------------------
# Point-major: binary search for the point's contiguous block
# ---------------------------------------------------------------------------


async def _lower_bound(reader: RangeReader, layout: FileLayout, target: tuple) -> int:
    """Smallest offset whose following row has ``key >= target`` (bisect left)."""
    lo, hi = 0, await reader.size()
    while lo < hi:
        mid = (lo + hi) // 2
        row = await _read_row_after(reader, mid)
        if row is None or _row_key(layout, row) >= target:
            hi = mid
        else:
            lo = mid + 1
    return lo


async def _cached_start_valid(
    reader: RangeReader, layout: FileLayout, target: tuple, block_start: int
) -> bool:
    """Whether ``block_start`` is still the point's first row (one-probe check)."""
    at = await _read_row_after(reader, max(0, block_start - 1))
    if at is None or at.start != block_start or _row_key(layout, at) != target:
        return False
    before = await _read_row_before(reader, block_start)
    if before is None:
        return False  # cannot prove it is the first row → re-search to be safe
    return _row_key(layout, before) < target


async def _read_block_forward(
    reader: RangeReader, layout: FileLayout, target: tuple, block_start: int
) -> bytes:
    """Read rows from ``block_start`` until the key leaves ``target`` or EOF."""
    size = await reader.size()
    pos = block_start
    collected = bytearray()
    leftover = b""
    while pos < size:
        chunk = await reader.read(pos, HOURLY_BLOCK_CHUNK_BYTES)
        if not chunk:
            break
        pos += len(chunk)
        data = leftover + chunk
        leftover = b""
        while True:
            nl = data.find(b"\n")
            if nl == -1:
                leftover = data
                break
            line, data = data[:nl], data[nl + 1 :]
            row = _parse_row(0, line)
            if row is None:
                continue
            if _row_key(layout, row) == target:
                collected += line + b"\n"
            else:
                return bytes(collected)
    if leftover:
        row = _parse_row(0, leftover)
        if row is not None and _row_key(layout, row) == target:
            collected += leftover + b"\n"
    return bytes(collected)


async def _fetch_point_major(
    reader: RangeReader,
    layout: FileLayout,
    point: ForecastPoint,
    cached_start: int | None,
) -> tuple[str, int | None]:
    """Fetch the point's contiguous block; returns ``(csv_text, block_start)``.

    ``csv_text`` carries the header so the shared parser reads it unchanged;
    ``block_start`` is the offset to cache for the next run.
    """
    target = _target_key(layout, point)
    header = await _read_header(reader)

    start: int | None = None
    if cached_start is not None and await _cached_start_valid(
        reader, layout, target, cached_start
    ):
        start = cached_start
    if start is None:
        lo = await _lower_bound(reader, layout, target)
        first = await _read_row_after(reader, lo)
        if first is None or _row_key(layout, first) != target:
            return header.decode(FORECAST_ENCODING), None  # point absent
        start = first.start

    block = await _read_block_forward(reader, layout, target, start)
    return (header + block).decode(FORECAST_ENCODING), start


# ---------------------------------------------------------------------------
# Date-major: horizon prefix
# ---------------------------------------------------------------------------


def _dt_from_stamp(stamp: str) -> datetime | None:
    digits = stamp.strip()
    if len(digits) < 12 or not digits[:12].isdigit():
        return None
    return datetime(
        int(digits[:4]),
        int(digits[4:6]),
        int(digits[6:8]),
        int(digits[8:10]),
        int(digits[10:12]),
        tzinfo=UTC,
    )


def _last_complete_date(data: bytes, at_eof: bool) -> datetime | None:
    """Date of the last *complete* row in ``data`` (its max, date-major)."""
    end = len(data)
    if not at_eof:
        end = data.rfind(b"\n")  # drop the trailing partial row
        if end <= 0:
            return None
    seg = data[:end].rstrip(b"\n")
    nl = seg.rfind(b"\n")
    line = seg[nl + 1 :] if nl != -1 else seg
    row = _parse_row(0, line)
    return _dt_from_stamp(row.date) if row is not None else None


async def _fetch_date_major(
    reader: RangeReader, horizon_end: datetime | None
) -> str:
    """Fetch the prefix that covers ``horizon_end`` (or the whole file)."""
    size = await reader.size()
    if horizon_end is None:
        return (await reader.read_all()).decode(FORECAST_ENCODING)

    first = await _read_row_after(reader, 0)
    start_dt = _dt_from_stamp(first.date) if first is not None else None
    if start_dt is None:
        return (await reader.read_all()).decode(FORECAST_ENCODING)

    span_hours = ceil((horizon_end - start_dt).total_seconds() / 3600)
    hours = max(1, span_hours) + _HORIZON_MARGIN_HOURS
    end = min(int(hours * HOURLY_BYTES_PER_HOUR * HOURLY_RANGE_SAFETY), size)

    while True:
        data = await reader.read(0, end)
        at_eof = end >= size
        covered = _last_complete_date(data, at_eof)
        if at_eof or (covered is not None and covered >= horizon_end):
            break
        end = min(end * 2, size)  # horizon not reached yet → widen the prefix
    return data.decode(FORECAST_ENCODING)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HourlyFileResult:
    """One file's fetched CSV text, the layout used, and the offset to cache."""

    text: str
    layout: FileLayout
    block_start: int | None


async def fetch_hourly_file(
    session: aiohttp.ClientSession,
    url: str,
    point: ForecastPoint,
    *,
    horizon_end: datetime | None,
    cached_start: int | None = None,
) -> HourlyFileResult:
    """Fetch one hourly parameter file with the cheapest strategy for its layout."""
    reader = AiohttpRangeReader(session, url)
    layout = await classify_layout(reader)

    if layout is FileLayout.DATE_MAJOR:
        return HourlyFileResult(
            text=await _fetch_date_major(reader, horizon_end),
            layout=layout,
            block_start=None,
        )
    if layout in (FileLayout.POINT_MAJOR_TYPE, FileLayout.POINT_MAJOR_ID):
        text, block_start = await _fetch_point_major(
            reader, layout, point, cached_start
        )
        return HourlyFileResult(text=text, layout=layout, block_start=block_start)

    # FALLBACK: an unrecognised order — download the whole file and warn, so a
    # layout change upstream is visible rather than silently over-trimming.
    _LOGGER.warning(
        "hourly file %s has an unrecognised row order; downloading it in full", url
    )
    return HourlyFileResult(
        text=(await reader.read_all()).decode(FORECAST_ENCODING),
        layout=FileLayout.FALLBACK,
        block_start=None,
    )


async def fetch_wind_block(
    session: aiohttp.ClientSession,
    url: str,
    point: ForecastPoint,
    *,
    cached_start: int | None = None,
) -> HourlyFileResult | None:
    """Fetch the point's block from a point-major wind file for the daily wind.

    The daily wind feature must never trigger a full 30 MB download (ADR-0002
    guardrail, issue #60): returns ``None`` when the file is not point-major so
    the caller can set the daily wind fields to ``None`` and log a warning.
    When the file is point-major the full point block (~5 KB) is fetched and
    returned as a :class:`HourlyFileResult`.
    """
    reader = AiohttpRangeReader(session, url)
    layout = await classify_layout(reader)
    if layout not in (FileLayout.POINT_MAJOR_TYPE, FileLayout.POINT_MAJOR_ID):
        return None
    text, block_start = await _fetch_point_major(reader, layout, point, cached_start)
    return HourlyFileResult(text=text, layout=layout, block_start=block_start)


def horizon_end_utc(horizon_days: int | None, now: datetime) -> datetime | None:
    """UTC cut-off for the hourly horizon, or ``None`` for the full run.

    The horizon is counted in full **local calendar days** (Europe/Zurich, the
    boundary the daily p-variants and the app use, docs/ogd.md §E4): the result
    is local midnight at the end of ``today + horizon_days``. ``horizon_days=0``
    is the rest of today; the default 2 is the rest of today plus two full days.
    """
    if horizon_days is None or horizon_days == HOURLY_HORIZON_FULL_RUN:
        return None
    tz = ZoneInfo(FORECAST_TIMEZONE)
    now_local = now.astimezone(tz)
    start_of_today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_of_today + timedelta(days=horizon_days + 1)
    return end_local.astimezone(UTC)
