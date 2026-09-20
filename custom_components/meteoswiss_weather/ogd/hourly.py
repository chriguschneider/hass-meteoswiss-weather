"""Per-file HTTP-Range strategies for the hourly bulk forecast (issue #50).

The hourly local-forecast files are ~30 MB and hold every one of the ~5,600
points (docs/ogd.md §E4). Downloading the full set every refresh is the whole
traffic budget (ADR-0002). Measured on 2026-08-28, the files have **two**
layouts, and each admits a cheaper Range fetch:

- **date-major** files (`tre200h0`, the `treq*` group) are sorted by
  `Date`, so the earliest hours of all points lead the file — a prefix
  ``Range: bytes=0-<budget>`` covers the wanted horizon;
- **point-major** files (symbol, precipitation, wind, gust, direction, and —
  since a silent upstream re-sort on 2026-08-31 (issue #100) — the `npro*`
  cloud group) are sorted so one point's ~220 rows form a contiguous ~5 KB
  block — a binary search over byte offsets with tiny Range probes locates
  it, then one Range GET fetches it.

The layout is **detected at runtime** (byte-offset probes), never hard-coded;
anything unrecognised falls back to the full download. All of this is pure
Python over a small :class:`RangeReader` seam (ADR-0001), so the strategies are
unit-tested against an in-memory reader with no network.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from math import ceil
from typing import Any, Protocol
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
    SERIES_REQUEST_CAP,
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

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        limiter: asyncio.Semaphore | None = None,
    ) -> None:
        self._session = session
        self._url = url
        self._limiter = limiter
        self._size: int | None = None
        self._full: bytes | None = None

    async def _prime(self) -> None:
        resp = await get_bytes(
            self._session,
            self._url,
            start=0,
            end=HOURLY_ROW_PROBE_BYTES - 1,
            limiter=self._limiter,
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
            self._session,
            self._url,
            start=start,
            end=start + length - 1,
            limiter=self._limiter,
        )
        if resp.status == 200:
            # The origin ignored the Range; cache the full body once.
            self._full = resp.body
            self._size = len(resp.body)
            return self._full[start : start + length]
        return resp.body

    async def read_all(self) -> bytes:
        if self._full is None:
            resp = await get_bytes(self._session, self._url, limiter=self._limiter)
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

    # A many-block date-major file (the hourly files, ~220 hour blocks) has
    # every widely spaced probe in a different block, so the dates strictly
    # increase; requiring strict monotonicity here makes a false date-major
    # verdict on a point-major file — the one dangerous misclassification, since
    # it would drop the point's later hours — vanishingly unlikely.
    if _increasing(dates):
        return FileLayout.DATE_MAJOR
    if _nondecreasing(type_keys) and len(set(type_keys)) > 1:
        return FileLayout.POINT_MAJOR_TYPE
    if _nondecreasing(ids) and len(set(ids)) > 1:
        return FileLayout.POINT_MAJOR_ID
    # A few-block date-major file (the daily p-variants, only nine day blocks)
    # has more probes than blocks, so its dates are non-decreasing with repeats
    # rather than strictly increasing (issue #122). This branch is reached only
    # after the point-major keys are shown *not* to be monotonic, so a genuine
    # point-major file (whose sort key is monotonic by construction) is caught
    # above and never lands here — the dangerous misclassification stays ruled
    # out. A file that reaches here and is actually point-major would still be
    # caught by the addressing's completeness check and climb, not silently
    # truncate.
    if _nondecreasing(dates) and len(set(dates)) > 1:
        return FileLayout.DATE_MAJOR
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
    header: bytes | None = None,
) -> tuple[str, int | None]:
    """Fetch the point's contiguous block; returns ``(csv_text, block_start)``.

    ``csv_text`` carries the header so the shared parser reads it unchanged;
    ``block_start`` is the offset to cache for the next run. ``header`` is the
    already-known header line (from a same-day hint); ``None`` reads it.
    """
    target = _target_key(layout, point)
    if header is None:
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
# The escalation ladder (ADR-0008 section 4)
# ---------------------------------------------------------------------------
#
# ``fetch_series`` returns the configured point's rows of one file. It starts
# with the cheapest strategy the file's layout admits and climbs until a level
# *verifies complete*; the last rung is the whole file. It never gives up for
# cost — only upstream errors (raised as OgdConnectionError) end it early.
#
#   L0  a same-day FileHint: skip classification and the header/first/last
#       probes, address straight from the remembered positions, verify the rows
#   L1  layout-aware addressing (binary-searched block / row addressing)
#   L2  a window of whole hour blocks around a row that L1 could not find
#   L3  the prefix up to the end of the demanded window
#   L4  the whole file
#
# A hint (one object per file, remembered per UTC day, issue #121) only ever
# saves requests: a stale one — from another day or naming the wrong layout — is
# detected by the rows it fails to yield and retried once with a fresh
# classification before the ladder climbs.

# Row-addressing windows around a predicted row position: L1 tries these in
# turn; L2 then reads a few whole hour blocks around the prediction.
_ROW_WINDOWS = (2048, 16384)
_BLOCK_WINDOW_BLOCKS = 2.5
# Chunk size while learning a date-major file's geometry from its first block.
_LEARN_CHUNK_BYTES = 65_536
# Classification, header, first/last row and geometry learning, on top of one
# read per demanded hour, when deciding whether row addressing fits the cap.
_ADDRESSING_OVERHEAD_REQUESTS = 16


class _NotProven(Exception):
    """A level could not prove it delivered every demanded row; climb."""


class _RequestCapExceeded(_NotProven):
    """A level needed more requests than the cap allows; climb."""


class _CountingReader:
    """A :class:`RangeReader` that counts requests/bytes and enforces a cap."""

    def __init__(self, inner: RangeReader, cap: int | None) -> None:
        self._inner = inner
        self.cap = cap
        self.requests = 0
        self.bytes = 0

    def _charge(self) -> None:
        self.requests += 1
        if self.cap is not None and self.requests > self.cap:
            raise _RequestCapExceeded(f"more than {self.cap} requests")

    async def size(self) -> int:
        return await self._inner.size()

    async def read(self, start: int, length: int) -> bytes:
        self._charge()
        data = await self._inner.read(start, length)
        self.bytes += len(data)
        return data

    async def read_all(self) -> bytes:
        self._charge()
        data = await self._inner.read_all()
        self.bytes += len(data)
        return data


@dataclass(frozen=True, slots=True)
class RowGeometry:
    """Where a point's row sits in a date-major file (a hint, always verified).

    Every hour block of a date-major file lists the same points in the same
    order (measured 2026-09-18), so the point is ``row_offset`` bytes into
    every block, give or take the width of a few values. ``block_bytes`` is the
    running estimate of one hour block's size. ``anchor_stamp``/``anchor_offset``
    name one row found last time: byte offsets are stable across the runs of a
    UTC day, so the next fetch starts its predictions right next to its window
    instead of extrapolating from the file start.
    """

    block_bytes: float
    row_offset: int
    anchor_stamp: str | None = None
    anchor_offset: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serialisable form for persistence (issue #133)."""
        return {
            "block_bytes": self.block_bytes,
            "row_offset": self.row_offset,
            "anchor_stamp": self.anchor_stamp,
            "anchor_offset": self.anchor_offset,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RowGeometry:
        """Rebuild from :meth:`to_dict`; raises on a value it cannot trust."""
        return cls(
            block_bytes=float(data["block_bytes"]),
            row_offset=int(data["row_offset"]),
            anchor_stamp=_opt_str(data.get("anchor_stamp")),
            anchor_offset=_opt_int(data.get("anchor_offset")),
        )


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


@dataclass(frozen=True, slots=True)
class FileHint:
    """Everything learned about one file, remembered per UTC day (ADR-0008).

    A file's layout and byte offsets are stable across the runs of one UTC day
    (date-major files start at 21:00 UTC of the previous day, docs/ogd.md), so a
    same-day hint lets :func:`fetch_series` skip classification and the
    header/first/last probes and verify through the rows it finds instead.

    ``utc_day`` is the UTC day the offsets were learned on; a hint from another
    day is not trusted (a layout can flip between days without notice), so it
    costs a fresh look rather than a wrong or oversized read. ``header`` is the
    file's header line (with its trailing newline) so the point's rows can be
    re-emitted without reading it. ``block_start`` is the point-major block
    offset; ``geometry`` plus ``first_stamp``/``last_stamp`` are the date-major
    row-addressing hint. Only the fields that apply to the file's layout are set.
    """

    layout: FileLayout
    utc_day: date | None = None
    header: str = ""
    block_start: int | None = None
    geometry: RowGeometry | None = None
    first_stamp: str | None = None
    last_stamp: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serialisable form for persistence across restarts (issue #133).

        The layout enum becomes its string value and ``utc_day`` an ISO date;
        everything else is already JSON-native. Only ever a hint: the ladder
        re-verifies whatever :meth:`from_dict` hands back (ADR-0008).
        """
        return {
            "layout": self.layout.value,
            "utc_day": self.utc_day.isoformat() if self.utc_day else None,
            "header": self.header,
            "block_start": self.block_start,
            "geometry": self.geometry.to_dict() if self.geometry else None,
            "first_stamp": self.first_stamp,
            "last_stamp": self.last_stamp,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FileHint:
        """Rebuild from :meth:`to_dict`.

        Raises on anything it cannot trust (a bad ``layout`` value, a non-mapping
        geometry, a malformed date) so the caller
        (:meth:`~.backend.BulkCsvBackend.import_hints`) can drop just that entry —
        a corrupt store must cost a fresh look, never a wrong row (the ladder
        verifies every remembered position anyway).
        """
        geometry = data.get("geometry")
        utc_day = data.get("utc_day")
        return cls(
            layout=FileLayout(data["layout"]),
            utc_day=date.fromisoformat(utc_day) if utc_day else None,
            header=str(data.get("header", "")),
            block_start=_opt_int(data.get("block_start")),
            geometry=RowGeometry.from_dict(geometry) if geometry else None,
            first_stamp=_opt_str(data.get("first_stamp")),
            last_stamp=_opt_str(data.get("last_stamp")),
        )


@dataclass(frozen=True, slots=True)
class SeriesResult:
    """The point's rows of one file, and what it took to get them."""

    text: str  # header + only the point's rows, ready for the shared parsers
    layout: FileLayout
    level: int  # 0..4, the highest rung of the ladder that was needed
    requests: int
    bytes: int
    # True when ``text`` holds every row of the point in the file, so any
    # consumer may reuse it; False when it was cut to the demanded window.
    whole_run: bool
    # What was learned about the file, to hand back on the next fetch of the
    # same UTC day (one object per file, ADR-0008). ``None`` after an escalation
    # that learned no reusable position, so the caller keeps its previous hint.
    hint: FileHint | None = None

    @property
    def has_rows(self) -> bool:
        """Whether the file carried any row for the point."""
        return self.text.count("\n") > 1


def _needle(point: ForecastPoint, stamp: str) -> bytes:
    return f"\n{point.point_id};{point.point_type_id};{stamp};".encode("ascii")


def _stamp(when: datetime) -> str:
    return when.strftime("%Y%m%d%H%M")


def _filter_point_rows(text: str, point: ForecastPoint) -> str:
    """Header plus only ``point``'s rows. A plain function (runs in an executor)."""
    prefix = f"{point.point_id};{point.point_type_id};"
    lines = text.split("\n")
    kept = [lines[0], *(line for line in lines[1:] if line.startswith(prefix))]
    return "\n".join(kept) + "\n"


def _point_stamps(text: str) -> list[str]:
    """The ``Date`` stamps of the data rows in an already-filtered text."""
    return [
        parts[2]
        for line in text.split("\n")[1:]
        if len(parts := line.split(";", 3)) >= 3
    ]


def _window_hours(
    start: datetime,
    end: datetime,
    first: datetime,
    last: datetime,
    step: timedelta = timedelta(hours=1),
) -> list[datetime]:
    """The block stamps of ``[start, end)`` that the file ``[first, last]`` covers.

    ``step`` is the spacing between a date-major file's blocks: one hour for the
    hourly files, one day for the daily ``p``-variants whose ``Date`` stamps are
    ``YYYYMMDD0000`` (docs/ogd.md §E4 "Row order"). The stamps are generated from
    ``first`` by whole steps so they land exactly on the file's block boundaries.
    """
    lo = max(start, first)
    hi = min(end, last + step)
    hours = []
    # Walk from the file's first block by whole steps and keep the ones inside
    # the window, so a day step lands on 00:00 stamps and an hour step on the
    # hour regardless of where ``start`` falls.
    when = first
    while when < hi:
        if when >= lo:
            hours.append(when)
        when += step
    return hours


async def _learn_geometry(
    reader: RangeReader, point: ForecastPoint, first: _Row
) -> RowGeometry:
    """Scan the first hour block for its size and the point's offset in it."""
    size = await reader.size()
    pos = first.start
    line_start = first.start
    pending = b""
    row_offset: int | None = None
    while pos < size:
        chunk = await reader.read(pos, _LEARN_CHUNK_BYTES)
        if not chunk:
            break
        pos += len(chunk)
        data = pending + chunk
        cursor = 0
        while (nl := data.find(b"\n", cursor)) != -1:
            row = _parse_row(line_start, data[cursor:nl])
            if row is not None:
                if row.date != first.date:
                    if row_offset is None:
                        raise _NotProven("point not in the first hour block")
                    return RowGeometry(
                        block_bytes=float(line_start - first.start),
                        row_offset=row_offset,
                    )
                if (
                    row.point_id == point.point_id
                    and row.point_type_id == point.point_type_id
                ):
                    row_offset = line_start - first.start
            line_start += nl - cursor + 1
            cursor = nl + 1
        pending = data[cursor:]
    if row_offset is None:
        raise _NotProven("point not in the first hour block")
    # A single-block file: the block runs to the end of the file.
    return RowGeometry(block_bytes=float(size - first.start), row_offset=row_offset)


async def _find_row(
    reader: RangeReader,
    predicted: float,
    needle: bytes,
    windows: tuple[int, ...],
    *,
    from_window: int = 0,
) -> tuple[int, bytes, int] | None:
    """Look for ``needle`` in windows centred on ``predicted``.

    Returns ``(offset, line, window_index)`` — the index of the window that hit,
    so the caller can start the next row's search there. ``from_window`` skips
    the smaller windows a previous row already found too tight, so a drifting
    file does not pay a miss on every hour.
    """
    size = await reader.size()
    for idx in range(from_window, len(windows)):
        window = windows[idx]
        start = max(0, int(predicted) - window // 2)
        if start >= size:
            return None
        data = await reader.read(start, window)
        at = data.find(needle)
        if at == -1:
            continue
        line_start = at + 1
        end = data.find(b"\n", line_start)
        if end == -1:
            data += await reader.read(start + len(data), HOURLY_ROW_PROBE_BYTES)
            end = data.find(b"\n", line_start)
            if end == -1:
                end = len(data)
        return start + line_start, data[line_start:end], idx
    return None


@dataclass(frozen=True, slots=True)
class _DateMajorRows:
    """The row-addressing result of one date-major fetch, plus what it learned."""

    text: str
    level: int
    geometry: RowGeometry
    header: str
    first_stamp: str
    last_stamp: str


async def _fetch_rows_date_major(
    reader: RangeReader,
    point: ForecastPoint,
    window_start: datetime | None,
    window_end: datetime | None,
    hint: FileHint | None,
    step: timedelta = timedelta(hours=1),
    request_cap: int | None = None,
) -> _DateMajorRows:
    """Row-address the point's rows for the window.

    Each verified row re-anchors the prediction for the next block, because
    blocks differ by a few dozen bytes (variable-width values) and a position
    extrapolated from the file start drifts by kilobytes over the run. A same-day
    ``hint`` supplies the header, the file's first/last stamp and the geometry,
    so classification and the header/first/last probes are skipped; the rows
    found still verify it. Once a row needs a wider search window, the following
    rows start there too, so a drifting file does not pay a miss every block.

    ``step`` is the spacing between blocks: one hour for the hourly files, one
    day for the daily ``p``-variants (``Date`` stamped ``YYYYMMDD0000``). A
    ``window_start``/``window_end`` of ``None`` means "the whole run", resolved
    to the file's own extent so a date-major file with few blocks (the nine day
    blocks of the daily files) is row-addressed instead of downloaded whole
    (issue #122). ``request_cap`` bounds that: a whole run whose block count plus
    the addressing overhead exceeds it raises :class:`_RequestCapExceeded` so the
    ladder climbs, keeping a ~220-block hourly run off row addressing.
    """
    geometry = hint.geometry if hint is not None else None
    level = 0 if geometry is not None else 1

    # Same-day offsets are stable: take the header and the file's first/last
    # stamps from the hint when it has them, else read them.
    if hint is not None and hint.header and hint.first_stamp and hint.last_stamp:
        header = hint.header.encode(FORECAST_ENCODING)
        first_start = len(header)
        first_stamp, last_stamp = hint.first_stamp, hint.last_stamp
        first_dt = _dt_from_stamp(first_stamp)
        last_dt = _dt_from_stamp(last_stamp)
    else:
        header = await _read_header(reader)
        size = await reader.size()
        first = await _read_row_after(reader, 0)
        last = await _read_row_before(reader, size)
        first_dt = _dt_from_stamp(first.date) if first is not None else None
        last_dt = _dt_from_stamp(last.date) if last is not None else None
        if first is None or first_dt is None or last_dt is None:
            raise _NotProven("could not read the file's first/last row")
        first_start = first.start
        first_stamp, last_stamp = first.date, last.date

    if first_dt is None or last_dt is None:
        raise _NotProven("could not read the file's first/last stamp")

    # A whole-run demand (window None) is served by addressing every block from
    # the file's first to its last, so a few-block date-major file (the daily
    # p-variants) never falls to the full download (issue #122).
    ws = window_start if window_start is not None else first_dt
    we = window_end if window_end is not None else last_dt + step
    hours = _window_hours(ws, we, first_dt, last_dt, step)
    # Decide by the number of blocks, not the window length alone: a run whose
    # block count would overrun the request cap climbs instead of storming the
    # origin (ADR-0008 section 4).
    if request_cap is not None and len(hours) + _ADDRESSING_OVERHEAD_REQUESTS > (
        request_cap
    ):
        raise _RequestCapExceeded("run has too many blocks for row addressing")
    if not hours:
        return _DateMajorRows(
            text=header.decode(FORECAST_ENCODING),
            level=0,
            geometry=geometry or RowGeometry(block_bytes=0.0, row_offset=0),
            header=header.decode(FORECAST_ENCODING),
            first_stamp=first_stamp,
            last_stamp=last_stamp,
        )

    if geometry is None:
        first_row = _Row(first_start, point.point_id, point.point_type_id, first_stamp)
        geometry = await _learn_geometry(reader, point, first_row)
    block = geometry.block_bytes
    # The anchor is a row found on the last same-day run; predictions start from
    # it (offsets are stable across a day) and fall back to the first block when
    # it is out of range. The rows found below verify it either way.
    anchor_idx, anchor_off = 0, first_start + geometry.row_offset
    anchor_dt = (
        _dt_from_stamp(geometry.anchor_stamp) if geometry.anchor_stamp else None
    )
    if (
        anchor_dt is not None
        and geometry.anchor_offset is not None
        and anchor_dt >= first_dt
    ):
        anchor_idx = int((anchor_dt - first_dt) / step)
        anchor_off = geometry.anchor_offset

    lines: list[bytes] = []
    first_found: tuple[str, int] | None = None
    from_window = 0
    for when in hours:
        idx = int((when - first_dt) / step)
        predicted = anchor_off + (idx - anchor_idx) * block
        needle = _needle(point, _stamp(when))
        found = await _find_row(
            reader, predicted, needle, _ROW_WINDOWS, from_window=from_window
        )
        if found is None:
            level = 2
            found = await _find_row(
                reader, predicted, needle, (int(block * _BLOCK_WINDOW_BLOCKS),)
            )
            if found is not None:
                found = (found[0], found[1], len(_ROW_WINDOWS) - 1)
        if found is None:
            raise _NotProven(f"row for {_stamp(when)} not found by addressing")
        offset, line, hit_window = found
        if hit_window > 0:
            level = max(level, 1)
        # Adapt the next row's starting window to the drift just observed: when
        # the prediction lands well inside the small window keep using it (a few
        # bytes per hour); when the row keeps sitting outside it, start wider so
        # a drifting file does not pay a miss on every hour.
        error = abs(offset - int(predicted))
        from_window = 0 if 2 * error < _ROW_WINDOWS[0] else 1
        if idx != anchor_idx:
            block = (block + (offset - anchor_off) / (idx - anchor_idx)) / 2
        anchor_idx, anchor_off = idx, offset
        if first_found is None:
            first_found = (_stamp(when), offset)
        lines.append(line)

    text = (header + b"\n".join(lines) + b"\n").decode(FORECAST_ENCODING)
    learned = RowGeometry(
        block_bytes=block,
        row_offset=geometry.row_offset,
        anchor_stamp=first_found[0] if first_found else None,
        anchor_offset=first_found[1] if first_found else None,
    )
    return _DateMajorRows(
        text=text,
        level=level,
        geometry=learned,
        header=header.decode(FORECAST_ENCODING),
        first_stamp=first_stamp,
        last_stamp=last_stamp,
    )


def _window_complete(
    text: str,
    window_start: datetime,
    window_end: datetime,
    step: timedelta = timedelta(hours=1),
) -> bool:
    """Whether a filtered prefix text holds every block it can be expected to."""
    stamps = _point_stamps(text)
    if not stamps:
        return False
    first_dt, last_dt = _dt_from_stamp(stamps[0]), _dt_from_stamp(stamps[-1])
    if first_dt is None or last_dt is None:
        return False
    have = set(stamps)
    return all(
        _stamp(hour) in have
        for hour in _window_hours(window_start, window_end, first_dt, last_dt, step)
    ) and last_dt + step >= window_end


def _hint_is_current(hint: FileHint, utc_day: date | None) -> bool:
    """Whether ``hint``'s remembered offsets can still be trusted.

    Offsets are stable only across the runs of one UTC day (ADR-0008). A hint
    from another day is not trusted — a layout can flip between days without
    notice — so it is dropped and the file gets a fresh classification. When the
    day of either side is unknown the rows found still verify the hint, so it is
    used but its offsets are re-anchored against what is actually there.
    """
    if hint.utc_day is None or utc_day is None:
        return True
    return hint.utc_day == utc_day


async def _address(
    reader: _CountingReader,
    point: ForecastPoint,
    window_start: datetime | None,
    window_end: datetime | None,
    hint: FileHint | None,
    layout: FileLayout,
    utc_day: date | None,
    step: timedelta = timedelta(hours=1),
) -> SeriesResult:
    """One addressing attempt for a known ``layout``; raises ``_NotProven`` to
    climb. ``hint`` (when given) supplies the header and byte positions to skip
    the probes; the rows found always verify it. ``step`` is the block spacing
    (one hour for the hourly files, one day for the daily p-variants)."""
    windowed = window_start is not None and window_end is not None
    if layout in (FileLayout.POINT_MAJOR_TYPE, FileLayout.POINT_MAJOR_ID):
        header = _hint_header_bytes(hint)
        block_start = hint.block_start if hint is not None else None
        text, start = await _fetch_point_major(
            reader, layout, point, block_start, header
        )
        stamps = _point_stamps(text)
        # The block is read until the key changes, so it is complete by
        # construction; an empty or unordered one is "not proven". When the
        # layout came from a hint (never confirmed by classification), the block
        # must also cover the demanded window: a hint that names the wrong layout
        # yields at most a stray row on a date-major file, which fails this and
        # falls back to a fresh classification (ADR-0008).
        ordered = bool(stamps) and stamps == sorted(set(stamps))
        proven = ordered and (
            not windowed or _window_complete(text, window_start, window_end, step)
        )
        if proven:
            header_line = text.split("\n", 1)[0] + "\n"
            return SeriesResult(
                text=text,
                layout=layout,
                level=0 if block_start is not None and start == block_start else 1,
                requests=reader.requests,
                bytes=reader.bytes,
                whole_run=True,
                hint=FileHint(
                    layout=layout,
                    utc_day=utc_day,
                    header=header_line,
                    block_start=start,
                ),
            )
        raise _NotProven("empty or unordered point block")
    if layout is FileLayout.DATE_MAJOR:
        if windowed:
            assert window_start is not None and window_end is not None
            demanded = int((window_end - window_start) / step) + 1
            if reader.cap is not None and (
                demanded + _ADDRESSING_OVERHEAD_REQUESTS > reader.cap
            ):
                raise _RequestCapExceeded("window too long for row addressing")
        # A windowed demand's cap was pre-checked above; a whole-run demand
        # (window None) cannot be, since the block count is only known once the
        # file's extent is read, so the cap is enforced inside the addressing
        # after it reads the first/last stamp (issue #122).
        rows = await _fetch_rows_date_major(
            reader,
            point,
            window_start,
            window_end,
            hint,
            step,
            request_cap=None if windowed else reader.cap,
        )
        return SeriesResult(
            text=rows.text,
            layout=layout,
            level=rows.level,
            requests=reader.requests,
            bytes=reader.bytes,
            # A whole-run demand served by addressing every block holds the
            # point's whole run; a windowed one was cut to the window.
            whole_run=not windowed,
            hint=FileHint(
                layout=layout,
                utc_day=utc_day,
                header=rows.header,
                geometry=rows.geometry,
                first_stamp=rows.first_stamp,
                last_stamp=rows.last_stamp,
            ),
        )
    raise _NotProven("no addressing strategy for this layout and demand")


def _hint_header_bytes(hint: FileHint | None) -> bytes | None:
    """The header line of a same-day hint, or ``None`` to read it afresh."""
    if hint is None or not hint.header:
        return None
    return hint.header.encode(FORECAST_ENCODING)


async def _fetch_series(
    reader: _CountingReader,
    point: ForecastPoint,
    *,
    window_start: datetime | None,
    window_end: datetime | None,
    hint: FileHint | None,
    utc_day: date | None,
    step: timedelta = timedelta(hours=1),
) -> SeriesResult:
    """Climb the ladder over ``reader`` (the network-free core of fetch_series)."""
    loop = asyncio.get_running_loop()
    layout = FileLayout.FALLBACK
    windowed = window_start is not None and window_end is not None
    fresh = hint if (hint is not None and _hint_is_current(hint, utc_day)) else None
    try:
        if fresh is not None:
            # A same-day hint skips classification and the probes; the rows it
            # yields verify it. A cap overflow escalates (the layout is known);
            # any other failure retries once with a fresh classification.
            layout = fresh.layout
            try:
                return await _address(
                    reader, point, window_start, window_end, fresh, layout,
                    utc_day, step,
                )
            except _RequestCapExceeded:
                raise
            except _NotProven as reason:
                _LOGGER.debug("hint did not verify; fresh classification: %s", reason)
        layout = await classify_layout(reader)
        return await _address(
            reader, point, window_start, window_end, None, layout, utc_day, step
        )
    except _NotProven as reason:
        _LOGGER.debug("series fetch escalates past addressing: %s", reason)

    # From here on bytes are spent rather than requests.
    reader.cap = None
    if layout is FileLayout.DATE_MAJOR and windowed:
        assert window_start is not None and window_end is not None
        prefix = await _fetch_date_major(reader, window_end)
        text = await loop.run_in_executor(None, _filter_point_rows, prefix, point)
        if _window_complete(text, window_start, window_end, step):
            return SeriesResult(
                text=text,
                layout=layout,
                level=3,
                requests=reader.requests,
                bytes=reader.bytes,
                whole_run=False,
            )

    full = (await reader.read_all()).decode(FORECAST_ENCODING)
    text = await loop.run_in_executor(None, _filter_point_rows, full, point)
    return SeriesResult(
        text=text,
        layout=layout,
        level=4,
        requests=reader.requests,
        bytes=reader.bytes,
        whole_run=True,
    )


async def fetch_series(
    session: aiohttp.ClientSession,
    url: str,
    point: ForecastPoint,
    *,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    hint: FileHint | None = None,
    utc_day: date | None = None,
    step: timedelta = timedelta(hours=1),
    request_cap: int | None = SERIES_REQUEST_CAP,
    limiter: asyncio.Semaphore | None = None,
) -> SeriesResult:
    """Fetch ``point``'s rows of one file, as cheaply as can be proven complete.

    ``window_start``/``window_end`` (aware UTC) name the hours the caller needs;
    ``None`` means the whole run. ``step`` is the spacing between a date-major
    file's blocks — one hour for the hourly files, one day for the daily
    ``p``-variants (``Date`` stamped ``YYYYMMDD0000``, docs/ogd.md §E4). A
    whole-run demand on a date-major file is served by row addressing when the
    file has few blocks (the nine day blocks of the daily files) and climbs to
    the full file otherwise (issue #122). ``hint`` is the :class:`FileHint` a
    previous :class:`SeriesResult` returned for the same file; ``utc_day`` is the
    run's UTC day, used to decide whether the hint's offsets are still stable. A
    hint only ever saves requests — a stale or wrong one is detected by the rows
    it fails to yield and costs a fresh look, never a prefix or the whole file
    (unless upstream really lacks the rows). A level that would need more than
    ``request_cap`` requests is skipped for the next one (ADR-0008: 96).
    ``limiter`` is the shared :class:`asyncio.Semaphore` that caps how many
    requests of one refresh are in flight across all its files at once (issue
    #132); ``None`` leaves this fetch unbounded.
    """
    reader = _CountingReader(
        AiohttpRangeReader(session, url, limiter=limiter), request_cap
    )
    return await _fetch_series(
        reader,
        point,
        window_start=window_start,
        window_end=window_end,
        hint=hint,
        utc_day=utc_day,
        step=step,
    )


# ---------------------------------------------------------------------------
# Windowed fetch: a long horizon split into cap-sized row-addressed windows
# ---------------------------------------------------------------------------
#
# A single ``fetch_series`` skips row addressing once the demanded window needs
# more than ``SERIES_REQUEST_CAP`` requests and falls to the multi-MB prefix
# (level 3): with the canary reporting a change on practically every run, a
# horizon of ~80 h or more then reads a prefix every hour (issue #143). The far
# remainder of such a horizon is instead fetched here as **consecutive windows
# that each fit the cap**, so every window stays on row addressing (level ≤ 2)
# and the prefix/full file remain only the per-window fallback. Each window is
# its own ``fetch_series`` call with its own request budget; the hint learned
# from one window feeds the next so successive windows re-anchor cheaply.


def _split_window(
    window_start: datetime,
    window_end: datetime,
    step: timedelta,
    request_cap: int | None,
) -> list[tuple[datetime, datetime]]:
    """Tile ``[window_start, window_end)`` into windows that each fit the cap.

    Each window spans at most ``request_cap - _ADDRESSING_OVERHEAD_REQUESTS - 1``
    steps, so its demanded block count plus the addressing overhead stays within
    ``request_cap`` and row addressing is never skipped for it (see ``_address``).
    ``None`` cap (no limit) yields the whole window in one piece.
    """
    if request_cap is None or window_end <= window_start:
        return [(window_start, window_end)]
    max_span_steps = max(1, request_cap - _ADDRESSING_OVERHEAD_REQUESTS - 1)
    span = step * max_span_steps
    windows: list[tuple[datetime, datetime]] = []
    cursor = window_start
    while cursor < window_end:
        end = min(cursor + span, window_end)
        windows.append((cursor, end))
        cursor = end
    return windows


async def _resolve_run_end(
    make_reader: Callable[[], _CountingReader],
    hint: FileHint | None,
    utc_day: date | None,
    step: timedelta,
) -> datetime | None:
    """The stamp one ``step`` past the file's last block, for an open-ended window.

    A full-run far remainder is ``[near_end, end of run]`` and the run end is not
    known without the file. A same-day hint carries the file's ``last_stamp``; if
    it does not, one cheap probe reads the last row. ``None`` when neither yields
    a stamp, so the caller falls back to a single whole-run fetch.
    """
    if hint is not None and hint.last_stamp and _hint_is_current(hint, utc_day):
        last = _dt_from_stamp(hint.last_stamp)
        if last is not None:
            return last + step
    reader = make_reader()
    size = await reader.size()
    last_row = await _read_row_before(reader, size)
    if last_row is None:
        return None
    last = _dt_from_stamp(last_row.date)
    return last + step if last is not None else None


async def _fetch_series_windows(
    make_reader: Callable[[], _CountingReader],
    point: ForecastPoint,
    *,
    window_start: datetime,
    window_end: datetime | None,
    hint: FileHint | None,
    utc_day: date | None,
    step: timedelta = timedelta(hours=1),
    request_cap: int | None = SERIES_REQUEST_CAP,
) -> SeriesResult:
    """Fetch ``[window_start, window_end)`` as cap-sized windows and merge the rows.

    The network-free core of :func:`fetch_series_windows`: ``make_reader`` returns
    a fresh :class:`_CountingReader` (its own request budget) per window. A long
    horizon then stays on row addressing for every window instead of falling to
    the prefix (issue #143). ``window_end`` of ``None`` is the full-run remainder,
    resolved to the file's last block first. The merged text carries the point's
    rows across all windows, deduplicated by ``Date`` and ordered.
    """
    cur_hint = hint
    if window_end is None:
        window_end = await _resolve_run_end(make_reader, cur_hint, utc_day, step)
        if window_end is None:
            # The file's extent could not be learned cheaply: one whole-run fetch
            # (the ladder climbs as it sees fit) rather than guessing a window.
            reader = make_reader()
            return await _fetch_series(
                reader,
                point,
                window_start=None,
                window_end=None,
                hint=cur_hint,
                utc_day=utc_day,
                step=step,
            )

    header = ""
    rows: dict[str, str] = {}
    level = 0
    requests = 0
    bytes_read = 0
    layout = FileLayout.FALLBACK
    for ws, we in _split_window(window_start, window_end, step, request_cap):
        reader = make_reader()
        result = await _fetch_series(
            reader,
            point,
            window_start=ws,
            window_end=we,
            hint=cur_hint,
            utc_day=utc_day,
            step=step,
        )
        level = max(level, result.level)
        requests += result.requests
        bytes_read += result.bytes
        layout = result.layout
        if result.hint is not None:
            cur_hint = result.hint
        lines = result.text.split("\n")
        if lines:
            header = lines[0]
        for line in lines[1:]:
            if not line:
                continue
            parts = line.split(";", 3)
            if len(parts) >= 3:
                rows[parts[2]] = line

    ordered = [rows[stamp] for stamp in sorted(rows)]
    text = header + "\n" + ("\n".join(ordered) + "\n" if ordered else "")
    return SeriesResult(
        text=text,
        layout=layout,
        level=level,
        requests=requests,
        bytes=bytes_read,
        whole_run=False,
        hint=cur_hint,
    )


async def fetch_series_windows(
    session: aiohttp.ClientSession,
    url: str,
    point: ForecastPoint,
    *,
    window_start: datetime,
    window_end: datetime | None,
    hint: FileHint | None = None,
    utc_day: date | None = None,
    step: timedelta = timedelta(hours=1),
    request_cap: int | None = SERIES_REQUEST_CAP,
    limiter: asyncio.Semaphore | None = None,
) -> SeriesResult:
    """Fetch ``point``'s rows of ``[window_start, window_end)`` from one file,
    split into consecutive windows that each fit ``request_cap`` (issue #143).

    Every window is row-addressed on its own budget, so a horizon far longer than
    the cap never falls to the multi-MB prefix as a whole — the prefix/full file
    stay only the per-window fallback. ``window_end`` of ``None`` means the rest
    of the run. The returned :class:`SeriesResult` carries the merged rows, the
    highest level any window needed, the summed requests/bytes and the hint the
    last window learned (to persist for the next run of the same UTC day).
    """

    def make_reader() -> _CountingReader:
        return _CountingReader(
            AiohttpRangeReader(session, url, limiter=limiter), request_cap
        )

    return await _fetch_series_windows(
        make_reader,
        point,
        window_start=window_start,
        window_end=window_end,
        hint=hint,
        utc_day=utc_day,
        step=step,
        request_cap=request_cap,
    )


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
