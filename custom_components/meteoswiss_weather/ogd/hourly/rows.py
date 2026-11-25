"""Date-major row addressing: a point's row in each block, found directly."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..const import FORECAST_ENCODING, HOURLY_ROW_PROBE_BYTES
from ..models import ForecastPoint
from ..parse import parse_stamp
from .hints import FileHint, RowGeometry
from .reader import (
    RangeReader,
    _NotProven,
    _parse_row,
    _read_header,
    _read_row_after,
    _read_row_before,
    _RequestCapExceeded,
    _Row,
)

# Row-addressing windows around a predicted row position: L1 tries these in
# turn; L2 then reads a few whole hour blocks around the prediction.
_ROW_WINDOWS = (2048, 16384)
_BLOCK_WINDOW_BLOCKS = 2.5
# Chunk size while learning a date-major file's geometry from its first block.
_LEARN_CHUNK_BYTES = 65_536
# Requests on top of one read per demanded hour, when deciding whether row
# addressing fits the cap (and how long a chunk may be): a fresh look costs
# about 15 (nine classification probes, header, first/last row, ~3 reads to
# learn the geometry); a same-day hint that fails to verify costs at most 2
# more before that fresh look (only the row windows are tried for its first
# row); and 6 are slack for rows that need a wider window. With 16, a full
# 79-hour chunk had one spare request, so a stale hint plus the fresh look
# overran the cap and every refresh fell to the 30 MB prefix (issue #281).
# 23 still lets a 72 h near window (73 blocks) stay one read under the cap.
_ADDRESSING_OVERHEAD_REQUESTS = 23


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


@dataclass(frozen=True, slots=True)
class _Extent:
    """A date-major file's header and its first/last block, read or remembered."""

    header: bytes
    first_start: int  # byte offset of the file's first data row
    first_stamp: str
    last_stamp: str
    first_dt: datetime
    last_dt: datetime


async def _read_extent(reader: RangeReader, hint: FileHint | None) -> _Extent:
    """The header and the file's first/last stamp, from a same-day hint if it has them.

    Same-day offsets are stable, so a hint with the header and both stamps skips
    the three probes; otherwise they are read.
    """
    if hint is not None and hint.header and hint.first_stamp and hint.last_stamp:
        header = hint.header.encode(FORECAST_ENCODING)
        first_start = len(header)
        first_stamp, last_stamp = hint.first_stamp, hint.last_stamp
        first_dt = parse_stamp(first_stamp)
        last_dt = parse_stamp(last_stamp)
    else:
        header = await _read_header(reader)
        size = await reader.size()
        first = await _read_row_after(reader, 0)
        last = await _read_row_before(reader, size)
        first_dt = parse_stamp(first.date) if first is not None else None
        last_dt = parse_stamp(last.date) if last is not None else None
        if first is None or last is None or first_dt is None or last_dt is None:
            raise _NotProven("could not read the file's first/last row")
        first_start = first.start
        first_stamp, last_stamp = first.date, last.date

    if first_dt is None or last_dt is None:
        raise _NotProven("could not read the file's first/last stamp")
    return _Extent(header, first_start, first_stamp, last_stamp, first_dt, last_dt)


def _plan_stamps(
    extent: _Extent,
    window_start: datetime | None,
    window_end: datetime | None,
    step: timedelta,
    request_cap: int | None,
) -> list[datetime]:
    """The block stamps to address; raises when they would overrun the cap."""
    # A whole-run demand (window None) is served by addressing every block from
    # the file's first to its last, so a few-block date-major file (the daily
    # p-variants) is not downloaded whole.
    ws = window_start if window_start is not None else extent.first_dt
    we = window_end if window_end is not None else extent.last_dt + step
    hours = _window_hours(ws, we, extent.first_dt, extent.last_dt, step)
    # Decide by the number of blocks, not the window length alone: a run whose
    # block count would overrun the request cap climbs instead of storming the
    # origin (ADR-0008 section 4).
    if request_cap is not None and len(hours) + _ADDRESSING_OVERHEAD_REQUESTS > (
        request_cap
    ):
        # Hand back the resolved run window so the caller can re-fetch it as
        # cap-sized row-addressed windows rather than the whole file.
        raise _RequestCapExceeded(
            "run has too many blocks for row addressing", window=(ws, we)
        )
    return hours


