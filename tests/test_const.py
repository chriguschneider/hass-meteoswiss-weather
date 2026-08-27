"""Constant invariants that the ADRs pin down.

Stdlib-only (no ``hass`` fixture): these guard the traffic-budget cadences
(ADR-0002) so a future edit that shortens them fails loudly here.
"""

from __future__ import annotations

from datetime import timedelta

from custom_components.meteoswiss_weather.const import (
    FORECAST_CHECK_INTERVAL,
    HOURLY_FORECAST_MIN_INTERVAL,
    STATION_UPDATE_INTERVAL,
)
from custom_components.meteoswiss_weather.ogd.const import (
    DAILY_REQUIRED_PARAMS,
    DAILY_SYMBOL,
)


def test_hourly_forecast_min_interval_at_least_three_hours() -> None:
    """ADR-0002: the opt-in hourly forecast is never fetched faster than 3 h."""
    assert HOURLY_FORECAST_MIN_INTERVAL >= timedelta(hours=3)


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
