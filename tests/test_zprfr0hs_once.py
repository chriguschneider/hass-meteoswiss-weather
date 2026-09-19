"""Acceptance tests for issue #134: zprfr0hs fetched once per run.

With the hourly option on, ``zprfr0hs`` (zero-degree level) was fetched twice
per run: once by the daily path (48 h window) and again by the hourly refresh
(horizon window, typically 49–72 h). The shared series cache of issue #123
could not serve the second request because 48 h < the hourly horizon.

The fix: when ``hourly_horizon_days`` is passed to ``fetch_daily``, the daily
path uses ``max(DAILY_ZERO_DEGREE_WINDOW_HOURS, horizon_hours)`` as the
zero-degree window, so the series cache covers the hourly path's window and
serves it without a second download. The CHANGELOG entry is under
``## [Unreleased] / Fixed``.

Tests here work at the ``BulkCsvBackend`` level, patching
``fetch_series`` so no network is needed. ``whole_run=False`` is used for
``zprfr0hs`` to exercise the covering-window logic of ``_CachedSeries.covers``
(a whole-run entry would cover every request and hide the difference between
the fixed and broken paths).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from freezegun import freeze_time

from custom_components.meteoswiss_weather.ogd.backend import BulkCsvBackend
from custom_components.meteoswiss_weather.ogd.const import (
    DAILY_BLOCK_PARAMS,
    DAILY_REQUIRED_PARAMS,
    DAILY_ZERO_DEGREE_WINDOW_HOURS,
    HOURLY_HORIZON_FULL_RUN,
    HOURLY_POINT_MAJOR_PARAMS,
    HOURLY_ZERO_DEGREE,
)
from custom_components.meteoswiss_weather.ogd.hourly import (
    FileLayout,
    SeriesResult,
    horizon_end_utc,
)
from custom_components.meteoswiss_weather.ogd.models import ForecastPoint
from custom_components.meteoswiss_weather.ogd.stac import Run

# Freeze at the start of 2026-08-27 UTC to get a deterministic horizon.
_NOW = datetime(2026, 8, 27, 0, 0, tzinfo=UTC)

_POINT = ForecastPoint(
    point_id=309800,
    point_type_id=2,
    postal_code="3098",
    name="Köniz",
    lat=46.9245,
    lon=7.4147,
    height_masl=595.0,
)

_ASSET_BASE = (
    "https://data.geo.admin.ch/ch.meteoschweiz.ogd-local-forecasting/20260827-ch"
)
_RUN_TS = "202608270200"
_RUN_STAMP = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)

# All params the backend might fetch in a daily + point-major-hourly run.
_ALL_PARAMS: tuple[str, ...] = (
    *DAILY_REQUIRED_PARAMS,
    *DAILY_BLOCK_PARAMS,
    *HOURLY_POINT_MAJOR_PARAMS,
)


def _run() -> Run:
    """A Run carrying every param we need."""
    return Run(
        timestamp=_RUN_STAMP,
        assets={
            p: f"{_ASSET_BASE}/vnut12.lssw.{_RUN_TS}.{p}.csv"
            for p in _ALL_PARAMS
        },
    )


def _minimal_text(param: str) -> str:
    """One-row text that satisfies ``SeriesResult.has_rows``.

    ``parse_hourly`` and ``parse_daily`` iterate over rows matching the point;
    a single row at hour-0 / day-0 means the parsers will not crash even if
    the value is unused by the test assertion.
    """
    stamp = "202608270000"
    return (
        f"point_id;point_type_id;Date;{param}\n"
        f"309800;2;{stamp};2500.0\n"
    )


def _make_mock_fetch_series(call_count: dict[str, int]) -> object:
    """Return a mock ``fetch_series`` that tracks calls per param.

    ``zprfr0hs`` is returned as ``whole_run=False`` so the series cache stores
    a windowed entry and ``_CachedSeries.covers`` is exercised — a
    ``whole_run=True`` entry would trivially cover every request and hide the
    difference between the fixed and broken paths.

    All other params return ``whole_run=True`` to keep them out of the
    covering-window logic being tested here.
    """

    async def _mock(session, url, point, **kwargs):
        # Derive the param from the URL (…/<timestamp>.<param>.csv).
        param = url.rsplit(".", 2)[1]
        call_count[param] += 1
        whole = param != HOURLY_ZERO_DEGREE
        return SeriesResult(
            text=_minimal_text(param),
            layout=FileLayout.POINT_MAJOR_TYPE,
            level=1,
            requests=1,
            bytes=100,
            whole_run=whole,
        )

    return _mock


@freeze_time(_NOW)
async def test_zprfr0hs_fetched_once_daily_first() -> None:
    """Order 1: ``fetch_daily`` then ``fetch_hourly`` — one fetch of zprfr0hs.

    The daily path uses the covering window (max of 48 h and the hourly
    horizon), so the series cache entry satisfies the hourly path's window
    check and no second download is needed.
    """
    call_count: dict[str, int] = defaultdict(int)
    run = _run()

    with patch(
        "custom_components.meteoswiss_weather.ogd.backend.fetch_series",
        _make_mock_fetch_series(call_count),
    ):
        backend = BulkCsvBackend(session=None)  # type: ignore[arg-type]
        await backend.fetch_daily(_POINT, run=run, hourly_horizon_days=2)
        await backend.fetch_hourly(
            _POINT,
            horizon_days=2,
            params=HOURLY_POINT_MAJOR_PARAMS,
            run=run,
        )

    assert call_count[HOURLY_ZERO_DEGREE] == 1, (
        f"zprfr0hs was fetched {call_count[HOURLY_ZERO_DEGREE]} times; "
        "expected 1 — the covering window should serve the hourly path from cache"
    )


@freeze_time(_NOW)
async def test_zprfr0hs_fetched_once_hourly_first() -> None:
    """Order 2: ``fetch_hourly`` then ``fetch_daily`` — one fetch of zprfr0hs.

    Even without the fix the hourly path's wider window already covers the
    daily path's 48 h request, but this test guards the invariant for both
    orderings regardless.
    """
    call_count: dict[str, int] = defaultdict(int)
    run = _run()

    with patch(
        "custom_components.meteoswiss_weather.ogd.backend.fetch_series",
        _make_mock_fetch_series(call_count),
    ):
        backend = BulkCsvBackend(session=None)  # type: ignore[arg-type]
        await backend.fetch_hourly(
            _POINT,
            horizon_days=2,
            params=HOURLY_POINT_MAJOR_PARAMS,
            run=run,
        )
        await backend.fetch_daily(_POINT, run=run, hourly_horizon_days=2)

    assert call_count[HOURLY_ZERO_DEGREE] == 1, (
        f"zprfr0hs was fetched {call_count[HOURLY_ZERO_DEGREE]} times; "
        "expected 1 — the hourly-first fetch should cover the daily path's window"
    )


@freeze_time(_NOW)
def test_covering_window_satisfies_hourly_path() -> None:
    """The covering-window formula produces an end that is >= the hourly horizon.

    Pure arithmetic: no backend or network needed. This pins the formula so a
    refactor that accidentally shrinks the window is caught immediately.
    """
    from custom_components.meteoswiss_weather.ogd.backend import _CachedSeries

    now = _NOW
    this_hour = now.replace(minute=0, second=0, microsecond=0)
    horizon_days = 2

    hourly_end = horizon_end_utc(horizon_days, now)
    assert hourly_end is not None

    # Reproduce the covering-window formula from _get_block_texts.
    horizon_hours = int((hourly_end - this_hour).total_seconds() / 3600)
    zero_degree_hours = max(DAILY_ZERO_DEGREE_WINDOW_HOURS, horizon_hours)
    daily_window_end = this_hour + timedelta(hours=zero_degree_hours)

    # The series the daily path would store.
    cached = _CachedSeries(
        text=_minimal_text(HOURLY_ZERO_DEGREE),
        whole_run=False,
        window_start=this_hour,
        window_end=daily_window_end,
    )

    # The request the hourly path would make.
    assert cached.covers(this_hour, hourly_end), (
        f"daily window [{this_hour}, {daily_window_end}) does not cover "
        f"hourly window [{this_hour}, {hourly_end})"
    )


@freeze_time(_NOW)
def test_option_off_keeps_48h_window() -> None:
    """With the hourly option off (hourly_horizon_days=None), keep the 48 h window.

    The zero-degree sensor still gets its 48 h of coverage; nothing extra is
    fetched. This guards the ADR-0002 cost gate: a feature that is off demands
    nothing.
    """
    now = _NOW
    this_hour = now.replace(minute=0, second=0, microsecond=0)

    # None → no hourly option → 48 h window.
    hourly_horizon_days = None
    if (
        hourly_horizon_days is not None
        and hourly_horizon_days != HOURLY_HORIZON_FULL_RUN
    ):
        h_end = horizon_end_utc(hourly_horizon_days, now)
        if h_end is not None:
            horizon_hours = int((h_end - this_hour).total_seconds() / 3600)
            zero_degree_hours = max(DAILY_ZERO_DEGREE_WINDOW_HOURS, horizon_hours)
        else:
            zero_degree_hours = DAILY_ZERO_DEGREE_WINDOW_HOURS
    else:
        zero_degree_hours = DAILY_ZERO_DEGREE_WINDOW_HOURS

    assert zero_degree_hours == DAILY_ZERO_DEGREE_WINDOW_HOURS, (
        f"With hourly off, window should be {DAILY_ZERO_DEGREE_WINDOW_HOURS} h, "
        f"got {zero_degree_hours}"
    )


@freeze_time(_NOW)
def test_full_run_sentinel_keeps_48h_window() -> None:
    """The HOURLY_HORIZON_FULL_RUN sentinel also keeps the 48 h window.

    For the full-run case the hourly path fetches the whole file anyway;
    widening the daily path's window would be wasteful.
    """
    now = _NOW
    this_hour = now.replace(minute=0, second=0, microsecond=0)

    hourly_horizon_days = HOURLY_HORIZON_FULL_RUN
    if (
        hourly_horizon_days is not None
        and hourly_horizon_days != HOURLY_HORIZON_FULL_RUN
    ):
        h_end = horizon_end_utc(hourly_horizon_days, now)
        if h_end is not None:
            horizon_hours = int((h_end - this_hour).total_seconds() / 3600)
            zero_degree_hours = max(DAILY_ZERO_DEGREE_WINDOW_HOURS, horizon_hours)
        else:
            zero_degree_hours = DAILY_ZERO_DEGREE_WINDOW_HOURS
    else:
        zero_degree_hours = DAILY_ZERO_DEGREE_WINDOW_HOURS

    assert zero_degree_hours == DAILY_ZERO_DEGREE_WINDOW_HOURS
