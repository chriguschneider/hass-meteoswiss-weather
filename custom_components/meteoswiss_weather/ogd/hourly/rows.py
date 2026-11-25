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
# Classification, header, first/last row and geometry learning, on top of one
# read per demanded hour, when deciding whether row addressing fits the cap.
_ADDRESSING_OVERHEAD_REQUESTS = 16

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
        first_dt = parse_stamp(first_stamp)
        last_dt = parse_stamp(last_stamp)
    else:
        header = await _read_header(reader)
        size = await reader.size()
        first = await _read_row_after(reader, 0)
        last = await _read_row_before(reader, size)
        first_dt = parse_stamp(first.date) if first is not None else None
        last_dt = parse_stamp(last.date) if last is not None else None
        if first is None or first_dt is None or last_dt is None:
            raise _NotProven("could not read the file's first/last row")
        first_start = first.start
        first_stamp, last_stamp = first.date, last.date

    if first_dt is None or last_dt is None:
        raise _NotProven("could not read the file's first/last stamp")

    # A whole-run demand (window None) is served by addressing every block from
    # the file's first to its last, so a few-block date-major file (the daily
    # p-variants) is not downloaded whole.
    ws = window_start if window_start is not None else first_dt
    we = window_end if window_end is not None else last_dt + step
    hours = _window_hours(ws, we, first_dt, last_dt, step)
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
        parse_stamp(geometry.anchor_stamp) if geometry.anchor_stamp else None
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
    first_dt, last_dt = parse_stamp(stamps[0]), parse_stamp(stamps[-1])
    if first_dt is None or last_dt is None:
        return False
    have = set(stamps)
    return all(
        _stamp(hour) in have
        for hour in _window_hours(window_start, window_end, first_dt, last_dt, step)
    ) and last_dt + step >= window_end
