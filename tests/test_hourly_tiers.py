"""Eager, canary-driven hourly refresh (issue #125, ADR-0008; #124/#68 history).

:class:`HourlyRefresher` is driven over frozen time against in-memory recording
backends that log every ``fetch_hourly`` and ``fetch_hourly_canary`` call, so a
test asserts *what* each new run fetched (or confirmed) without touching the
network. The lazy, card-driven provider it replaced is gone (ADR-0008, "Decided
by the owner", item 1): the refresher fetches whether or not anything subscribes.

What triggers a refresh is now a **canary** read, not a timetable (ADR-0008
section 3, owner decision 2): every new run reads the point's next few hours of
one representative file per group and compares them with the store. Equal values
keep the stored series and re-stamp it to the run; different values, or a canary
that cannot be read, refresh the group. The near/far/point-major ``max_age``
fallbacks still force a refresh so a canary blind spot cannot let a series go
stale unbounded.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.meteoswiss_weather.const import (
    HOURLY_FAR_MAX_AGE,
    HOURLY_HORIZON_FULL_RUN,
    HOURLY_NEAR_HORIZON_DAYS,
    HOURLY_NEAR_MAX_AGE,
    HOURLY_POINT_MAJOR_MAX_AGE,
)
from custom_components.meteoswiss_weather.coordinator import (
    HourlyRefresher,
    hourly_from_store,
)
from custom_components.meteoswiss_weather.ogd import DailyBundle, FileLayout, Run
from custom_components.meteoswiss_weather.ogd.const import (
    HOURLY_CLOUD_HIGH,
    HOURLY_CLOUD_LOW,
    HOURLY_CLOUD_MID,
    HOURLY_DATE_MAJOR_PARAMS,
    HOURLY_POINT_MAJOR_PARAMS,
    HOURLY_PRECIPITATION,
    HOURLY_SYMBOL,
    HOURLY_TEMP_P10,
    HOURLY_TEMP_P90,
    HOURLY_TEMPERATURE,
    HOURLY_WIND_SPEED,
    HOURLY_ZERO_DEGREE,
    hourly_date_major_params,
)
from custom_components.meteoswiss_weather.ogd.forecast import HOURLY_FIELD_BY_PARAM
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
_BASE = datetime(2026, 8, 27, 0, 0, tzinfo=UTC)


def _run(stamp: datetime) -> Run:
    """A minimal discovered run for ``stamp`` (assets unused by the fakes)."""
    return Run(timestamp=stamp, assets={})


# ---------------------------------------------------------------------------
# A recording backend whose forecast content the test can change between runs
# ---------------------------------------------------------------------------


class _CanaryBackend:
    """Records fetches and answers canary reads from a mutable ``content``.

    Both ``fetch_hourly`` and ``fetch_hourly_canary`` derive their values from
    the same ``content`` version and hour, so leaving ``content`` alone across a
    new run makes the canary equal the store (unchanged), and bumping it makes
    the canary differ (a changed forecast). ``fail_canary`` makes the canary read
    yield nothing, which the refresher must treat as changed.
    """

    def __init__(self) -> None:
        # Each call records (params, horizon_days, window_start_override): the
        # far remainder passes a window start (issue #143), the near window does
        # not, so the tuple tells the two date-major tiers apart.
        self.calls: list[tuple[tuple[str, ...], int, datetime | None]] = []
        self.canary_calls: list[tuple[str, int]] = []
        self.content = 0.0
        self.fail_canary = False

    def _value(self, field: str, hour: int) -> float | int:
        if field == "symbol":
            return 1
        # Continuous fields move with the content version so the canary can see
        # a change; the exact shape does not matter, only that it is a function
        # of (field, hour, content).
        return round(float(hour) + self.content, 2)

    def _hour(self, field: str, h: int) -> HourlyForecast:
        return HourlyForecast(
            time=_BASE + timedelta(hours=h),
            temperature=self._value("temperature", h),
            precipitation=0.0,
            symbol=1,
            wind_speed_kmh=self._value("wind_speed_kmh", h),
            gust_kmh=2.0,
            wind_bearing=90,
        )

    async def fetch_daily(self, point, *, run=None):  # pragma: no cover
        return DailyBundle(daily=[])

    async def fetch_hourly(
        self, point, *, horizon_days=-1, params=(), run=None,
        window_start_override=None,
    ):
        self.calls.append((tuple(params), horizon_days, window_start_override))
        return [self._hour("", h) for h in range(24)]

    async def fetch_hourly_canary(self, point, param, *, hours, run=None):
        self.canary_calls.append((param, hours))
        if self.fail_canary:
            return None
        field = HOURLY_FIELD_BY_PARAM[param]
        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        out: dict[datetime, float | int] = {}
        for h in range(24):
            when = _BASE + timedelta(hours=h)
            if now <= when < now + timedelta(hours=hours):
                out[when] = self._value(field, h)
        return out or None


def _make_refresher(hass, backend, *, horizon_days=_HORIZON_DAYS, **kwargs):
    """A refresher wired to a fresh store; returns ``(refresher, store)``."""
    store = ForecastStore()
    refresher = HourlyRefresher(
        hass, backend, _POINT, store, enabled=True, horizon_days=horizon_days, **kwargs
    )
    return refresher, store


def _tier_of(call: tuple[tuple[str, ...], int, datetime | None]) -> str:
    """Label a recorded fetch as near / far / point-major.

    The far remainder is the only date-major fetch that passes a window start
    (issue #143); the near window and the point-major group do not.
    """
    params, _horizon, window_start = call
    if params == tuple(HOURLY_POINT_MAJOR_PARAMS):
        return "point_major"
    return "far" if window_start is not None else "near"


# ---------------------------------------------------------------------------
# The first refresh fetches; there is nothing yet to canary against
# ---------------------------------------------------------------------------


async def test_first_refresh_fetches_near_and_point_major(
    hass: HomeAssistant,
) -> None:
    """The first refresh downloads the near window + point-major, reads no canary.

    At the default horizon the near window already covers the whole horizon, so
    there is no far remainder to fetch; both groups are "never fetched", so they
    fetch straight away — a canary read would have nothing to compare against.
    """
    backend = _CanaryBackend()
    refresher, store = _make_refresher(hass, backend)
    run = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    with freeze_time(run):
        assert await refresher.async_refresh(_run(run)) is True

    assert [_tier_of(c) for c in backend.calls] == ["near", "point_major"]
    assert backend.canary_calls == []  # nothing to canary on the first run
    # Acceptance #143.4: the default horizon never has a far remainder, so it
    # never pays the far tier — its behaviour and cost are unchanged.
    assert not refresher._has_far_remainder
    hourly = hourly_from_store(store, refresher.demanded_params)
    assert len(hourly) == 24
    assert hourly[5].temperature == 5.0
    assert hourly[5].symbol == 1


async def test_default_horizon_changed_canary_never_fetches_far(
    hass: HomeAssistant,
) -> None:
    """Acceptance #143.4: at the default horizon a changed run refreshes the near
    window (the whole horizon) and never triggers a far-remainder fetch."""
    backend = _CanaryBackend()
    refresher, _store = _make_refresher(hass, backend)  # default horizon (2)
    start = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    with freeze_time(start) as frozen:
        await refresher.async_refresh(_run(start))
        backend.calls.clear()
        # Several changed runs over many hours: never a far fetch.
        for hour in range(1, 8):
            backend.content += 1.0
            frozen.move_to(start + timedelta(hours=hour))
            await refresher.async_refresh(_run(start + timedelta(hours=hour)))

    assert all(_tier_of(c) != "far" for c in backend.calls)
    # The near window is fetched for the whole configured horizon, as before.
    near = next(c for c in backend.calls if _tier_of(c) == "near")
    assert near[1] == _HORIZON_DAYS


# ---------------------------------------------------------------------------
# Unchanged canary: keep and confirm, no fetch (acceptance 1)
# ---------------------------------------------------------------------------


async def test_unchanged_canary_confirms_without_fetching(
    hass: HomeAssistant,
) -> None:
    """A new run whose canary matches the store re-stamps it, downloads nothing."""
    backend = _CanaryBackend()
    refresher, store = _make_refresher(hass, backend)
    start = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    with freeze_time(start) as frozen:
        await refresher.async_refresh(_run(start))  # first fetch
        backend.calls.clear()
        backend.canary_calls.clear()

        # A genuinely new run, one hour later, but the forecast is unchanged.
        frozen.move_to(start + timedelta(hours=1))
        new_run = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)
        assert await refresher.async_refresh(_run(new_run)) is False

    # No download; both groups were canaried once.
    assert backend.calls == []
    assert {param for param, _ in backend.canary_calls} == {
        HOURLY_TEMPERATURE,
        HOURLY_WIND_SPEED,
    }
    # The store's series now read as current for the new run, and confirmed.
    for param in (HOURLY_TEMPERATURE, HOURLY_SYMBOL, HOURLY_WIND_SPEED):
        series = store.get(param)
        assert series is not None
        assert series.provenance.run == new_run
        assert series.provenance.confirmed is True
        assert not store.is_stale(param, new_run)


# ---------------------------------------------------------------------------
# Changed canary: refresh the group once (acceptance 2)
# ---------------------------------------------------------------------------


async def test_changed_canary_refreshes_both_groups_once(
    hass: HomeAssistant,
) -> None:
    """A new run whose canary differs refetches near + point-major, exactly once."""
    backend = _CanaryBackend()
    refresher, store = _make_refresher(hass, backend)
    start = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    with freeze_time(start) as frozen:
        await refresher.async_refresh(_run(start))
        backend.calls.clear()

        # The forecast moved; a new run one hour later.
        backend.content = 5.0
        frozen.move_to(start + timedelta(hours=1))
        new_run = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)
        assert await refresher.async_refresh(_run(new_run)) is True

    assert [_tier_of(c) for c in backend.calls] == ["near", "point_major"]
    # The store carries the new content, not confirmed (it was fetched).
    series = store.get(HOURLY_TEMPERATURE)
    assert series is not None
    assert series.provenance.confirmed is False
    assert store.value_at(HOURLY_TEMPERATURE, _BASE + timedelta(hours=5)) == 10.0


async def test_changed_date_major_only_leaves_point_major_confirmed(
    hass: HomeAssistant,
) -> None:
    """A change the point-major canary does not see refreshes only date-major."""
    backend = _CanaryBackend()
    refresher, store = _make_refresher(hass, backend)
    start = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)

    # A backend where only the temperature (date-major) canary sees the change.
    class _TempOnly(_CanaryBackend):
        async def fetch_hourly_canary(self, point, param, *, hours, run=None):
            if param == HOURLY_WIND_SPEED:
                # Pretend the point-major group did not move: return the stored
                # values by using content 0 for wind regardless of self.content.
                saved, self.content = self.content, 0.0
                try:
                    return await super().fetch_hourly_canary(
                        point, param, hours=hours, run=run
                    )
                finally:
                    self.content = saved
            return await super().fetch_hourly_canary(
                point, param, hours=hours, run=run
            )

    backend = _TempOnly()
    refresher, store = _make_refresher(hass, backend)
    with freeze_time(start) as frozen:
        await refresher.async_refresh(_run(start))
        backend.calls.clear()

        backend.content = 5.0
        frozen.move_to(start + timedelta(hours=1))
        await refresher.async_refresh(_run(datetime(2026, 8, 27, 3, 0, tzinfo=UTC)))

    # Only the date-major near window refetched; point-major stayed confirmed.
    assert [_tier_of(c) for c in backend.calls] == ["near"]
    assert store.get(HOURLY_SYMBOL).provenance.confirmed is True


# ---------------------------------------------------------------------------
# Canary failure and max-age both force a refresh (acceptance 3)
# ---------------------------------------------------------------------------


async def test_canary_failure_counts_as_changed(hass: HomeAssistant) -> None:
    """A canary that cannot be read forces a refresh of both groups."""
    backend = _CanaryBackend()
    refresher, _store = _make_refresher(hass, backend)
    start = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    with freeze_time(start) as frozen:
        await refresher.async_refresh(_run(start))
        backend.calls.clear()

        backend.fail_canary = True
        frozen.move_to(start + timedelta(hours=1))
        await refresher.async_refresh(_run(datetime(2026, 8, 27, 3, 0, tzinfo=UTC)))

    assert [_tier_of(c) for c in backend.calls] == ["near", "point_major"]


async def test_far_fallback_refetches_without_a_new_run(
    hass: HomeAssistant,
) -> None:
    """Past the far fallback the far remainder refetches even on an unchanged run.

    A stale tier is due before the canary is consulted, so no canary read is
    needed to force it. Uses horizon 3, which reaches beyond the near window and
    so has a far remainder at all.
    """
    backend = _CanaryBackend()
    refresher, _store = _make_refresher(hass, backend, horizon_days=3)
    start = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)
    with freeze_time(start) as frozen:
        await refresher.async_refresh(_run(start))
        backend.calls.clear()
        backend.canary_calls.clear()

        # Same run, but past the 6 h far fallback (and the 3 h near one).
        frozen.move_to(start + HOURLY_FAR_MAX_AGE + timedelta(seconds=1))
        await refresher.async_refresh(_run(start))

    tiers = [_tier_of(c) for c in backend.calls]
    assert "far" in tiers
    assert backend.canary_calls == []  # the fallback fired, no probe needed


async def test_near_fallback_refetches_near_not_far(hass: HomeAssistant) -> None:
    """Past the near fallback (but within far's) the near tier refetches, not far.

    Uses horizon 3 so a far remainder exists and can be shown to stay put while
    only the near window refetches.
    """
    backend = _CanaryBackend()
    refresher, _store = _make_refresher(hass, backend, horizon_days=3)
    start = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)
    with freeze_time(start) as frozen:
        await refresher.async_refresh(_run(start))  # near + far + point-major at t0
        backend.calls.clear()

        # Same run, +3 h past the near fetch (near stale) but 3 h < far's 6 h
        # (far fresh): the near window refetches, the far remainder does not.
        frozen.move_to(start + HOURLY_NEAR_MAX_AGE + timedelta(seconds=1))
        await refresher.async_refresh(_run(start))

    tiers = [_tier_of(c) for c in backend.calls]
    assert "near" in tiers and "far" not in tiers
    assert backend.canary_calls == []  # the fallback fired, no probe needed


async def test_point_major_fallback_refetches(hass: HomeAssistant) -> None:
    """Past the point-major fallback the group refetches on an unchanged run."""
    backend = _CanaryBackend()
    refresher, _store = _make_refresher(hass, backend)
    start = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)
    with freeze_time(start) as frozen:
        await refresher.async_refresh(_run(start))
        backend.calls.clear()

        frozen.move_to(start + HOURLY_POINT_MAJOR_MAX_AGE + timedelta(seconds=1))
        await refresher.async_refresh(_run(start))

    assert "point_major" in [_tier_of(c) for c in backend.calls]


# ---------------------------------------------------------------------------
# Split near/far refresh for a horizon beyond the near window (issue #143)
# ---------------------------------------------------------------------------


class _WindowBackend(_CanaryBackend):
    """Returns window-accurate hours so ``hourly_from_store`` spans the horizon.

    A date-major fetch fills only the fields of the params it was asked for
    (temperature for the near window / far remainder); the point-major group
    fills symbol/precipitation/wind. Every call returns exactly the hours of its
    effective window, so the store's union reaches the configured horizon only
    when the far hours are present.
    """

    def _fields(self, params, h):
        out: dict[str, float | int] = {}
        for p in params:
            field = HOURLY_FIELD_BY_PARAM[p]
            out[field] = 1 if field == "symbol" else round(float(h) + self.content, 2)
        return out

    async def fetch_hourly(
        self, point, *, horizon_days=-1, params=(), run=None,
        window_start_override=None,
    ):
        from custom_components.meteoswiss_weather.ogd.hourly import horizon_end_utc

        self.calls.append((tuple(params), horizon_days, window_start_override))
        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        end = horizon_end_utc(horizon_days, now)
        start = window_start_override if window_start_override is not None else now
        hours: list[HourlyForecast] = []
        when = start
        # Cap the full-run case so the fake terminates.
        hard_end = end if end is not None else now + timedelta(hours=240)
        while when < hard_end:
            h = int((when - _BASE).total_seconds() // 3600)
            hours.append(HourlyForecast(time=when, **self._fields(params, h)))
            when += timedelta(hours=1)
        return hours


@pytest.mark.parametrize("horizon_days", [3, HOURLY_HORIZON_FULL_RUN])
async def test_changed_canary_refreshes_near_only_far_on_cadence(
    hass: HomeAssistant, horizon_days: int
) -> None:
    """Acceptance #143.1: a changed canary refreshes only the near window; the
    far remainder is fetched at most once per HOURLY_FAR_MAX_AGE."""
    backend = _CanaryBackend()
    refresher, _store = _make_refresher(hass, backend, horizon_days=horizon_days)
    start = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    with freeze_time(start) as frozen:
        # First refresh: near + far + point-major all fetch (nothing stored yet).
        await refresher.async_refresh(_run(start))
        assert [_tier_of(c) for c in backend.calls] == ["near", "far", "point_major"]
        backend.calls.clear()

        # Four consecutive changed runs within the 6 h far window: each refreshes
        # the near window, and the far remainder is never touched.
        for hour in range(1, 5):
            backend.content += 1.0
            frozen.move_to(start + timedelta(hours=hour))
            await refresher.async_refresh(_run(start + timedelta(hours=hour)))
        tiers = [_tier_of(c) for c in backend.calls]
        assert "far" not in tiers
        assert tiers.count("near") == 4  # one near per changed run
        backend.calls.clear()

        # Past the far fallback: the far remainder refetches, exactly once.
        frozen.move_to(start + HOURLY_FAR_MAX_AGE + timedelta(seconds=1))
        await refresher.async_refresh(_run(start + HOURLY_FAR_MAX_AGE))
        assert [_tier_of(c) for c in backend.calls].count("far") == 1


async def test_far_remainder_starts_beyond_the_near_window(
    hass: HomeAssistant,
) -> None:
    """The far remainder is fetched from the near-window end, not the run start."""
    from custom_components.meteoswiss_weather.ogd.hourly import horizon_end_utc

    backend = _CanaryBackend()
    refresher, _store = _make_refresher(hass, backend, horizon_days=3)
    start = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    with freeze_time(start):
        await refresher.async_refresh(_run(start))

    far = next(c for c in backend.calls if _tier_of(c) == "far")
    _params, _horizon, window_start = far
    assert window_start == horizon_end_utc(HOURLY_NEAR_HORIZON_DAYS, start)


async def test_horizon_reached_after_near_only_refresh_on_new_run(
    hass: HomeAssistant,
) -> None:
    """Acceptance #143.3: the hourly forecast still reaches the configured horizon
    right after a near-only refresh on a new run — the far hours are held over."""
    backend = _WindowBackend()
    refresher, store = _make_refresher(hass, backend, horizon_days=3)
    start = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    with freeze_time(start) as frozen:
        await refresher.async_refresh(_run(start))
        full = hourly_from_store(store, refresher.demanded_params)
        assert full  # the horizon is populated
        last_hour = full[-1].time

        # A new run one hour later whose canary changed: only the near window
        # (and the point-major group) refetch; the far remainder is not due.
        backend.content = 9.0
        backend.calls.clear()
        frozen.move_to(start + timedelta(hours=1))
        await refresher.async_refresh(_run(start + timedelta(hours=1)))
        assert "far" not in [_tier_of(c) for c in backend.calls]

        after_near = hourly_from_store(store, refresher.demanded_params)

    # The forecast still reaches the same far horizon: the previous run's far
    # hours were kept, not dropped by the newer near-only window (issue #143).
    assert after_near[-1].time == last_hour


# ---------------------------------------------------------------------------
# Schedule by detected layout, not the static group (issue #153)
# ---------------------------------------------------------------------------


class _LayoutHintWindowBackend(_WindowBackend):
    """A window-accurate backend that reports zprfr0hs as date-major (#153).

    ``zprfr0hs`` is date-major upstream (docs/ogd.md §E4) yet still listed in the
    static point-major group. Reporting its detected layout through
    ``layout_hint`` must make the refresher schedule it with the date-major near
    and far tiers rather than fetching it whole per run with the point-major
    group. Every other file keeps its natural layout.
    """

    def layout_hint(self, param: str) -> FileLayout | None:
        if param in (HOURLY_TEMPERATURE, HOURLY_ZERO_DEGREE):
            return FileLayout.DATE_MAJOR
        return FileLayout.POINT_MAJOR_TYPE


def _is_far(call) -> bool:
    """A date-major far-remainder fetch: it passes a window start override."""
    return call[2] is not None


def _is_near(call) -> bool:
    """A date-major near-window fetch: no window override, carries temperature."""
    return call[2] is None and HOURLY_TEMPERATURE in call[0]


def _is_point_major(call) -> bool:
    """A point-major group fetch: no window override, carries precipitation."""
    return call[2] is None and HOURLY_PRECIPITATION in call[0]


async def test_date_major_hint_moves_zprfr0hs_to_the_date_major_tiers(
    hass: HomeAssistant,
) -> None:
    """Acceptance #153.2: a point-major-group file whose hint says date-major gets
    the near window on a changed canary and the far tail at most once per
    HOURLY_FAR_MAX_AGE — never fetched whole with the point-major group."""
    backend = _LayoutHintWindowBackend()
    refresher, _store = _make_refresher(hass, backend, horizon_days=3)
    start = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    with freeze_time(start) as frozen:
        # First refresh: near + far + point-major. zprfr0hs rides the date-major
        # near and far fetches; it is absent from the point-major group.
        await refresher.async_refresh(_run(start))
        near = [c for c in backend.calls if _is_near(c)]
        far = [c for c in backend.calls if _is_far(c)]
        point_major = [c for c in backend.calls if _is_point_major(c)]
        assert near and far and point_major
        assert all(HOURLY_ZERO_DEGREE in c[0] for c in near)
        assert all(HOURLY_ZERO_DEGREE in c[0] for c in far)
        assert all(HOURLY_ZERO_DEGREE not in c[0] for c in point_major)
        backend.calls.clear()

        # Four changed runs within the 6 h far window: the near window (with
        # zprfr0hs) refetches each time, the far remainder is never touched.
        for hour in range(1, 5):
            backend.content += 1.0
            frozen.move_to(start + timedelta(hours=hour))
            await refresher.async_refresh(_run(start + timedelta(hours=hour)))
        assert [c for c in backend.calls if _is_far(c)] == []
        assert backend.calls  # something was fetched
        assert all(
            HOURLY_ZERO_DEGREE in c[0] for c in backend.calls if _is_near(c)
        )
        backend.calls.clear()

        # Past the far fallback: the far remainder refetches, exactly once.
        frozen.move_to(start + HOURLY_FAR_MAX_AGE + timedelta(seconds=1))
        await refresher.async_refresh(_run(start + HOURLY_FAR_MAX_AGE))
        assert len([c for c in backend.calls if _is_far(c)]) == 1


async def test_date_major_hint_keeps_zero_degree_to_the_horizon(
    hass: HomeAssistant,
) -> None:
    """Acceptance #153.4: with zprfr0hs scheduled on the date-major tiers, the
    hourly forecast still carries zero_degree_level for every hour to the
    horizon (near window + far remainder together span it)."""
    from custom_components.meteoswiss_weather.ogd.hourly import horizon_end_utc

    backend = _LayoutHintWindowBackend()
    refresher, store = _make_refresher(hass, backend, horizon_days=3)
    start = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    with freeze_time(start):
        await refresher.async_refresh(_run(start))
        hourly = hourly_from_store(store, refresher.demanded_params)

    assert hourly
    assert all(h.zero_degree_level is not None for h in hourly)
    # The forecast reaches the configured 3-day horizon.
    horizon_end = horizon_end_utc(3, start)
    assert horizon_end is not None
    assert hourly[-1].time == horizon_end - timedelta(hours=1)


async def test_no_layout_hint_keeps_the_static_grouping(hass: HomeAssistant) -> None:
    """A backend without ``layout_hint`` (or a file with no hint yet) keeps the
    static group split, so zprfr0hs rides the point-major group as before."""
    backend = _CanaryBackend()  # no layout_hint method at all
    refresher, _store = _make_refresher(hass, backend, horizon_days=3)
    start = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    with freeze_time(start):
        await refresher.async_refresh(_run(start))
    point_major = [c for c in backend.calls if _tier_of(c) == "point_major"]
    assert point_major  # the static point-major group still fetched as one tier
    assert all(HOURLY_ZERO_DEGREE in c[0] for c in point_major)


# ---------------------------------------------------------------------------
# The near tier still never overshoots a narrowed horizon (issue #92)
# ---------------------------------------------------------------------------


class _TrimmingCanaryBackend(_CanaryBackend):
    """Trims its synthetic run to the requested horizon."""

    async def fetch_hourly(
        self, point, *, horizon_days=-1, params=(), run=None,
        window_start_override=None,
    ):
        self.calls.append((tuple(params), horizon_days, window_start_override))
        from custom_components.meteoswiss_weather.ogd.hourly import horizon_end_utc

        end = horizon_end_utc(horizon_days, datetime.now(UTC))
        return [
            self._hour("", h)
            for h in range(72)
            if end is None or _BASE + timedelta(hours=h) < end
        ]


async def test_near_tier_never_overshoots_configured_horizon(
    hass: HomeAssistant,
) -> None:
    """A near-only fallback refresh must not leak hours past a narrowed horizon."""
    backend = _TrimmingCanaryBackend()
    refresher, store = _make_refresher(hass, backend, horizon_days=0)
    start = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)
    with freeze_time(start) as frozen:
        await refresher.async_refresh(_run(start))
        after_far = hourly_from_store(store, refresher.demanded_params)

        # Same run, past the near fallback: the near tier refetches, capped.
        frozen.move_to(start + HOURLY_NEAR_MAX_AGE + timedelta(seconds=1))
        await refresher.async_refresh(_run(start))
        after_near = hourly_from_store(store, refresher.demanded_params)

    assert len(after_near) == len(after_far)
    assert all(h.symbol is not None for h in after_near)
    near_calls = [c for c in backend.calls if c[0] == tuple(HOURLY_DATE_MAJOR_PARAMS)]
    assert near_calls[-1][1] == 0  # the capped horizon, not the near reach of 2
    # A narrowed horizon (0) never reaches beyond the near window, so it never
    # triggers a far-remainder fetch.
    assert not refresher._has_far_remainder
    assert all(_tier_of(c) != "far" for c in backend.calls)


# ---------------------------------------------------------------------------
# Option off / no run
# ---------------------------------------------------------------------------


async def test_refresher_disabled_never_fetches(hass: HomeAssistant) -> None:
    """With the option off the refresher demands nothing and never fetches."""
    backend = _CanaryBackend()
    store = ForecastStore()
    refresher = HourlyRefresher(
        hass, backend, _POINT, store, enabled=False, horizon_days=_HORIZON_DAYS
    )
    run = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)
    with freeze_time(run):
        assert await refresher.async_refresh(_run(run)) is False
    assert backend.calls == []
    assert backend.canary_calls == []
    assert refresher.demanded_params == ()


async def test_refresher_none_run_returns_false(hass: HomeAssistant) -> None:
    """No discovered run yet means nothing to fetch."""
    backend = _CanaryBackend()
    refresher, _store = _make_refresher(hass, backend)
    assert await refresher.async_refresh(None) is False
    assert backend.calls == []


# ---------------------------------------------------------------------------
# B9/B11 per-entity gating of the date-major additions (issue #69)
# ---------------------------------------------------------------------------


class _GatedCanaryBackend(_CanaryBackend):
    """Fills the gated date-major fields when asked."""

    async def fetch_hourly(
        self, point, *, horizon_days=-1, params=(), run=None,
        window_start_override=None,
    ):
        self.calls.append((tuple(params), horizon_days, window_start_override))
        want = set(params)
        return [
            HourlyForecast(
                time=_BASE + timedelta(hours=h),
                temperature=self._value("temperature", h),
                precipitation=0.0,
                symbol=1,
                wind_speed_kmh=self._value("wind_speed_kmh", h),
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
        for params, _horizon, _ws in backend.calls
        if params != tuple(HOURLY_POINT_MAJOR_PARAMS)
    ]


async def test_refresher_default_fetches_no_gated_files(hass: HomeAssistant) -> None:
    """With neither gated option on, the date-major fetch is temperature only."""
    backend = _GatedCanaryBackend()
    refresher, _store = _make_refresher(hass, backend)
    run = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    with freeze_time(run):
        await refresher.async_refresh(_run(run))

    for params in _date_major_calls(backend):
        assert params == tuple(HOURLY_DATE_MAJOR_PARAMS)


async def test_refresher_cloud_option_fetches_and_files_layers(
    hass: HomeAssistant,
) -> None:
    """With cloud layers on, the date-major fetch adds the three cloud files."""
    backend = _GatedCanaryBackend()
    refresher, store = _make_refresher(hass, backend, cloud_layers=True)
    run = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
    with freeze_time(run):
        await refresher.async_refresh(_run(run))

    expected = hourly_date_major_params(cloud_layers=True)
    for params in _date_major_calls(backend):
        assert params == expected
    hourly = hourly_from_store(store, refresher.demanded_params)
    assert hourly[0].cloud_high == 20.0
    assert hourly[0].cloud_mid == 40.0
    assert hourly[0].cloud_low == 10.0
    assert hourly[0].temperature_p10 is None


async def test_refresher_percentile_option_fetches_and_files_band(
    hass: HomeAssistant,
) -> None:
    """With percentiles on, the date-major fetch adds the p10/p90 files only."""
    backend = _GatedCanaryBackend()
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
    """An hour with all required fields is emitted even if optionals are absent."""
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
    """An hour only in the temperature series (no point-major) is dropped."""
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


class _ZeroDegreeBackend(_CanaryBackend):
    """Returns hours that carry a zero-degree level and records the run it got."""

    def __init__(self) -> None:
        super().__init__()
        self.runs: list = []

    async def fetch_hourly(
        self, point, *, horizon_days=-1, params=(), run=None,
        window_start_override=None,
    ):
        self.runs.append(run)
        self.calls.append((tuple(params), horizon_days, window_start_override))
        return [
            HourlyForecast(
                time=_BASE + timedelta(hours=h),
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

    # Same run again: nothing new (no new run means no canary, no fetch).
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