@dataclass(slots=True)
class _Walk:
    """The running prediction while walking the blocks, re-anchored on each row."""

    first_dt: datetime
    step: timedelta
    block: float  # running estimate of one block's size in bytes
    anchor_idx: int  # block index of the last row found
    anchor_off: int  # byte offset of the last row found
    level: int
    from_window: int = 0  # the smallest _ROW_WINDOWS entry worth trying next
    first_found: tuple[datetime, int] | None = None
    last_found: tuple[datetime, int] | None = None

    def predict(self, when: datetime) -> tuple[int, float]:
        """The block index of ``when`` and the predicted offset of its row."""
        idx = int((when - self.first_dt) / self.step)
        return idx, self.anchor_off + (idx - self.anchor_idx) * self.block

    def accept(
        self, when: datetime, idx: int, predicted: float, offset: int, hit_window: int
    ) -> None:
        """Re-anchor on a row found at ``offset`` and adapt the next search."""
        if hit_window > 0:
            self.level = max(self.level, 1)
        # Adapt the next row's starting window to the drift just observed: when
        # the prediction lands well inside the small window keep using it (a few
        # bytes per hour); when the row keeps sitting outside it, start wider so
        # a drifting file does not pay a miss on every hour.
        error = abs(offset - int(predicted))
        self.from_window = 0 if 2 * error < _ROW_WINDOWS[0] else 1
        if idx != self.anchor_idx:
            self.block = (
                self.block + (offset - self.anchor_off) / (idx - self.anchor_idx)
            ) / 2
        self.anchor_idx, self.anchor_off = idx, offset
        if self.first_found is None:
            self.first_found = (when, offset)
        self.last_found = (when, offset)

    def learned(self, geometry: RowGeometry) -> RowGeometry:
        """The geometry to remember: mean block size, first and last row found."""
        block = self.block
        first, last = self.first_found, self.last_found
        if first is not None and last is not None and last[0] > first[0]:
            # The mean over the whole span walked, not the running estimate of
            # the last few blocks: it is what extrapolates best next time.
            block = (last[1] - first[1]) / ((last[0] - first[0]) / self.step)
        return RowGeometry(
            block_bytes=block,
            row_offset=geometry.row_offset,
            anchor_stamp=_stamp(first[0]) if first else None,
            anchor_offset=first[1] if first else None,
            end_stamp=_stamp(last[0]) if last else None,
            end_offset=last[1] if last else None,
        )


def _known_rows(
    geometry: RowGeometry, extent: _Extent, step: timedelta
) -> dict[int, int]:
    """Block index -> byte offset of every row of the point the geometry knows.

    Always the first block's row; plus the first and last row found on the last
    same-day fetch, when they fall inside this file on a whole step.
    """
    known = {0: extent.first_start + geometry.row_offset}
    for stamp, offset in (
        (geometry.anchor_stamp, geometry.anchor_offset),
        (geometry.end_stamp, geometry.end_offset),
    ):
        when = parse_stamp(stamp) if stamp else None
        if when is None or offset is None or when < extent.first_dt:
            continue
        steps = (when - extent.first_dt) / step
        if steps == int(steps):
            known[int(steps)] = offset
    return known


