"""Eager tiered hourly refresh (issue #124, ADR-0008; near/far cadence #68).

Two layers are exercised:

- :func:`_tier_due` — the pure refresh decision — is table-tested with no I/O;
- :class:`HourlyRefresher` is driven over frozen time against an in-memory
  recording backend that logs every ``fetch_hourly`` call, so the test asserts
  *which* tier fetched at each step and that the result lands in the store —
  without touching the network. The lazy, card-driven provider it replaced is
  gone (ADR-0008, "Decided by the owner", item 1): the refresher fetches
  whether or not anything subscribes.

The measured facts behind the schedule (docs/ogd.md, "Change rhythm across
runs"): the near term (today + tomorrow) moves at the ICON-CH1 runs
{02,05,08,11,14,17,20,23} UTC, days 2+ at the ICON-CH2 runs {05,11,17,23} UTC,
and six runs a day change nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.meteoswiss_weather.const import (
    HOURLY_FAR_MAX_AGE,
    HOURLY_FAR_RUN_HOURS,
    HOURLY_NEAR_HORIZON_DAYS,
    HOURLY_NEAR_MAX_AGE,
    HOURLY_NEAR_RUN_HOURS,
)
from custom_components.meteoswiss_weather.coordinator import (
    HourlyRefresher,
    _tier_due,
    hourly_from_store,
)
from custom_components.meteoswiss_weather.ogd import DailyBundle, Run
from custom_components.meteoswiss_weather.ogd.const import (
    HOURLY_CLOUD_HIGH,
    HOURLY_CLOUD_LOW,
    HOURLY_CLOUD_MID,
    HOURLY_CLOUD_PARAMS,
    HOURLY_DATE_MAJOR_PARAMS,
    HOURLY_POINT_MAJOR_PARAMS,
    HOURLY_PRECIPITATION,
    HOURLY_SYMBOL,
    HOURLY_TEMP_P10,
    HOURLY_TEMP_P90,
    HOURLY_TEMP_PERCENTILE_PARAMS,
    HOURLY_WIND_SPEED,
    HOURLY_ZERO_DEGREE,
    hourly_date_major_params,
)
from custom_components.meteoswiss_weather.ogd.models import (
    ForecastPoint,
    HourlyForecast,
)
from custom_components.meteoswiss_weather.store import ForecastStore

_HORIZON_DAYS = 2  # the default: today + two full days
_POINT = ForecastPoint(
    point_id=309800,
    point_type_id=2,
    postal_code="3098",
    name="Köniz",
    lat=46.9,
    lon=7.4,
    height_masl=560.0,
)


def _run(stamp: datetime) -> Run:
    """A minimal discovered run for ``stamp`` (assets unused by the fakes)."""
    return Run(timestamp=stamp, assets={})


# ---------------------------------------------------------------------------
# _tier_due: the pure refresh decision
# ---------------------------------------------------------------------------


def _due(**kwargs) -> bool:
    base = {
        "landing_hours": HOURLY_NEAR_RUN_HOURS,
        "max_age": HOURLY_NEAR_MAX_AGE,
    }
    base.update(kwargs)
    return _tier_due(**base)


def test_tier_due_when_never_fetched() -> None:
    """A tier that has never fetched is always due, whatever the run."""
    run = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)  # a non-landing hour
    assert _due(run=run, last_run=None, last_fetch=None, now=run) is True


def test_tier_due_when_older_than_fallback() -> None:
    """Past the staleness fallback a tier is due even on an unchanged run."""
    run = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)  # non-landing, unchanged
    fetched = datetime(2026, 8, 27, 0, 0, tzinfo=UTC)
    now = fetched + HOURLY_NEAR_MAX_AGE
    assert _due(run=run, last_run=run, last_fetch=fetched, now=now) is True


def test_tier_due_on_new_run_at_landing_hour() -> None:
    """A new run whose hour is a landing hour is due within the fallback."""
    last_run = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    run = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)  # 05 UTC: a near landing hour
    fetched = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    now = fetched + timedelta(hours=1)
    assert _due(run=run, last_run=last_run, last_fetch=fetched, now=now) is True


def test_tier_not_due_on_new_run_at_non_landing_hour() -> None:
    """A new run at a non-landing hour, within the fallback, is not due."""
    last_run = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    run = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)  # 03 UTC: changes nothing
    fetched = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    now = fetched + timedelta(hours=1)
    assert _due(run=run, last_run=last_run, last_fetch=fetched, now=now) is False


def test_tier_not_due_on_unchanged_run_within_fallback() -> None:
    """The same run within the fallback never triggers a fetch."""
    run = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)  # even a landing hour
    fetched = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)
    now = fetched + timedelta(hours=1)
    assert _due(run=run, last_run=run, last_fetch=fetched, now=now) is False


def test_far_tier_hours_are_a_subset_of_near_tier_hours() -> None:
    """Every far landing hour is also a near landing hour (far ⇒ near)."""
    assert HOURLY_FAR_RUN_HOURS <= HOURLY_NEAR_RUN_HOURS


# ---------------------------------------------------------------------------
# Refresher scheduling against a recording backend
# ---------------------------------------------------------------------------


class _RecordingBackend:
    """An in-memory backend that records every ``fetch_hourly`` call.

    ``calls`` holds ``(params, horizon_days)`` tuples in call order so a test
    can assert which tier fetched. ``fetch_hourly`` returns the same synthetic
    24-hour run each time; the refresher files each fetched parameter's series
    in the store.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], int]] = []

    async def fetch_daily(self, point, *, run=None):  # pragma: no cover
        return DailyBundle(daily=[])

    async def fetch_hourly(self, point, *, horizon_days=-1, params=(), run=None):
        self.calls.append((tuple(params), horizon_days))
        base = datetime(2026, 8, 27, 0, 0, tzinfo=UTC)
        return [
            HourlyForecast(
                time=base + timedelta(hours=h),
                temperature=float(h),
                precipitation=0.0,
                symbol=1,
                wind_speed_kmh=1.0,
                gust_kmh=2.0,
                wind_bearing=90,
            )
            for h in range(24)
        ]


