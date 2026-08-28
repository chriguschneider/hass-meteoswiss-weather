"""Data models and exceptions for the OGD client.

Plain dataclasses, no Home Assistant imports (ADR-0001). ``None`` marks a
field the upstream file left empty; the client never invents a value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


class FileLayout(Enum):
    """How the rows of an hourly bulk file are sorted (issue #50).

    Detected at runtime from byte-offset probes; picks the Range strategy:
    a date-major file is fetched as a horizon prefix, a point-major file has
    the point's contiguous block located by binary search, and anything
    unexpected falls back to the full download.
    """

    # Sorted by Date first: the earliest hours of all points lead the file.
    DATE_MAJOR = "date_major"
    # Sorted by (point_type_id, point_id, Date): one point's rows are a block.
    POINT_MAJOR_TYPE = "point_major_type"
    # Sorted by (point_id, Date), types mixed: one point_id's rows are a block.
    POINT_MAJOR_ID = "point_major_id"
    # Unrecognised order: fetch the whole file and parse it (safe default).
    FALLBACK = "fallback"


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
    # B1
    snow_depth: float | None = None
    # B2
    wind_chill: float | None = None
    pressure_qnh: float | None = None
    # B3
    soil_temp_5cm: float | None = None
    soil_temp_10cm: float | None = None
    soil_temp_20cm: float | None = None
    # B4
    air_temp_5cm: float | None = None
    # B5
    diffuse_radiation: float | None = None
    longwave_radiation: float | None = None


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

    ``time`` is an aware UTC datetime at the top of the hour. Every measured
    field is optional: a parameter file that omitted the hour, or left the
    cell empty, yields ``None`` rather than an invented value. The fields
    mirror the opt-in hourly parameter set (ADR-0002): temperature,
    precipitation, symbol, wind speed, gust and bearing.
    """

    time: datetime
    temperature: float | None = None
    precipitation: float | None = None
    symbol: int | None = None
    wind_speed_kmh: float | None = None
    gust_kmh: float | None = None
    wind_bearing: float | None = None


@dataclass(frozen=True, slots=True)
class HourlyHistoryRow:
    """One hour of a station's measured history (ADR-0007, issue #51).

    ``ts_utc`` is the ``reference_timestamp`` of the upstream row, UTC.
    Every other field is optional: an empty cell yields ``None`` rather than
    an invented value. The hourly mean/min/max temperatures match the shape
    of a Home Assistant long-term statistics row.
    """

    ts_utc: datetime
    temp_mean: float | None = None
    temp_min: float | None = None
    temp_max: float | None = None
    humidity: float | None = None
    dew_point: float | None = None
    pressure_qff: float | None = None
    wind_speed_kmh: float | None = None
    gust_kmh: float | None = None
    precipitation_sum: float | None = None
    sunshine: float | None = None
    global_radiation: float | None = None
