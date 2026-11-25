"""Point-major files: binary search for the point's contiguous block."""

from __future__ import annotations

from ..const import FORECAST_ENCODING, HOURLY_BLOCK_CHUNK_BYTES
from ..models import FileLayout, ForecastPoint
from .layout import _row_key, _target_key
from .reader import (
    RangeReader,
    _parse_row,
    _read_header,
    _read_row_after,
    _read_row_before,
)

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
