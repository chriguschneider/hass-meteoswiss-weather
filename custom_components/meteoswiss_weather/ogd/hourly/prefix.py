"""Date-major files: the prefix that covers the horizon (ladder level 3)."""

from __future__ import annotations

from datetime import datetime
from math import ceil

from ..const import FORECAST_ENCODING, HOURLY_BYTES_PER_HOUR, HOURLY_RANGE_SAFETY
from ..parse import parse_stamp
from .reader import RangeReader, _parse_row, _read_row_after

# Extra hours added to the date-major prefix budget so the horizon is reached
# even when the per-hour block runs large; the prefix is extended if not.
_HORIZON_MARGIN_HOURS = 6


# ---------------------------------------------------------------------------
# Date-major: horizon prefix
# ---------------------------------------------------------------------------


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
    return parse_stamp(row.date) if row is not None else None


async def _fetch_date_major(
    reader: RangeReader, horizon_end: datetime | None
) -> str:
    """Fetch the prefix that covers ``horizon_end`` (or the whole file)."""
    size = await reader.size()
    if horizon_end is None:
        return (await reader.read_all()).decode(FORECAST_ENCODING)

    first = await _read_row_after(reader, 0)
    start_dt = parse_stamp(first.date) if first is not None else None
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
