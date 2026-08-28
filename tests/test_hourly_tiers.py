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
    HOURLY_DATE_MAJOR_PARAMS,
    HOURLY_POINT_MAJOR_PARAMS,
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