def _make_refresher(hass, backend, *, horizon_days=_HORIZON_DAYS, **kwargs):
    """A refresher wired to a fresh store; returns ``(refresher, store)``."""
    store = ForecastStore()
    refresher = HourlyRefresher(
        hass, backend, _POINT, store, enabled=True, horizon_days=horizon_days, **kwargs
    )
    return refresher, store


def _tier_of(call: tuple[tuple[str, ...], int]) -> str:
    """Label a recorded fetch as near / far / point-major."""
    params, horizon = call
    if params == tuple(HOURLY_POINT_MAJOR_PARAMS):
        return "point_major"
    if params == tuple(HOURLY_DATE_MAJOR_PARAMS):
        return "near" if horizon == HOURLY_NEAR_HORIZON_DAYS else "far"
    return f"unexpected:{params}:{horizon}"


async def test_refresher_first_call_fetches_far_and_point_major(
    hass: HomeAssistant,
) -> None:
    """The first refresh downloads the far temperature tier and point-major.

    Far is stale (never fetched) so the full-horizon temperature is fetched;
    the near window is a subset of it, so no separate near fetch runs. The
    result is readable from the store.
    """
    backend = _RecordingBackend()
    refresher, store = _make_refresher(hass, backend)
    run = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)  # a near-only landing hour
    with freeze_time(run):
        assert await refresher.async_refresh(_run(run)) is True

    assert [_tier_of(c) for c in backend.calls] == ["far", "point_major"]
    hourly = hourly_from_store(store, refresher.demanded_params)
    assert len(hourly) == 24
    # The store carries both the temperature and the point-major fields.
    assert hourly[5].temperature == 5.0
    assert hourly[5].symbol == 1
    assert hourly[5].wind_speed_kmh == 1.0


async def test_refresher_near_landing_skips_far(hass: HomeAssistant) -> None:
    """A new run at a near-only landing hour refreshes near, not far.

    Point-major refreshes too (every new run), but the far temperature tier is
    left untouched because the run's hour is not a far landing hour and far is
    still within its 6 h fallback.
    """
    backend = _RecordingBackend()
    refresher, _store = _make_refresher(hass, backend)
    start = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)  # 05 UTC: near and far
    with freeze_time(start) as frozen:
        await refresher.async_refresh(_run(start))  # far + point-major
        backend.calls.clear()

        # A new run at 08 UTC (near landing, not far), one hour later.
        frozen.move_to(start + timedelta(hours=1))
        run2 = datetime(2026, 8, 27, 8, 0, tzinfo=UTC)
        await refresher.async_refresh(_run(run2))

    assert [_tier_of(c) for c in backend.calls] == ["near", "point_major"]


