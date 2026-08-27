"""Diagnostics support for MeteoSwiss Weather (issue #15).

Exposes config entry data, coordinator state and the cached HTTP validators.
The postal code is location-identifying, so the ``TO_REDACT`` set removes it
via :func:`~homeassistant.components.diagnostics.async_redact_data`.

Exponential back-off for transient connection errors is handled by
:class:`~homeassistant.helpers.update_coordinator.DataUpdateCoordinator`
automatically (see coordinator.py); no separate diagnostics entry is needed.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import MeteoSwissConfigEntry

# Keys whose values reveal the user's location and must not appear in reports.
_TO_REDACT = {"postal_code", "lat", "lon"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MeteoSwissConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data
    sc = runtime.station_coordinator
    fc = runtime.forecast_coordinator

    return async_redact_data(
        {
            "entry": {
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            "point": {
                "point_id": runtime.point.point_id,
                "point_type_id": runtime.point.point_type_id,
                "postal_code": runtime.point.postal_code,
                "name": runtime.point.name,
                "lat": runtime.point.lat,
                "lon": runtime.point.lon,
                "height_masl": runtime.point.height_masl,
            },
            "station_abbr": runtime.station_abbr,
            "station_coordinator": {
                "last_update_success": sc.last_update_success,
                "last_success": (
                    sc.last_success.isoformat() if sc.last_success else None
                ),
                "last_exception": (
                    str(sc.last_exception) if sc.last_exception else None
                ),
                "cache_etag": sc._cache.etag,
                "cache_last_modified": sc._cache.last_modified,
            },
            "forecast_coordinator": {
                "last_update_success": fc.last_update_success,
                "last_success": (
                    fc.last_success.isoformat() if fc.last_success else None
                ),
                "last_exception": (
                    str(fc.last_exception) if fc.last_exception else None
                ),
                "last_run": fc.last_run.isoformat() if fc.last_run else None,
            },
        },
        _TO_REDACT,
    )
