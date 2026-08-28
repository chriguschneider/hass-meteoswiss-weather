"""Near/far tiered hourly refresh scheduling (issue #68, ADR-0002 revision 2).

Two layers are exercised:

- :func:`_tier_due` — the pure refresh decision — is table-tested with no I/O;
- :class:`HourlyForecastProvider` is driven over frozen time against an
  in-memory recording backend that logs every ``fetch_hourly`` call, so the
  test asserts *which* tier fetched at each step without touching the network.

The measured facts behind the schedule (docs/ogd.md, "Change rhythm across
runs"): the near term (today + tomorrow) moves at the ICON-CH1 runs
{02,05,08,11,14,17,20,23} UTC, days 2+ at the ICON-CH2 runs {05,11,17,23} UTC,
and six runs a day change nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
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
    HourlyForecastProvider,
    _tier_due,
)
from custom_components.meteoswiss_weather.ogd.const import (
    HOURLY_CLOUD_HIGH,
    HOURLY_CLOUD_LOW,
    HOURLY_CLOUD_MID,
    HOURLY_CLOUD_PARAMS,
    HOURLY_DATE_MAJOR_PARAMS,
    HOURLY_POINT_MAJOR_PARAMS,
    HOURLY_TEMP_P10,
    HOURLY_TEMP_P90,
    HOURLY_TEMP_PERCENTILE_PARAMS,
    hourly_date_major_params,
)
from custom_components.meteoswiss_weather.ogd.models import (
    ForecastPoint,
    HourlyForecast,
)

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
# Provider scheduling against a recording backend
# ---------------------------------------------------------------------------


class _RecordingBackend:
    """An in-memory backend that records every ``fetch_hourly`` call.

    ``calls`` holds ``(params, horizon_days)`` tuples in call order so a test
    can assert which tier fetched. ``fetch_hourly`` returns the same synthetic
    24-hour run each time; the provider merges the pieces by hour.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], int]] = []

    async def fetch_daily(self, point):  # pragma: no cover - unused here
        return []

    async def fetch_hourly(self, point, *, horizon_days=-1, params=()):
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


def _tier_of(call: tuple[tuple[str, ...], int]) -> str:
    """Label a recorded fetch as near / far / point-major."""
    params, horizon = call
    if params == tuple(HOURLY_POINT_MAJOR_PARAMS):
        return "point_major"
    if params == tuple(HOURLY_DATE_MAJOR_PARAMS):
        return "near" if horizon == HOURLY_NEAR_HORIZON_DAYS else "far"
    return f"unexpected:{params}:{horizon}"


async def test_provider_first_call_fetches_far_and_point_major(
    hass: HomeAssistant,
) -> None:
    """The first request downloads the far temperature tier and point-major.

    Far is stale (never fetched) so the full-horizon temperature is fetched;
    the near window is a subset of it, so no separate near fetch runs.
    """
    backend = _RecordingBackend()
    provider = HourlyForecastProvider(
        hass, backend, _POINT, enabled=True, horizon_days=_HORIZON_DAYS
    )
    run = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)  # a near-only landing hour
    with freeze_time(run):
        hourly = await provider.async_get_hourly(run)

    assert [_tier_of(c) for c in backend.calls] == ["far", "point_major"]
    assert hourly is not None and len(hourly) == 24
    # The merge carries both the temperature and the point-major fields.
    assert hourly[5].temperature == 5.0
    assert hourly[5].symbol == 1
    assert hourly[5].wind_speed_kmh == 1.0


async def test_provider_near_landing_skips_far(hass: HomeAssistant) -> None:
    """A new run at a near-only landing hour refreshes near, not far.

    Point-major refreshes too (every new run), but the far temperature tier is
    left untouched because the run's hour is not a far landing hour and far is
    still within its 6 h fallback.
    """
    backend = _RecordingBackend()
    provider = HourlyForecastProvider(
        hass, backend, _POINT, enabled=True, horizon_days=_HORIZON_DAYS
    )
    start = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)  # 05 UTC: near and far
    with freeze_time(start) as frozen:
        await provider.async_get_hourly(start)  # far + point-major
        backend.calls.clear()

        # A new run at 08 UTC (near landing, not far), one hour later.
        frozen.move_to(start + timedelta(hours=1))
        run2 = datetime(2026, 8, 27, 8, 0, tzinfo=UTC)
        await provider.async_get_hourly(run2)

    assert [_tier_of(c) for c in backend.calls] == ["near", "point_major"]