async def test_refresher_far_landing_hour_refreshes_far(
    hass: HomeAssistant,
) -> None:
    """A new run at a far landing hour refreshes the far tier (full horizon)."""
    backend = _RecordingBackend()
    refresher, _store = _make_refresher(hass, backend)
    start = datetime(2026, 8, 27, 8, 0, tzinfo=UTC)  # near-only first fetch
    with freeze_time(start) as frozen:
        await refresher.async_refresh(_run(start))  # far (stale) + point-major
        backend.calls.clear()

        # A new run at 11 UTC — a far landing hour — one hour later.
        frozen.move_to(start + timedelta(hours=1))
        far_run = datetime(2026, 8, 27, 11, 0, tzinfo=UTC)
        await refresher.async_refresh(_run(far_run))

    assert [_tier_of(c) for c in backend.calls] == ["far", "point_major"]


async def test_refresher_non_landing_run_fetches_only_point_major(
    hass: HomeAssistant,
) -> None:
    """A new run at an hour that changes nothing refreshes only point-major."""
    backend = _RecordingBackend()
    refresher, _store = _make_refresher(hass, backend)
    start = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)
    with freeze_time(start) as frozen:
        await refresher.async_refresh(_run(start))  # far + point-major
        backend.calls.clear()

        frozen.move_to(start + timedelta(hours=1))
        run2 = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)  # non-landing hour
        await refresher.async_refresh(_run(run2))

    assert [_tier_of(c) for c in backend.calls] == ["point_major"]


async def test_refresher_far_fallback_refetches_without_new_run(
    hass: HomeAssistant,
) -> None:
    """Past the far fallback the far tier refetches even on an unchanged run."""
    backend = _RecordingBackend()
    refresher, _store = _make_refresher(hass, backend)
    start = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)
    with freeze_time(start) as frozen:
        await refresher.async_refresh(_run(start))  # far + point-major
        backend.calls.clear()

        # Same run, but past the 6 h far fallback.
        frozen.move_to(start + HOURLY_FAR_MAX_AGE + timedelta(seconds=1))
        await refresher.async_refresh(_run(start))

    assert [_tier_of(c) for c in backend.calls] == ["far"]


async def test_refresher_near_fallback_refetches_near_only(
    hass: HomeAssistant,
) -> None:
    """Past the near fallback (but within far's) only the near tier refetches."""
    backend = _RecordingBackend()
    refresher, _store = _make_refresher(hass, backend)
    start = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)
    with freeze_time(start) as frozen:
        await refresher.async_refresh(_run(start))  # far + point-major at t0

        # A near-only landing run 2 h later resets the near clock, not far's.
        frozen.move_to(start + timedelta(hours=2))
        near_run = datetime(2026, 8, 27, 8, 0, tzinfo=UTC)
        await refresher.async_refresh(_run(near_run))  # near + point-major
        backend.calls.clear()

        # Now +3 h past the last near fetch (near stale) but only 5 h past the
        # far fetch (far fresh), unchanged run: near refetches alone.
        frozen.move_to(start + timedelta(hours=5, seconds=1))
        await refresher.async_refresh(_run(near_run))

    assert [_tier_of(c) for c in backend.calls] == ["near"]


