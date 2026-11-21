"""Scaffolding shared by the sensor and weather platforms."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN
from .ogd import ForecastPoint


def point_device_unique_id(point: ForecastPoint) -> str:
    """Return the device identifier (and weather unique id) of a forecast point."""
    return f"{point.point_type_id}-{point.point_id}"


def point_device_info(point: ForecastPoint) -> DeviceInfo:
    """Return the device of a forecast point, shared by every platform."""
    return DeviceInfo(
        identifiers={(DOMAIN, point_device_unique_id(point))},
        name=point.name,
        manufacturer="MeteoSwiss",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://opendatadocs.meteoswiss.ch",
    )
