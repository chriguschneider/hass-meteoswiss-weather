"""Data models and exceptions for the SLF IMIS client (ADR-0009).

Plain dataclasses, no Home Assistant imports. ``None`` marks a field the
upstream response left null; the client never invents a value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


class SlfError(Exception):
    """Base class for every error this package raises."""


class SlfConnectionError(SlfError):
    """The upstream endpoint could not be fetched (network error, 4xx/5xx)."""


class ImisNoDataError(SlfConnectionError):
    """The station has no data in the requested period (HTTP 404 NO_DATA).

    Subclass of SlfConnectionError so existing ``except SlfConnectionError``
    callers keep working unchanged (ADR-0009 amendment, issue #186).
    """


class SlfParseError(SlfError):
    """The upstream response was fetched but could not be understood."""


@dataclass(frozen=True, slots=True)
class ImisStation:
    """One IMIS station from ``/public/api/imis/stations``.

    ``type`` is one of ``SNOW_FLAT``, ``WIND``, ``FLOWCAPT``,
    ``SNOW_SLOPE`` (as of the 2026-10-19 measurement; docs/slf.md).
    Only ``SNOW_FLAT`` stations have a meaningful snow height on a flat
    study plot.
    """

    code: str
    label: str
    lat: float
    lon: float
    elevation: float
    canton: str
    type: str


@dataclass(frozen=True, slots=True)
class ImisMeasurement:
    """One 30-minute row from ``/station/{code}/measurements``.

    ``time`` is an aware UTC datetime. Every numeric field is optional:
    a ``WIND`` station returns ``HS: null``; any field the API left null
    becomes ``None`` here.
    """

    time: datetime
    snow_height_cm: float | None = None
    air_temperature: float | None = None
    humidity: float | None = None
    wind_speed_ms: float | None = None
    gust_ms: float | None = None
    wind_bearing: float | None = None
    wind_bearing_sd: float | None = None
    snow_surface_temperature: float | None = None
    snow_temperature_0cm: float | None = None
    snow_temperature_25cm: float | None = None
    snow_temperature_50cm: float | None = None
    snow_temperature_100cm: float | None = None
    reflected_shortwave_radiation: float | None = None


@dataclass(frozen=True, slots=True)
class ImisDailySnow:
    """One row from ``/public/api/imis/daily-snow`` (SNOWPACK-modelled values).

    ``date`` is the UTC date (``measure_date`` parsed as UTC; the upstream
    field carries no timezone suffix — docs/slf.md). Both numeric fields are
    optional: some stations have a row but all-null values (e.g. ``BOG4``),
    meaning "unknown" rather than zero.
    """

    date: date
    snow_height_cm: float | None = None
    new_snow_24h_cm: float | None = None