class _TrimmingBackend:
    """A backend that trims its synthetic run to the requested horizon."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], int]] = []

    async def fetch_daily(self, point, *, run=None):  # pragma: no cover
        return DailyBundle(daily=[])

    async def fetch_hourly(self, point, *, horizon_days=-1, params=(), run=None):
        self.calls.append((tuple(params), horizon_days))
        from custom_components.meteoswiss_weather.ogd.hourly import horizon_end_utc

        base = datetime(2026, 8, 27, 0, 0, tzinfo=UTC)
        end = horizon_end_utc(horizon_days, datetime.now(UTC))
        return [
            HourlyForecast(
                time=base + timedelta(hours=h),
                temperature=float(h),
                precipitation=0.0,
                symbol=1,
                wind_speed_kmh=1.0,
                gust_kmh=2.0,
                wind_bearing=90,
            )
            for h in range(72)
            if end is None or base + timedelta(hours=h) < end
        ]


async def test_near_tier_never_overshoots_configured_horizon(
    hass: HomeAssistant,
) -> None:
    """A near-only refresh must not leak hours past a narrowed horizon.

    With ``horizon_days=0`` (today only) the near tier's default reach (end of
    tomorrow) would otherwise add temperature-only hours with no point-major
    fields, dropped by the required-field gate but shrinking then regrowing the
    forecast as near and far alternate. The near horizon is capped at the
    configured horizon, so the forecast length stays stable.
    """
    backend = _TrimmingBackend()
    refresher, store = _make_refresher(hass, backend, horizon_days=0)
    start = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)  # far landing hour
    with freeze_time(start) as frozen:
        await refresher.async_refresh(_run(start))
        after_far = hourly_from_store(store, refresher.demanded_params)

        # A near-only landing run (08 UTC) an hour later.
        frozen.move_to(start + timedelta(hours=1))
        await refresher.async_refresh(_run(datetime(2026, 8, 27, 8, 0, tzinfo=UTC)))
        after_near = hourly_from_store(store, refresher.demanded_params)

    # No growth and no temperature-only leak past the point-major window.
    assert len(after_near) == len(after_far)
    assert all(h.symbol is not None for h in after_near)
    # The near fetch used the capped horizon, not the default reach of 1.
    near_calls = [c for c in backend.calls if c[0] == tuple(HOURLY_DATE_MAJOR_PARAMS)]
    assert near_calls[-1][1] == 0


async def test_refresher_disabled_never_fetches(hass: HomeAssistant) -> None:
    """With the option off the refresher demands nothing and never fetches."""
    backend = _RecordingBackend()
    store = ForecastStore()
    refresher = HourlyRefresher(
        hass, backend, _POINT, store, enabled=False, horizon_days=_HORIZON_DAYS
    )
    run = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)
    with freeze_time(run):
        assert await refresher.async_refresh(_run(run)) is False
    assert backend.calls == []
    assert refresher.demanded_params == ()


async def test_refresher_none_run_returns_false(hass: HomeAssistant) -> None:
    """No discovered run yet means nothing to fetch."""
    backend = _RecordingBackend()
    refresher, _store = _make_refresher(hass, backend)
    assert await refresher.async_refresh(None) is False
    assert backend.calls == []


# ---------------------------------------------------------------------------
# B9/B11 per-entity gating of the date-major additions (issue #69)
# ---------------------------------------------------------------------------


class _GatedRecordingBackend:
    """Recording backend that fills the gated date-major fields when asked."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], int]] = []

    async def fetch_daily(self, point, *, run=None):  # pragma: no cover
        return DailyBundle(daily=[])

    async def fetch_hourly(self, point, *, horizon_days=-1, params=(), run=None):
        self.calls.append((tuple(params), horizon_days))
        want = set(params)
        base = datetime(2026, 8, 27, 0, 0, tzinfo=UTC)
        return [
            HourlyForecast(
                time=base + timedelta(hours=h),
                temperature=float(h),
                precipitation=0.0,
                symbol=1,
                wind_speed_kmh=1.0,
                gust_kmh=2.0,
                wind_bearing=90,
                cloud_high=20.0 if HOURLY_CLOUD_HIGH in want else None,
                cloud_mid=40.0 if HOURLY_CLOUD_MID in want else None,
                cloud_low=10.0 if HOURLY_CLOUD_LOW in want else None,
                temperature_p10=8.0 if HOURLY_TEMP_P10 in want else None,
                temperature_p90=13.0 if HOURLY_TEMP_P90 in want else None,
            )
            for h in range(24)
        ]


def _date_major_calls(backend) -> list[tuple[str, ...]]:
    """Params of the recorded date-major fetches (not the point-major group)."""
    return [
        params
        for params, _ in backend.calls
        if params != tuple(HOURLY_POINT_MAJOR_PARAMS)
    ]


