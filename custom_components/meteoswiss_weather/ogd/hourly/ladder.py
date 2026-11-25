"""The escalation ladder for one read of one file (ADR-0008 section 4)."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from ..const import FORECAST_ENCODING
from ..models import FileLayout, ForecastPoint
from .hints import FileHint, SeriesResult, _hint_header_bytes, _hint_is_current
from .layout import classify_layout
from .point_major import _fetch_point_major
from .prefix import _fetch_date_major
from .reader import _CountingReader, _NotProven, _RequestCapExceeded
from .rows import (
    _ADDRESSING_OVERHEAD_REQUESTS,
    _fetch_rows_date_major,
    _filter_point_rows,
    _point_stamps,
    _window_complete,
)

_LOGGER = logging.getLogger(__name__)


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


class _ChunkNeeded(Exception):
    """A date-major demand overran the cap; retry it as cap-sized windows.

    Raised by the single-shot ladder (:func:`_fetch_series`) when a date-major
    file cannot be row-addressed in one read under the request cap — a long
    windowed horizon, or a whole run with too many blocks. The caller
    (:func:`_fetch_series_auto`) then tiles the resolved window into consecutive
    cap-sized windows and row-addresses each, instead of falling to a prefix or
    the whole file. It is the ladder call, not the static group a file was
    listed in, that decides about chunking, so ``zprfr0hs`` re-sorted to
    date-major upstream is chunked wherever it is demanded.
    """

    def __init__(self, window_start: datetime, window_end: datetime) -> None:
        super().__init__("date-major demand exceeds the request cap; chunk it")
        self.window_start = window_start
        self.window_end = window_end


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
        # A whole-run demand has no window to check against, so the one stray
        # row of a misread date-major file is caught by its count instead:
        # every forecast file spans several steps per point.
        ordered = bool(stamps) and stamps == sorted(set(stamps))
        proven = ordered and (
            _window_complete(text, window_start, window_end, step)
            if windowed
            else len(stamps) > 1
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
                raise _RequestCapExceeded(
                    "window too long for row addressing",
                    window=(window_start, window_end),
                )
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


async def _fetch_series(
    reader: _CountingReader,
    point: ForecastPoint,
    *,
    window_start: datetime | None,
    window_end: datetime | None,
    hint: FileHint | None,
    utc_day: date | None,
    step: timedelta = timedelta(hours=1),
    allow_chunk: bool = False,
) -> SeriesResult:
    """Climb the ladder over ``reader`` (the network-free core of fetch_series).

    With ``allow_chunk`` a date-major demand that cannot be row-addressed under
    the request cap raises :class:`_ChunkNeeded` (carrying the resolved window)
    instead of falling to a prefix or the whole file, so :func:`_fetch_series_auto`
    can tile it into cap-sized windows (issue #153). Left ``False`` — the default,
    and what the per-window fetches use — the cap escalates to prefix/full as
    before, which keeps the single-shot ladder's own behaviour unchanged.
    """
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
    except _RequestCapExceeded as err:
        # A date-major file whose demand overran the cap is not escalated to a
        # prefix/whole file here: it is chunked into cap-sized row-addressed
        # windows by the caller (issue #153). Any other over-cap case (or when
        # chunking is disabled, for the per-window fetches) escalates as before.
        if allow_chunk and layout is FileLayout.DATE_MAJOR and err.window is not None:
            raise _ChunkNeeded(err.window[0], err.window[1]) from err
        _LOGGER.debug("series fetch escalates past addressing: %s", err)
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
