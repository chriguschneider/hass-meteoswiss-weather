"""Data models and exceptions for the OGD client.

Plain dataclasses, no Home Assistant imports (ADR-0001). ``None`` marks a
field the upstream file left empty; the client never invents a value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class OgdError(Exception):
    """Base class for every error this package raises."""


class OgdConnectionError(OgdError):
    """The upstream file could not be fetched (network error, 4xx/5xx)."""


class OgdParseError(OgdError):
    """The upstream file was fetched but could not be understood."""


@dataclass(frozen=True, slots=True)
class Station:
    """A SwissMetNet automatic weather station."""

    abbr: str
    name: str
    canton: str
    lat: float
    lon: float
    height_masl: float | None


@dataclass(frozen=True, slots=True)
class Observation:
    """The latest 10-minute measurement from one station.

    Every measured field is optional: a station that does not carry a
    parameter, or a row that left it empty, yields ``None`` (docs/ogd.md).
    """

    station_abbr: str
    timestamp: datetime
    temperature: float | None = None
    humidity: float | None = None
    dew_point: float | None = None
    pressure_qff: float | None = None
    pressure_qfe: float | None = None
    wind_speed_kmh: float | None = None
    wind_bearing: float | None = None
    gust_kmh: float | None = None
    precipitation_10min: float | None = None
    sunshine_10min: float | None = None
    global_radiation: float | None = None