async def test_refresher_default_fetches_no_gated_files(hass: HomeAssistant) -> None:
    """With neither gated option on, the date-major fetch is temperature only."""
    backend = _GatedRecordingBackend()
    refresher, _store = _make_refresher(hass, backend)
    run = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    with freeze_time(run):
        await refresher.async_refresh(_run(run))

    for params in _date_major_calls(backend):
        assert params == tuple(HOURLY_DATE_MAJOR_PARAMS)
        assert not set(params) & set(HOURLY_CLOUD_PARAMS)
        assert not set(params) & set(HOURLY_TEMP_PERCENTILE_PARAMS)


async def test_refresher_cloud_option_fetches_and_files_layers(
    hass: HomeAssistant,
) -> None:
    """With cloud layers on, the date-major fetch adds the three cloud files."""
    backend = _GatedRecordingBackend()
    refresher, store = _make_refresher(hass, backend, cloud_layers=True)
    run = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    with freeze_time(run):
        await refresher.async_refresh(_run(run))

    expected = hourly_date_major_params(cloud_layers=True)
    for params in _date_major_calls(backend):
        assert params == expected
    # The store carries the cloud fields, but no percentiles.
    hourly = hourly_from_store(store, refresher.demanded_params)
    assert hourly[0].cloud_high == 20.0
    assert hourly[0].cloud_mid == 40.0
    assert hourly[0].cloud_low == 10.0
    assert hourly[0].temperature_p10 is None
    assert hourly[0].temperature_p90 is None


async def test_refresher_percentile_option_fetches_and_files_band(
    hass: HomeAssistant,
) -> None:
    """With percentiles on, the date-major fetch adds the p10/p90 files only."""
    backend = _GatedRecordingBackend()
    refresher, store = _make_refresher(hass, backend, temp_percentiles=True)
    run = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    with freeze_time(run):
        await refresher.async_refresh(_run(run))

    expected = hourly_date_major_params(temp_percentiles=True)
    for params in _date_major_calls(backend):
        assert params == expected
    hourly = hourly_from_store(store, refresher.demanded_params)
    assert hourly[0].temperature_p10 == 8.0
    assert hourly[0].temperature_p90 == 13.0
    assert hourly[0].cloud_high is None


# ---------------------------------------------------------------------------
# hourly_from_store: required-field gate and optional-field pass-through (#92)
# ---------------------------------------------------------------------------


def _store_with(params_values: dict[str, dict[datetime, float | int]]) -> ForecastStore:
    """A store pre-seeded with ``{param: {hour: value}}`` for one run."""
    store = ForecastStore()
    run = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    for param, values in params_values.items():
        store.put(param, values, run=run, fetched_at=run, source="hourly")
    return store


# The full hourly demand for a plain (non-gated) hourly entry.
_DEMAND = (*HOURLY_DATE_MAJOR_PARAMS, *HOURLY_POINT_MAJOR_PARAMS)
_TEMPERATURE = HOURLY_DATE_MAJOR_PARAMS[0]


def test_from_store_drops_hour_missing_temperature() -> None:
    """An hour present only in point-major params (no temperature) is dropped."""
    h0 = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    store = _store_with(
        {
            HOURLY_PRECIPITATION: {h0: 0.0},
            HOURLY_SYMBOL: {h0: 1},
            HOURLY_WIND_SPEED: {h0: 10.0},
        }
    )
    assert hourly_from_store(store, _DEMAND) == []


def test_from_store_drops_hour_missing_symbol() -> None:
    """An hour whose symbol is absent (ragged point-major head) is dropped."""
    h0 = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    store = _store_with(
        {
            _TEMPERATURE: {h0: 20.0},
            HOURLY_PRECIPITATION: {h0: 0.0},
            HOURLY_WIND_SPEED: {h0: 10.0},
        }
    )
    assert hourly_from_store(store, _DEMAND) == []


def test_from_store_drops_hour_missing_wind_speed() -> None:
    """An hour whose wind speed is absent is dropped."""
    h0 = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    store = _store_with(
        {
            _TEMPERATURE: {h0: 20.0},
            HOURLY_PRECIPITATION: {h0: 0.0},
            HOURLY_SYMBOL: {h0: 1},
        }
    )
    assert hourly_from_store(store, _DEMAND) == []


