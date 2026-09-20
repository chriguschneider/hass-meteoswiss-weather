"""Unit tests for the options traffic estimator (issue #145). No Home Assistant.

The estimator is pure bookkeeping — options → the demanded files → bytes — so it
is unit-tested as such, next to the demand registry it derives from.
"""

from __future__ import annotations

from custom_components.meteoswiss_weather.demand import (
    KIND_HOURLY_DATE_MAJOR,
    KIND_ZERO_DEGREE,
    REFRESHES_PER_DAY,
    estimate_traffic,
)
from custom_components.meteoswiss_weather.ogd.const import (
    DAILY_REQUIRED_PARAMS,
    HOURLY_CLOUD_PARAMS,
    HOURLY_HORIZON_FULL_RUN,
    HOURLY_TEMP_PERCENTILE_PARAMS,
    HOURLY_TEMPERATURE,
    HOURLY_ZERO_DEGREE,
)


def _params(estimate) -> set[str]:
    return {f.param for f in estimate.files}


def _by_param(estimate) -> dict:
    return {f.param: f for f in estimate.files}


def test_hourly_off_is_only_the_daily_baseline() -> None:
    """With hourly off nothing hourly-only is demanded (temperature file absent)."""
    est = estimate_traffic(hourly=False, horizon_days=2)
    params = _params(est)
    assert set(DAILY_REQUIRED_PARAMS) <= params
    # The zero-degree block rides the daily refresh, so it is in the baseline.
    assert HOURLY_ZERO_DEGREE in params
    # The date-major temperature file and cloud/percentile files are hourly-only.
    assert HOURLY_TEMPERATURE not in params
    assert not (set(HOURLY_CLOUD_PARAMS) & params)
    # Nothing measured, so the whole estimate is from the table.
    assert not est.any_measured
    assert not est.all_measured
    assert not est.has_unbounded


def test_zero_degree_has_its_own_kind() -> None:
    """``zprfr0hs`` is priced as its own kind, not a plain point-major block."""
    est = estimate_traffic(hourly=True, horizon_days=2)
    assert _by_param(est)[HOURLY_ZERO_DEGREE].kind == KIND_ZERO_DEGREE


def test_hourly_on_adds_the_date_major_temperature_file() -> None:
    """Turning hourly on demands the date-major temperature file at ~250 KB/72 h."""
    off = estimate_traffic(hourly=False, horizon_days=2)
    on = estimate_traffic(hourly=True, horizon_days=2)
    assert HOURLY_TEMPERATURE not in _params(off)
    temp = _by_param(on)[HOURLY_TEMPERATURE]
    assert temp.kind == KIND_HOURLY_DATE_MAJOR
    # The default horizon (~72 h) is the table anchor.
    assert temp.bytes == 250_000
    assert on.bytes_per_refresh > off.bytes_per_refresh


def test_date_major_scales_with_horizon() -> None:
    """A shorter horizon reads fewer date-major rows; a longer one more."""
    short = _by_param(estimate_traffic(hourly=True, horizon_days=0))
    default = _by_param(estimate_traffic(hourly=True, horizon_days=2))
    assert short[HOURLY_TEMPERATURE].bytes < default[HOURLY_TEMPERATURE].bytes


def test_cloud_and_percentiles_add_their_date_major_files() -> None:
    """Each gated option adds its own date-major files to the plan (issue #69)."""
    base = _params(estimate_traffic(hourly=True, horizon_days=2))
    with_cloud = _params(
        estimate_traffic(hourly=True, horizon_days=2, cloud_layers=True)
    )
    with_pct = _params(
        estimate_traffic(hourly=True, horizon_days=2, temp_percentiles=True)
    )
    assert set(HOURLY_CLOUD_PARAMS) <= with_cloud
    assert not (set(HOURLY_CLOUD_PARAMS) & base)
    assert set(HOURLY_TEMP_PERCENTILE_PARAMS) <= with_pct


def test_per_day_is_per_refresh_times_the_refresh_count() -> None:
    est = estimate_traffic(hourly=True, horizon_days=2)
    assert est.refreshes_per_day == REFRESHES_PER_DAY
    assert est.bytes_per_day == est.bytes_per_refresh * REFRESHES_PER_DAY


def test_long_horizon_date_major_files_are_unbounded() -> None:
    """A horizon past the row-addressing cap has no fixed date-major number."""
    est = estimate_traffic(hourly=True, horizon_days=HOURLY_HORIZON_FULL_RUN)
    temp = _by_param(est)[HOURLY_TEMPERATURE]
    assert temp.unbounded
    assert temp.bytes == 0
    assert est.has_unbounded
    # The point-major/daily files still carry fixed numbers.
    assert est.bytes_per_refresh > 0


def test_measured_beats_estimated_for_active_files() -> None:
    """A file's last measured fetch overrides the table figure (issue #145)."""
    measured = {HOURLY_TEMPERATURE: 12_345}
    est = estimate_traffic(
        hourly=True, horizon_days=2, measured_bytes=measured
    )
    temp = _by_param(est)[HOURLY_TEMPERATURE]
    assert temp.measured
    assert temp.bytes == 12_345
    assert est.any_measured
    assert not est.all_measured


def test_measured_beats_estimated_even_past_the_cap() -> None:
    """A measured fetch is a real number, so it is never treated as unbounded."""
    measured = {HOURLY_TEMPERATURE: 9_000_000}
    est = estimate_traffic(
        hourly=True,
        horizon_days=HOURLY_HORIZON_FULL_RUN,
        measured_bytes=measured,
    )
    temp = _by_param(est)[HOURLY_TEMPERATURE]
    assert temp.measured
    assert not temp.unbounded
    assert temp.bytes == 9_000_000


def test_all_measured_when_every_file_has_a_measurement() -> None:
    """When every demanded file has measured bytes the estimate reads measured."""
    plan = estimate_traffic(hourly=True, horizon_days=2)
    measured = {f.param: 1_000 for f in plan.files}
    est = estimate_traffic(hourly=True, horizon_days=2, measured_bytes=measured)
    assert est.all_measured
    assert est.bytes_per_refresh == 1_000 * len(est.files)
