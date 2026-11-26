"""Entry points: one file's series, whole or as cap-sized windows."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp

from ..const import FORECAST_TIMEZONE, HOURLY_HORIZON_FULL_RUN, SERIES_REQUEST_CAP
from ..models import FileLayout, ForecastPoint
from ..parse import parse_stamp
from .hints import FileHint, RowGeometry, SeriesResult, _hint_is_current
from .ladder import _ChunkNeeded, _fetch_series
from .reader import AiohttpRangeReader, _CountingReader, _read_row_before
from .rows import _ADDRESSING_OVERHEAD_REQUESTS

# Built once at import time: the first ZoneInfo lookup reads tzdata from disk,
# which must not happen per call on Home Assistant's event loop. Shared with
# ogd/forecast.py.
FORECAST_TZ = ZoneInfo(FORECAST_TIMEZONE)


async def _fetch_series_auto(
    make_reader: Callable[[], _CountingReader],
    point: ForecastPoint,
    *,
    window_start: datetime | None,
    window_end: datetime | None,
    hint: FileHint | None,
    utc_day: date | None,
    step: timedelta,
    request_cap: int | None,
) -> SeriesResult:
    """Run the single-shot ladder; on an over-cap date-major demand, tile it.

    The single-shot :func:`_fetch_series` climbs the ladder for one read. When a
    date-major file cannot be row-addressed under the request cap it raises
    :class:`_ChunkNeeded` with the resolved demand window instead of falling to a
    prefix or the whole file, and this tiles that window into consecutive
    cap-sized windows and row-addresses each (:func:`_fetch_series_windows`,
    issue #153). The initial reader's probe cost is carried into the merged
    result so the diagnostics reflect the whole fetch. A whole-run demand
    (window ``None``) that is chunked still reports ``whole_run`` — every block
    from the file's first to its last stamp was addressed.
    """
    reader = make_reader()
    whole = window_start is None and window_end is None
    try:
        return await _fetch_series(
            reader,
            point,
            window_start=window_start,
            window_end=window_end,
            hint=hint,
            utc_day=utc_day,
            step=step,
            allow_chunk=True,
        )
    except _ChunkNeeded as chunk:
        return await _fetch_series_windows(
            make_reader,
            point,
            window_start=chunk.window_start,
            window_end=chunk.window_end,
            hint=hint,
            utc_day=utc_day,
            step=step,
            request_cap=request_cap,
            whole_run=whole,
            spent=(reader.requests, reader.bytes),
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
    file has few blocks (the nine day blocks of the daily files) and is tiled
    into cap-sized row-addressed windows otherwise. ``hint`` is the :class:`FileHint` a
    previous :class:`SeriesResult` returned for the same file; ``utc_day`` is the
    run's UTC day, used to decide whether the hint's offsets are still stable. A
    hint only ever saves requests — a stale or wrong one is detected by the rows
    it fails to yield and costs a fresh look, never a prefix or the whole file
    (unless upstream really lacks the rows). A level that would need more than
    ``request_cap`` requests is skipped for the next one (ADR-0008: 96).
    ``limiter`` is the shared :class:`asyncio.Semaphore` that caps how many
    requests of one refresh are in flight across all its files at once (issue
    #132); ``None`` leaves this fetch unbounded.

    A date-major file whose demand does not fit the request cap (a long horizon,
    or the whole run when it has too many blocks) is served by row addressing in
    consecutive cap-sized windows rather than a prefix or the whole file — the
    ladder call decides this from the file's detected layout, so a file that
    upstream re-sorted to date-major is handled correctly whatever static group
    it was listed in.
    """

    def make_reader() -> _CountingReader:
        return _CountingReader(
            AiohttpRangeReader(session, url, limiter=limiter), request_cap
        )

    return await _fetch_series_auto(
        make_reader,
        point,
        window_start=window_start,
        window_end=window_end,
        hint=hint,
        utc_day=utc_day,
        step=step,
        request_cap=request_cap,
    )


# ---------------------------------------------------------------------------
# Windowed fetch: a long horizon split into cap-sized row-addressed windows
# ---------------------------------------------------------------------------
#
# A long horizon is fetched as consecutive windows that each fit
# ``SERIES_REQUEST_CAP``, so each stays row-addressed (level ≤ 2); the prefix
# is only the per-window fallback. Each window is its own ``fetch_series`` call
# with its own request budget; the hint learned from one window feeds the next.


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
        last = parse_stamp(hint.last_stamp)
        if last is not None:
            return last + step
    reader = make_reader()
    size = await reader.size()
    last_row = await _read_row_before(reader, size)
    if last_row is None:
        return None
    last = parse_stamp(last_row.date)
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
    whole_run: bool = False,
    spent: tuple[int, int] = (0, 0),
) -> SeriesResult:
    """Fetch ``[window_start, window_end)`` as cap-sized windows and merge the rows.

    The network-free core of :func:`fetch_series_windows`: ``make_reader`` returns
    a fresh :class:`_CountingReader` (its own request budget) per window. A long
    horizon then stays on row addressing for every window instead of falling to
    the prefix (issue #143). ``window_end`` of ``None`` is the full-run remainder,
    resolved to the file's last block first. The merged text carries the point's
    rows across all windows, deduplicated by ``Date`` and ordered.

    ``whole_run`` marks the merged result as covering the point's whole run (set
    when :func:`_fetch_series_auto` chunks a whole-run demand, issue #153), so the
    shared per-run cache can reuse it for any request. ``spent`` is the
    ``(requests, bytes)`` an earlier probe already cost (the ladder read that
    detected the over-cap layout), added to the totals so diagnostics are honest.
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
    first_geometry: RowGeometry | None = None
    level = 0
    requests = spent[0]
    bytes_read = spent[1]
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

        if first_geometry is None and result.hint is not None:
            first_geometry = result.hint.geometry

    ordered = [rows[stamp] for stamp in sorted(rows)]
    text = header + "\n" + ("\n".join(ordered) + "\n" if ordered else "")
    return SeriesResult(
        text=text,
        layout=layout,
        level=level,
        requests=requests,
        bytes=bytes_read,
        whole_run=whole_run,
        hint=_span_hint(cur_hint, first_geometry, step),
    )


def _span_hint(
    last: FileHint | None, first: RowGeometry | None, step: timedelta
) -> FileHint | None:
    """The last window's hint, re-anchored on the first window's first row.

    Each window hands the next one its own hint, whose last row sits right before
    the next window. What is remembered for the next refresh, though, must know
    a row near the *start* of the demand (offsets are stable across a UTC day's
    runs), or the first window of the next refresh extrapolates across the whole
    span and misses (issue #281). So the merged hint keeps the first window's
    first row as its anchor, the last window's last row as its end, and the mean
    block size between the two.
    """
    geometry = last.geometry if last is not None else None
    if (
        last is None
        or geometry is None
        or first is None
        or first.anchor_stamp is None
        or first.anchor_offset is None
    ):
        return last
    block = geometry.block_bytes
    start_dt = parse_stamp(first.anchor_stamp)
    end_dt = parse_stamp(geometry.end_stamp) if geometry.end_stamp else None
    if (
        start_dt is not None
        and end_dt is not None
        and geometry.end_offset is not None
        and end_dt > start_dt
    ):
        block = (geometry.end_offset - first.anchor_offset) / (
            (end_dt - start_dt) / step
        )
    return replace(
        last,
        geometry=replace(
            geometry,
            block_bytes=block,
            anchor_stamp=first.anchor_stamp,
            anchor_offset=first.anchor_offset,
        ),
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
    now_local = now.astimezone(FORECAST_TZ)
    start_of_today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_of_today + timedelta(days=horizon_days + 1)
    return end_local.astimezone(UTC)