async def test_provider_far_landing_hour_refreshes_far(
    hass: HomeAssistant,
) -> None:
    """A new run at a far landing hour refreshes the far tier (full horizon).

    Far landing hours are a subset of near landing hours, so the far fetch also
    resets the near clock; only the full-horizon temperature and the
    point-major group are fetched, never a separate near prefix.
    """
    backend = _RecordingBackend()
    provider = HourlyForecastProvider(
        hass, backend, _POINT, enabled=True, horizon_days=_HORIZON_DAYS
    )
    start = datetime(2026, 8, 27, 8, 0, tzinfo=UTC)  # near-only first fetch
    with freeze_time(start) as frozen:
        await provider.async_get_hourly(start)  # far (stale) + point-major
        backend.calls.clear()

        # A new run at 11 UTC — a far landing hour — one hour later.
        frozen.move_to(start + timedelta(hours=1))
        far_run = datetime(2026, 8, 27, 11, 0, tzinfo=UTC)
        await provider.async_get_hourly(far_run)

    assert [_tier_of(c) for c in backend.calls] == ["far", "point_major"]


async def test_provider_non_landing_run_fetches_only_point_major(
    hass: HomeAssistant,
) -> None:
    """A new run at a hour that changes nothing refreshes only point-major.

    Neither temperature tier is due (03 UTC is neither a near nor a far landing
    hour and both are within their fallbacks), so only the cheap point-major
    blocks are refetched.
    """
    backend = _RecordingBackend()
    provider = HourlyForecastProvider(
        hass, backend, _POINT, enabled=True, horizon_days=_HORIZON_DAYS
    )
    start = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)
    with freeze_time(start) as frozen:
        await provider.async_get_hourly(start)  # far + point-major
        backend.calls.clear()

        frozen.move_to(start + timedelta(hours=1))
        run2 = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)  # non-landing hour
        await provider.async_get_hourly(run2)

    assert [_tier_of(c) for c in backend.calls] == ["point_major"]


async def test_provider_far_fallback_refetches_without_new_run(
    hass: HomeAssistant,
) -> None:
    """Past the far fallback the far tier refetches even on an unchanged run.

    The run stamp does not change, so point-major stays cached; only the stale
    far temperature is refetched.
    """
    backend = _RecordingBackend()
    provider = HourlyForecastProvider(
        hass, backend, _POINT, enabled=True, horizon_days=_HORIZON_DAYS
    )
    start = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)
    with freeze_time(start) as frozen:
        await provider.async_get_hourly(start)  # far + point-major
        backend.calls.clear()

        # Same run, but past the 6 h far fallback.
        frozen.move_to(start + HOURLY_FAR_MAX_AGE + timedelta(seconds=1))
        await provider.async_get_hourly(start)

    assert [_tier_of(c) for c in backend.calls] == ["far"]


async def test_provider_near_fallback_refetches_near_only(
    hass: HomeAssistant,
) -> None:
    """Past the near fallback (but within far's) only the near tier refetches.

    A near-only fetch happened first (a near-landing run after an initial far
    fetch). Advancing past the 3 h near fallback while staying inside far's 6 h
    window, on an unchanged run, makes near due and far not.
    """
    backend = _RecordingBackend()
    provider = HourlyForecastProvider(
        hass, backend, _POINT, enabled=True, horizon_days=_HORIZON_DAYS
    )
    start = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)
    with freeze_time(start) as frozen:
        await provider.async_get_hourly(start)  # far + point-major at t0

        # A near-only landing run 2 h later resets the near clock, not far's.
        frozen.move_to(start + timedelta(hours=2))
        near_run = datetime(2026, 8, 27, 8, 0, tzinfo=UTC)
        await provider.async_get_hourly(near_run)  # near + point-major
        backend.calls.clear()

        # Now +3 h past the last near fetch (near stale) but only 5 h past the
        # far fetch (far fresh), unchanged run: near refetches alone.
        frozen.move_to(start + timedelta(hours=5, seconds=1))
        await provider.async_get_hourly(near_run)

    assert [_tier_of(c) for c in backend.calls] == ["near"]


class _TrimmingBackend:
    """A backend that trims its synthetic run to the requested horizon.

    Unlike :class:`_RecordingBackend` (which returns a fixed window whatever the
    caller asks), this honours ``horizon_days`` so the test can see the near
    tier's horizon, not just its param set.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], int]] = []

    async def fetch_daily(self, point):  # pragma: no cover - unused here
        return []

    async def fetch_hourly(self, point, *, horizon_days=-1, params=()):
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
    fields, flickering in and out as near and far alternate. The near horizon is
    capped at the configured horizon, so the forecast length stays stable.
    """
    backend = _TrimmingBackend()
    provider = HourlyForecastProvider(
        hass, backend, _POINT, enabled=True, horizon_days=0
    )
    start = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)  # far landing hour
    with freeze_time(start) as frozen:
        after_far = await provider.async_get_hourly(start)
        assert after_far is not None

        # A near-only landing run (08 UTC) an hour later.
        frozen.move_to(start + timedelta(hours=1))
        after_near = await provider.async_get_hourly(
            datetime(2026, 8, 27, 8, 0, tzinfo=UTC)
        )

    assert after_near is not None
    # No growth and no temperature-only leak past the point-major window.
    assert len(after_near) == len(after_far)
    assert all(h.symbol is not None for h in after_near)
    # The near fetch used the capped horizon, not the default reach of 1.
    near_calls = [c for c in backend.calls if c[0] == tuple(HOURLY_DATE_MAJOR_PARAMS)]
    assert near_calls[-1][1] == 0


