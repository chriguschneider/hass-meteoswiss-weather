"""Data models and exceptions for the OGD client.

Plain dataclasses, no Home Assistant imports (ADR-0001). ``None`` marks a
field the upstream file left empty; the client never invents a value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


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


@dataclass(frozen=True, slots=True)
class ForecastPoint:
    """A local-forecast point (``ch.meteoschweiz.ogd-local-forecasting``).

    Only ``(point_id, point_type_id)`` is unique (docs/ogd.md §E4): a postal
    code can carry several points, so both fields identify a forecast row.
    """

    point_id: int
    point_type_id: int
    postal_code: str
    name: str
    lat: float
    lon: float
    height_masl: float | None


@dataclass(frozen=True, slots=True)
class DailyForecast:
    """One day of the 9-day local forecast for a point.

    Every measured field is optional: a parameter file that omitted the day,
    or left the cell empty, yields ``None`` rather than an invented value.
    """

    date: date
    temp_max: float | None = None
    temp_min: float | None = None
    precipitation: float | None = None
    precipitation_probability: float | None = None
    symbol: int | None = None


@dataclass(frozen=True, slots=True)
class HourlyForecast:
    """One hour of the local forecast for a point.

    Placeholder for the hourly backend built in #10; declared here so the
    :class:`ForecastBackend` protocol can name its return type. Fields will
    grow with the hourly parameter set (ADR-0002).
    """

    time: datetime
    temperature: float | None = None
    precipitation: float | None = None
    symbol: int | None = None