def _start_walk(
    geometry: RowGeometry,
    extent: _Extent,
    step: timedelta,
    level: int,
    first_target: datetime,
) -> _Walk:
    """Seed the prediction for ``first_target`` from the rows the geometry knows.

    Offsets are stable across a UTC day's runs, so the first row is predicted
    from the known rows around it: interpolated between the nearest known row
    before and after it, else extrapolated from the nearest one with the mean
    block size. Extrapolating a running block estimate across dozens of blocks
    instead misses by tens of KB once block sizes vary with the value widths
    (``zprfr0hs`` in winter, issue #281). The rows found verify it either way.
    """
    known = _known_rows(geometry, extent, step)
    target = int((first_target - extent.first_dt) / step)
    below = max((idx for idx in known if idx <= target), default=0)
    above = min((idx for idx in known if idx > target), default=None)
    block = geometry.block_bytes
    if above is not None:
        block = (known[above] - known[below]) / (above - below)
    return _Walk(
        first_dt=extent.first_dt,
        step=step,
        block=block,
        anchor_idx=below,
        anchor_off=known[below],
        level=level,
    )


async def _locate_row(
    reader: RangeReader,
    walk: _Walk,
    when: datetime,
    predicted: float,
    needle: bytes,
    *,
    verifying_hint: bool = False,
) -> tuple[int, bytes, int]:
    """Find the row near ``predicted``, widening to whole blocks (L2) on a miss.

    ``verifying_hint`` marks the first row predicted from a remembered geometry:
    it must sit inside the row windows, or the hint is not trusted and the
    ladder takes one fresh look. Searching whole blocks for it instead would
    spend the requests that fresh look needs, and a hint that far off would
    keep its wrong row offset for the next fetch (issue #281).
    """
    found = await _find_row(
        reader, predicted, needle, _ROW_WINDOWS, from_window=walk.from_window
    )
    if found is None and verifying_hint:
        raise _NotProven(f"hint did not verify at {_stamp(when)}")
    if found is None:
        walk.level = 2
        found = await _find_row(
            reader, predicted, needle, (int(walk.block * _BLOCK_WINDOW_BLOCKS),)
        )
        if found is not None:
            found = (found[0], found[1], len(_ROW_WINDOWS) - 1)
    if found is None:
        raise _NotProven(f"row for {_stamp(when)} not found by addressing")
    return found


def _rows_result(
    extent: _Extent, text: bytes, level: int, geometry: RowGeometry
) -> _DateMajorRows:
    return _DateMajorRows(
        text=text.decode(FORECAST_ENCODING),
        level=level,
        geometry=geometry,
        header=extent.header.decode(FORECAST_ENCODING),
        first_stamp=extent.first_stamp,
        last_stamp=extent.last_stamp,
    )


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
    extent = await _read_extent(reader, hint)
    hours = _plan_stamps(extent, window_start, window_end, step, request_cap)
    if not hours:
        return _rows_result(
            extent,
            extent.header,
            0,
            geometry or RowGeometry(block_bytes=0.0, row_offset=0),
        )

    hinted = geometry is not None
    level = 0 if hinted else 1
    if geometry is None:
        first_row = _Row(
            extent.first_start, point.point_id, point.point_type_id, extent.first_stamp
        )
        geometry = await _learn_geometry(reader, point, first_row)
    walk = _start_walk(geometry, extent, step, level, hours[0])

    lines: list[bytes] = []
    for when in hours:
        idx, predicted = walk.predict(when)
        needle = _needle(point, _stamp(when))
        offset, line, hit_window = await _locate_row(
            reader,
            walk,
            when,
            predicted,
            needle,
            verifying_hint=hinted and walk.first_found is None,
        )
        walk.accept(when, idx, predicted, offset, hit_window)
        lines.append(line)

    text = extent.header + b"\n".join(lines) + b"\n"
    return _rows_result(extent, text, walk.level, walk.learned(geometry))


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
    first_dt, last_dt = parse_stamp(stamps[0]), parse_stamp(stamps[-1])
    if first_dt is None or last_dt is None:
        return False
    have = set(stamps)
    return all(
        _stamp(hour) in have
        for hour in _window_hours(window_start, window_end, first_dt, last_dt, step)
    ) and last_dt + step >= window_end