async def test_provider_disabled_never_fetches(hass: HomeAssistant) -> None:
    """With the option off the provider returns None and never fetches."""
    backend = _RecordingBackend()
    provider = HourlyForecastProvider(
        hass, backend, _POINT, enabled=False, horizon_days=_HORIZON_DAYS
    )
    run = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)
    with freeze_time(run):
        assert await provider.async_get_hourly(run) is None
    assert backend.calls == []


@pytest.mark.parametrize("run", [None])
async def test_provider_none_run_returns_none(
    hass: HomeAssistant, run
) -> None:
    """No tracked run yet means nothing to fetch."""
    backend = _RecordingBackend()
    provider = HourlyForecastProvider(
        hass, backend, _POINT, enabled=True, horizon_days=_HORIZON_DAYS
    )
    assert await provider.async_get_hourly(run) is None
    assert backend.calls == []


# ---------------------------------------------------------------------------
# B9/B11 per-entity gating of the date-major additions (issue #69)
# ---------------------------------------------------------------------------


class _GatedRecordingBackend:
    """Recording backend that fills the gated date-major fields when asked.

    ``fetch_hourly`` returns cloud and percentile values only for the params it
    is actually asked for, so a test can assert both which files a tier fetched
    *and* that the merge carries the gated fields through.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], int]] = []

    async def fetch_daily(self, point):  # pragma: no cover - unused here
        return []

    async def fetch_hourly(self, point, *, horizon_days=-1, params=()):
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


async def test_provider_default_fetches_no_gated_files(hass: HomeAssistant) -> None:
    """With neither gated option on, the date-major fetch is temperature only."""
    backend = _GatedRecordingBackend()
    provider = HourlyForecastProvider(
        hass, backend, _POINT, enabled=True, horizon_days=_HORIZON_DAYS
    )
    run = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    with freeze_time(run):
        await provider.async_get_hourly(run)

    for params in _date_major_calls(backend):
        assert params == tuple(HOURLY_DATE_MAJOR_PARAMS)
        assert not set(params) & set(HOURLY_CLOUD_PARAMS)
        assert not set(params) & set(HOURLY_TEMP_PERCENTILE_PARAMS)


async def test_provider_cloud_option_fetches_and_merges_layers(
    hass: HomeAssistant,
) -> None:
    """With cloud layers on, the date-major fetch adds the three cloud files."""
    backend = _GatedRecordingBackend()
    provider = HourlyForecastProvider(
        hass,
        backend,
        _POINT,
        enabled=True,
        horizon_days=_HORIZON_DAYS,
        cloud_layers=True,
    )
    run = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    with freeze_time(run):
        hourly = await provider.async_get_hourly(run)

    expected = hourly_date_major_params(cloud_layers=True)
    for params in _date_major_calls(backend):
        assert params == expected
    # The merged forecast carries the cloud fields, but no percentiles.
    assert hourly is not None
    assert hourly[0].cloud_high == 20.0
    assert hourly[0].cloud_mid == 40.0
    assert hourly[0].cloud_low == 10.0
    assert hourly[0].temperature_p10 is None
    assert hourly[0].temperature_p90 is None


async def test_provider_percentile_option_fetches_and_merges_band(
    hass: HomeAssistant,
) -> None:
    """With percentiles on, the date-major fetch adds the p10/p90 files only."""
    backend = _GatedRecordingBackend()
    provider = HourlyForecastProvider(
        hass,
        backend,
        _POINT,
        enabled=True,
        horizon_days=_HORIZON_DAYS,
        temp_percentiles=True,
    )
    run = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    with freeze_time(run):
        hourly = await provider.async_get_hourly(run)

    expected = hourly_date_major_params(temp_percentiles=True)
    for params in _date_major_calls(backend):
        assert params == expected
    assert hourly is not None
    assert hourly[0].temperature_p10 == 8.0
    assert hourly[0].temperature_p90 == 13.0
    assert hourly[0].cloud_high is None


# ---------------------------------------------------------------------------
# _merge: required-field gate and optional-field pass-through (issue #92)
# ---------------------------------------------------------------------------


def _complete_hour(when: datetime) -> HourlyForecast:
    """A fully populated hour (all required fields present)."""
    return HourlyForecast(
        time=when,
        temperature=20.0,
        precipitation=0.0,
        symbol=1,
        wind_speed_kmh=10.0,
        gust_kmh=15.0,
        wind_bearing=270,
        precipitation_probability=30.0,
    )


def _provider_with_groups(
    hass,
    date_major: dict[datetime, HourlyForecast],
    point_major: dict[datetime, HourlyForecast],
) -> HourlyForecastProvider:
    """Return a provider whose two groups are pre-seeded (no fetch needed)."""
    provider = HourlyForecastProvider(
        hass, _RecordingBackend(), _POINT, enabled=True, horizon_days=2
    )
    provider._date_major = date_major
    provider._point_major = point_major
    return provider


async def test_merge_drops_hour_missing_temperature(hass: HomeAssistant) -> None:
    """An hour present only in point_major (no temperature) is not emitted."""
    h0 = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    # date_major has no entry for h0; point_major does.
    dm = {}
    pm = {h0: _complete_hour(h0)}
    provider = _provider_with_groups(hass, dm, pm)
    result = provider._merge()
    assert result == []


async def test_merge_drops_hour_missing_symbol(hass: HomeAssistant) -> None:
    """An hour whose symbol is None (ragged point-major head) is not emitted."""
    h0 = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    # Both groups have h0, but point-major symbol is missing.
    dm = {h0: HourlyForecast(time=h0, temperature=20.0)}
    block = HourlyForecast(time=h0, precipitation=0.0, symbol=None, wind_speed_kmh=10.0)
    pm = {h0: block}
    provider = _provider_with_groups(hass, dm, pm)
    result = provider._merge()
    assert result == []


async def test_merge_drops_hour_missing_wind_speed(hass: HomeAssistant) -> None:
    """An hour whose wind_speed_kmh is None is not emitted."""
    h0 = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    dm = {h0: HourlyForecast(time=h0, temperature=20.0)}
    block = HourlyForecast(time=h0, precipitation=0.0, symbol=1, wind_speed_kmh=None)
    pm = {h0: block}
    provider = _provider_with_groups(hass, dm, pm)
    result = provider._merge()
    assert result == []


async def test_merge_keeps_hour_missing_only_optional_fields(
    hass: HomeAssistant,
) -> None:
    """An hour with all required fields is emitted even if optional fields are None.

    precipitation_probability, zero_degree_level, radiation and the B9/B11
    gated fields are optional and must never gate an otherwise good hour.
    """
    h0 = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    dm = {h0: HourlyForecast(time=h0, temperature=20.0)}
    block = HourlyForecast(
        time=h0,
        precipitation=0.0,
        symbol=1,
        wind_speed_kmh=10.0,
        # All optional fields absent.
        precipitation_probability=None,
        zero_degree_level=None,
        radiation=None,
    )
    pm = {h0: block}
    provider = _provider_with_groups(hass, dm, pm)
    result = provider._merge()
    assert len(result) == 1
    assert result[0].time == h0
    assert result[0].temperature == 20.0
    assert result[0].precipitation_probability is None


async def test_merge_complete_hours_pass_through(hass: HomeAssistant) -> None:
    """Hours with all required fields in both groups are emitted intact."""
    h0 = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    h1 = h0 + timedelta(hours=1)
    dm = {
        h0: HourlyForecast(time=h0, temperature=20.0),
        h1: HourlyForecast(time=h1, temperature=21.0),
    }
    pm = {
        h0: HourlyForecast(time=h0, precipitation=0.0, symbol=1, wind_speed_kmh=10.0),
        h1: HourlyForecast(time=h1, precipitation=0.5, symbol=6, wind_speed_kmh=15.0),
    }
    provider = _provider_with_groups(hass, dm, pm)
    result = provider._merge()
    assert len(result) == 2
    assert result[0].time == h0
    assert result[1].time == h1
    assert result[1].temperature == 21.0
    assert result[1].symbol == 6


async def test_merge_near_only_refresh_drops_ragged_head(
    hass: HomeAssistant,
) -> None:
    """After a near-only refresh, an hour that exists only in date_major is dropped.

    This covers the scenario where the near tier has a fresher temperature
    for an hour that the point_major group does not yet cover: _merge must not
    emit a temperature-only stub.
    """
    h0 = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    h1 = h0 + timedelta(hours=1)
    # Near fetch brought h0 into date_major but point_major has only h1.
    dm = {
        h0: HourlyForecast(time=h0, temperature=20.0),
        h1: HourlyForecast(time=h1, temperature=21.0),
    }
    pm = {
        h1: HourlyForecast(time=h1, precipitation=0.5, symbol=6, wind_speed_kmh=15.0),
    }
    provider = _provider_with_groups(hass, dm, pm)
    result = provider._merge()
    # h0 is dropped (no point-major data); h1 is complete.
    assert len(result) == 1
    assert result[0].time == h1