def test_from_store_keeps_hour_missing_only_optional_fields() -> None:
    """An hour with all required fields is emitted even if optionals are absent.

    precipitation_probability, zero_degree_level, radiation and the B9/B11
    gated fields are optional and must never gate an otherwise good hour.
    """
    h0 = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    store = _store_with(
        {
            _TEMPERATURE: {h0: 20.0},
            HOURLY_PRECIPITATION: {h0: 0.0},
            HOURLY_SYMBOL: {h0: 1},
            HOURLY_WIND_SPEED: {h0: 10.0},
        }
    )
    result = hourly_from_store(store, _DEMAND)
    assert len(result) == 1
    assert result[0].time == h0
    assert result[0].temperature == 20.0
    assert result[0].precipitation_probability is None


def test_from_store_complete_hours_pass_through() -> None:
    """Hours with all required fields across params are emitted intact, sorted."""
    h0 = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    h1 = h0 + timedelta(hours=1)
    store = _store_with(
        {
            _TEMPERATURE: {h0: 20.0, h1: 21.0},
            HOURLY_PRECIPITATION: {h0: 0.0, h1: 0.5},
            HOURLY_SYMBOL: {h0: 1, h1: 6},
            HOURLY_WIND_SPEED: {h0: 10.0, h1: 15.0},
        }
    )
    result = hourly_from_store(store, _DEMAND)
    assert [h.time for h in result] == [h0, h1]
    assert result[1].temperature == 21.0
    assert result[1].symbol == 6


def test_from_store_drops_ragged_head_after_near_refresh() -> None:
    """An hour only in the temperature series (no point-major) is dropped.

    Covers a near-only refresh bringing an hour into the temperature series that
    the point-major group does not cover: no temperature-only stub is emitted.
    """
    h0 = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    h1 = h0 + timedelta(hours=1)
    store = _store_with(
        {
            _TEMPERATURE: {h0: 20.0, h1: 21.0},
            HOURLY_PRECIPITATION: {h1: 0.5},
            HOURLY_SYMBOL: {h1: 6},
            HOURLY_WIND_SPEED: {h1: 15.0},
        }
    )
    result = hourly_from_store(store, _DEMAND)
    assert [h.time for h in result] == [h1]


# --- the refresher files what it fetched in the store (ADR-0008) ---------------


class _ZeroDegreeBackend:
    """Returns hours that carry a zero-degree level and records the run it got."""

    def __init__(self) -> None:
        self.runs: list = []

    async def fetch_daily(self, point, *, run=None):  # pragma: no cover
        return DailyBundle(daily=[])

    async def fetch_hourly(self, point, *, horizon_days=-1, params=(), run=None):
        self.runs.append(run)
        base = datetime(2026, 8, 27, 0, 0, tzinfo=UTC)
        return [
            HourlyForecast(
                time=base + timedelta(hours=h),
                temperature=10.0,
                precipitation=0.0,
                symbol=1,
                wind_speed_kmh=5.0,
                zero_degree_level=2500.0 + 5 * h,
            )
            for h in range(24)
        ]


async def test_refresher_files_series_to_the_store(hass: HomeAssistant) -> None:
    """Whatever the hourly path fetched is readable from the store, per param."""
    backend = _ZeroDegreeBackend()
    refresher, store = _make_refresher(hass, backend)
    run = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    with freeze_time(run):
        assert await refresher.async_refresh(_run(run)) is True

    at = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)
    assert store.value_at(HOURLY_ZERO_DEGREE, at) == 2515.0
    assert store.value_at(HOURLY_SYMBOL, at) == 1
    series = store.get(HOURLY_ZERO_DEGREE)
    assert series is not None
    assert series.provenance.source == "hourly"
    assert series.provenance.run == run

    # Same run again: served from the backend's per-run cache, nothing new.
    with freeze_time(run + timedelta(minutes=5)):
        assert await refresher.async_refresh(_run(run)) is False


async def test_refresher_hands_the_discovered_run_to_the_backend(
    hass: HomeAssistant,
) -> None:
    """The run the coordinator discovered is passed straight to the backend."""
    stamp = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    discovered = _run(stamp)
    backend = _ZeroDegreeBackend()
    refresher, _store = _make_refresher(hass, backend)
    with freeze_time(stamp):
        await refresher.async_refresh(discovered)
    assert backend.runs and all(run is discovered for run in backend.runs)
