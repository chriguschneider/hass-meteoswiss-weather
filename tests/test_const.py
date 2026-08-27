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


def test_hourly_forecast_min_interval_at_least_three_hours() -> None:
    """ADR-0002: the opt-in hourly forecast is never fetched faster than 3 h."""
    assert HOURLY_FORECAST_MIN_INTERVAL >= timedelta(hours=3)


def test_station_and_forecast_intervals() -> None:
    """Station polls every 10 min; the forecast run is checked hourly."""
    assert STATION_UPDATE_INTERVAL == timedelta(minutes=10)
    assert FORECAST_CHECK_INTERVAL == timedelta(hours=1)
