"""Per-file HTTP-Range strategies for the hourly bulk forecast.

The hourly local-forecast files are ~30 MB and hold every one of the ~5,600
points (docs/ogd.md §E4). Downloading the full set every refresh is the whole
traffic budget (ADR-0002). Measured on 2026-08-28, the files have **two**
layouts, and each admits a cheaper Range fetch:

- **date-major** files (`tre200h0`, the `treq*` group, `zprfr0hs`) are sorted
  by `Date`, one block of all points per hour, so a point's row in each hour
  block is addressed directly (row addressing); a prefix
  ``Range: bytes=0-<budget>`` covering the horizon is the fallback;
- **point-major** files (symbol, precipitation, wind, gust, direction, and —
  since a silent upstream re-sort on 2026-08-31 — the `npro*`
  cloud group) are sorted so one point's ~220 rows form a contiguous ~5 KB
  block — a binary search over byte offsets with tiny Range probes locates
  it, then one Range GET fetches it.

The layout is **detected at runtime** (byte-offset probes), never hard-coded;
anything unrecognised falls back to the full download. All of this is pure
Python over a small :class:`RangeReader` seam (ADR-0001), so the strategies are
unit-tested against an in-memory reader with no network.

Package layout (issue #259):

- ``reader``: the :class:`RangeReader` seam, row helpers, the counting reader;
- ``layout``: runtime layout classification;
- ``point_major``: the binary-searched point block;
- ``prefix``: the date-major horizon prefix;
- ``hints``: :class:`FileHint`, :class:`RowGeometry`, :class:`SeriesResult`;
- ``rows``: date-major row addressing;
- ``ladder``: the escalation ladder for one read;
- ``windows``: the entry points, the cap-sized windows and the horizon.
"""

from __future__ import annotations

from ..models import FileLayout
from .hints import FileHint, RowGeometry, SeriesResult
from .ladder import _fetch_series
from .layout import _LAYOUT_PROBES, classify_layout
from .point_major import _fetch_point_major
from .prefix import _fetch_date_major
from .reader import AiohttpRangeReader, RangeReader, _CountingReader, _read_row_after
from .rows import _ADDRESSING_OVERHEAD_REQUESTS, _filter_point_rows
from .windows import (
    FORECAST_TZ,
    _fetch_series_auto,
    _fetch_series_windows,
    _split_window,
    fetch_series,
    fetch_series_windows,
    horizon_end_utc,
)

__all__ = [
    "FORECAST_TZ",
    "_ADDRESSING_OVERHEAD_REQUESTS",
    "_LAYOUT_PROBES",
    "AiohttpRangeReader",
    "FileHint",
    "FileLayout",
    "RangeReader",
    "RowGeometry",
    "SeriesResult",
    "_CountingReader",
    "_fetch_date_major",
    "_fetch_point_major",
    "_fetch_series",
    "_fetch_series_auto",
    "_fetch_series_windows",
    "_filter_point_rows",
    "_read_row_after",
    "_split_window",
    "classify_layout",
    "fetch_series",
    "fetch_series_windows",
    "horizon_end_utc",
]
