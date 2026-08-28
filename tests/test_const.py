"""Constant invariants that the ADRs pin down.

Stdlib-only (no ``hass`` fixture): these guard the traffic-budget cadences
(ADR-0002) so a future edit that shortens them fails loudly here.
"""

from __future__ import annotations

from datetime import timedelta

from custom_components.meteoswiss_weather.const import (
    FORECAST_CHECK_INTERVAL,
    HOURLY_FAR_MAX_AGE,
    HOURLY_FAR_RUN_HOURS,
    HOURLY_NEAR_HORIZON_DAYS,
    HOURLY_NEAR_MAX_AGE,
    HOURLY_NEAR_RUN_HOURS,
    STATION_UPDATE_INTERVAL,
)
from custom_components.meteoswiss_weather.ogd.const import (
    DAILY_REQUIRED_PARAMS,
    DAILY_SYMBOL,
    HOURLY_CLOUD_PARAMS,
    HOURLY_DATE_MAJOR_PARAMS,
    HOURLY_POINT_MAJOR_PARAMS,
    HOURLY_REQUIRED_PARAMS,
    HOURLY_TEMP_PERCENTILE_PARAMS,
    hourly_date_major_params,
)


def test_hourly_tier_landing_hours_match_measured_rhythm() -> None:
    """ADR-0002 rev. 2: the near/far tiers land on the measured run hours.

    Measured 2026-08-27 (docs/ogd.md, "Change rhythm across runs"): the near
    term (today + tomorrow) moves at the ICON-CH1 runs every 3 h, days 2+ at the
    ICON-CH2 runs every 6 h. The far hours must be a subset of the near hours so
    a far fetch always doubles as a near refresh.
    """
    assert HOURLY_NEAR_RUN_HOURS == frozenset({2, 5, 8, 11, 14, 17, 20, 23})
    assert HOURLY_FAR_RUN_HOURS == frozenset({5, 11, 17, 23})
    assert HOURLY_FAR_RUN_HOURS <= HOURLY_NEAR_RUN_HOURS


def test_hourly_tier_staleness_fallbacks() -> None:
    """The near tier's fallback is tighter than the far tier's, both bounded.

    The near term matters most, so it is never allowed to go stale longer than
    3 h; the far range refreshes at most every 6 h. Neither may drop below the
    old flat 3 h floor the tiers replaced.
    """
    assert HOURLY_NEAR_MAX_AGE == timedelta(hours=3)
    assert HOURLY_FAR_MAX_AGE == timedelta(hours=6)
    assert HOURLY_NEAR_MAX_AGE >= timedelta(hours=3)
    assert HOURLY_FAR_MAX_AGE >= HOURLY_NEAR_MAX_AGE
    # The near tier covers today + tomorrow (one full local day beyond today).
    assert HOURLY_NEAR_HORIZON_DAYS == 1


def test_hourly_param_groups_partition_the_required_set() -> None:
    """The date-major and point-major groups partition HOURLY_REQUIRED_PARAMS.

    The tiered fetch (issue #68) schedules the two groups separately, so between
    them they must cover every required parameter exactly once — otherwise a
    parameter would be fetched twice or not at all.
    """
    assert not set(HOURLY_DATE_MAJOR_PARAMS) & set(HOURLY_POINT_MAJOR_PARAMS)
    assert set(HOURLY_DATE_MAJOR_PARAMS) | set(HOURLY_POINT_MAJOR_PARAMS) == set(
        HOURLY_REQUIRED_PARAMS
    )


def test_fetch_set_registry_fetches_nothing_extra_by_default() -> None:
    """Issue #69: with no gated option on, only the base date-major set fetches.

    The B9 cloud and B11 percentile files are date-major and expensive (a
    horizon prefix each), so they must never be fetched unless their option is
    enabled. With both off the registry returns exactly the base set.
    """
    assert hourly_date_major_params() == HOURLY_DATE_MAJOR_PARAMS
    assert (
        hourly_date_major_params(cloud_layers=False, temp_percentiles=False)
        == HOURLY_DATE_MAJOR_PARAMS
    )
    # None of the extra files leak into the default set.
    default = set(hourly_date_major_params())
    assert not default & set(HOURLY_CLOUD_PARAMS)
    assert not default & set(HOURLY_TEMP_PERCENTILE_PARAMS)


def test_fetch_set_registry_adds_only_the_enabled_files() -> None:
    """Each option adds exactly its own files, and both together add both sets."""
    base = set(HOURLY_DATE_MAJOR_PARAMS)

    clouds = set(hourly_date_major_params(cloud_layers=True))
    assert clouds == base | set(HOURLY_CLOUD_PARAMS)
    assert not clouds & set(HOURLY_TEMP_PERCENTILE_PARAMS)

    percentiles = set(hourly_date_major_params(temp_percentiles=True))
    assert percentiles == base | set(HOURLY_TEMP_PERCENTILE_PARAMS)
    assert not percentiles & set(HOURLY_CLOUD_PARAMS)

    both = set(hourly_date_major_params(cloud_layers=True, temp_percentiles=True))
    assert both == base | set(HOURLY_CLOUD_PARAMS) | set(HOURLY_TEMP_PERCENTILE_PARAMS)


def test_gated_files_are_disjoint_from_the_point_major_group() -> None:
    """The B9/B11 files are date-major, never part of the point-major group.

    Guards the layout assumption behind their cost: they are fetched as horizon
    prefixes on the near/far schedule, not as cheap point blocks.
    """
    gated = set(HOURLY_CLOUD_PARAMS) | set(HOURLY_TEMP_PERCENTILE_PARAMS)
    assert not gated & set(HOURLY_POINT_MAJOR_PARAMS)
    # They are also disjoint from the always-on required set (opt-in additions).
    assert not gated & set(HOURLY_REQUIRED_PARAMS)


def test_station_and_forecast_intervals() -> None:
    """Station polls every 10 min; the forecast run is checked hourly."""
    assert STATION_UPDATE_INTERVAL == timedelta(minutes=10)
    assert FORECAST_CHECK_INTERVAL == timedelta(hours=1)


def test_daily_params_are_the_all_point_p_variants() -> None:
    """Issue #34: the daily forecast must use the ``p``-variant files.

    The ``d``/``0``-variants (``tre200dx``/``tre200dn``/``rka150d0``) are
    aggregated over the UTC day and published **for stations only** — the
    default postal-code point (type 2) has no rows in them, so the daily
    forecast silently loses its temperatures and precipitation. The
    ``p``-variants cover every point type. Guard the codes so the
    station-only files can never be reintroduced. The symbol file
    ``jp2000d0`` legitimately ends in ``d0`` and covers all types, so it is
    the one exception.
    """
    for param in DAILY_REQUIRED_PARAMS:
        if param == DAILY_SYMBOL:
            continue
        assert not param.endswith(("dx", "dn", "d0")), (
            f"{param} is a station-only daily file; use its p-variant (issue #34)"
        )
