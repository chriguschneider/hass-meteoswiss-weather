"""Local forecast: resolve a postal code to a point, parse the daily files.

Reads the official ``ch.meteoschweiz.ogd-local-forecasting`` files (ADR-0001).
Columns are addressed by header, never by position (docs/ogd.md §E4). The
daily parameter files are small (order of 5 MB together); the hourly files are
the whole traffic budget and live behind the backend seam (ADR-0002).
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import aiohttp

from .const import (
    CSV_SEPARATOR,
    DAILY_PRECIPITATION,
    DAILY_SYMBOL,
    DAILY_TEMP_MAX,
    DAILY_TEMP_MIN,
    FORECAST_ENCODING,
    FORECAST_TIMEZONE,
    HOURLY_CLOUD_HIGH,
    HOURLY_CLOUD_LOW,
    HOURLY_CLOUD_MID,
    HOURLY_GUST,
    HOURLY_PRECIP_PROBABILITY,
    HOURLY_PRECIPITATION,
    HOURLY_RADIATION,
    HOURLY_SYMBOL,
    HOURLY_TEMP_P10,
    HOURLY_TEMP_P90,
    HOURLY_TEMPERATURE,
    HOURLY_WIND_DIRECTION,
    HOURLY_WIND_SPEED,
    HOURLY_ZERO_DEGREE,
    META_POINT_URL,
    POINT_TYPE_MOUNTAIN,
    POINT_TYPE_POSTAL_CODE,
)
from .geo import haversine_km
from .http import get_text
from .models import DailyForecast, ForecastPoint, HourlyForecast, OgdParseError

# Header columns of ogd-local-forecasting_meta_point.csv (docs/ogd.md §E4).
_POINT_ID = "point_id"
_POINT_TYPE_ID = "point_type_id"
_POSTAL_CODE = "postal_code"
_POINT_NAME = "point_name"
_HEIGHT = "point_height_masl"
_LAT = "point_coordinates_wgs84_lat"
_LON = "point_coordinates_wgs84_lon"

# Columns every daily data file shares, before its one parameter column.
_DATA_POINT_ID = "point_id"
_DATA_POINT_TYPE_ID = "point_type_id"
_DATA_DATE = "Date"

# Daily parameter code -> DailyForecast field. ``precipitation_probability`` is
# deliberately absent: the probability column's code is not confirmed against
# ogd-local-forecasting_meta_parameters.csv yet, so the client never guesses it
# (docs/ogd.md §E4). Add it here once the meta CSV pins the code down.
_DAILY_FIELDS: dict[str, str] = {
    DAILY_TEMP_MAX: "temp_max",
    DAILY_TEMP_MIN: "temp_min",
    DAILY_PRECIPITATION: "precipitation",
    DAILY_SYMBOL: "symbol",
}

# Hourly parameter code -> HourlyForecast field (ADR-0002 minimum set plus the
# gust and wind-direction files the entity exposes, the B7/B8/B10 additions of
# issue #55, and the B9/B11 gated additions of issue #69). ``symbol`` is the
# integer icon code; the rest are floats. A parameter absent from the fetched
# text (because its option is off) is simply not merged — the field stays None.
_HOURLY_FIELDS: dict[str, str] = {
    HOURLY_TEMPERATURE: "temperature",
    HOURLY_PRECIPITATION: "precipitation",
    HOURLY_SYMBOL: "symbol",
    HOURLY_WIND_SPEED: "wind_speed_kmh",
    HOURLY_GUST: "gust_kmh",
    HOURLY_WIND_DIRECTION: "wind_bearing",
    HOURLY_PRECIP_PROBABILITY: "precipitation_probability",
    HOURLY_ZERO_DEGREE: "zero_degree_level",
    HOURLY_RADIATION: "radiation",
    # B9 cloud cover (three date-major layers) and B11 temperature percentiles
    # (two date-major files), fetched only behind their opt-in (issue #69).
    HOURLY_CLOUD_HIGH: "cloud_high",
    HOURLY_CLOUD_MID: "cloud_mid",
    HOURLY_CLOUD_LOW: "cloud_low",
    HOURLY_TEMP_P10: "temperature_p10",
    HOURLY_TEMP_P90: "temperature_p90",
}


def _reader(body: str) -> csv.DictReader:
    return csv.DictReader(io.StringIO(body), delimiter=CSV_SEPARATOR)


def _to_float(value: str | None) -> float | None:
    """Parse a numeric cell; empty means "not forecast", not zero."""
    if value is None or value.strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _to_int(value: str | None) -> int | None:
    """Parse an integer cell (point/type ids, symbol codes)."""
    number = _to_float(value)
    return int(number) if number is not None else None


async def fetch_points(session: aiohttp.ClientSession) -> list[ForecastPoint]:
    """Fetch and parse the local-forecast point metadata (download once)."""
    response = await get_text(session, META_POINT_URL, encoding=FORECAST_ENCODING)
    reader = _reader(response.body)
    if reader.fieldnames is None or _POINT_ID not in reader.fieldnames:
        raise OgdParseError("forecast point metadata is missing its header")

    points: list[ForecastPoint] = []
    for row in reader:
        point_id = _to_int(row.get(_POINT_ID))
        point_type_id = _to_int(row.get(_POINT_TYPE_ID))
        lat = _to_float(row.get(_LAT))
        lon = _to_float(row.get(_LON))
        # A point without an identity or coordinates cannot be used or ranked.
        if point_id is None or point_type_id is None or lat is None or lon is None:
            continue
        points.append(
            ForecastPoint(
                point_id=point_id,
                point_type_id=point_type_id,
                postal_code=(row.get(_POSTAL_CODE) or "").strip(),
                name=(row.get(_POINT_NAME) or "").strip(),
                lat=lat,
                lon=lon,
                height_masl=_to_float(row.get(_HEIGHT)),
            )
        )

    if not points:
        raise OgdParseError("forecast point metadata contained no usable points")
    return points


def points_for_postal_code(
    points: list[ForecastPoint], plz: int | str
) -> list[ForecastPoint]:
    """Postal-code-centre points (type 2) for ``plz``, ``PLZ*100+00`` first.

    Sorted by ``point_id`` ascending, so the ``n = 00`` centre — the default
    the config flow offers first — leads and the extra points follow.
    """
    wanted = str(plz).strip()
    matches = [
        p
        for p in points
        if p.point_type_id == POINT_TYPE_POSTAL_CODE and p.postal_code == wanted
    ]
    return sorted(matches, key=lambda p: p.point_id)


def mountain_points(points: list[ForecastPoint]) -> list[ForecastPoint]:
    """All type-3 (mountain point of interest) points, sorted by name."""
    return sorted(
        [p for p in points if p.point_type_id == POINT_TYPE_MOUNTAIN],
        key=lambda p: p.name,
    )


def nearest_point(
    points: list[ForecastPoint],
    lat: float,
    lon: float,
    *,
    point_type: int = POINT_TYPE_POSTAL_CODE,
) -> ForecastPoint:
    """The ``point_type`` point closest to ``lat``/``lon`` (haversine)."""
    candidates = [p for p in points if p.point_type_id == point_type]
    if not candidates:
        raise OgdParseError(f"no forecast point of type {point_type} to choose from")
    return min(candidates, key=lambda p: haversine_km(lat, lon, p.lat, p.lon))


def _parse_date(value: str | None) -> date | None:
    """Parse a daily ``Date`` cell (``YYYYMMDDHHMM`` UTC) into its date."""
    if value is None:
        return None
    digits = value.strip()
    if len(digits) < 8 or not digits[:8].isdigit():
        return None
    return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))


def parse_daily(
    text_by_param: dict[str, str], point: ForecastPoint
) -> list[DailyForecast]:
    """Merge the daily parameter files into one 9-day forecast for ``point``.

    A **plain function** so the backend can hand it to an executor (ADR-0002).
    Each file holds all ~5,600 points; only the rows matching ``point`` are
    kept while iterating, so no file is ever materialised as a list of rows.
    Order-independent: rows are keyed by date, then sorted at the end.
    """
    by_date: dict[date, dict[str, float | int | None]] = {}

    for param, text in text_by_param.items():
        field = _DAILY_FIELDS.get(param)
        if field is None:
            continue  # a parameter this forecast does not model
        cast = _to_int if field == "symbol" else _to_float
        for row in _reader(text):
            if _to_int(row.get(_DATA_POINT_ID)) != point.point_id:
                continue
            if _to_int(row.get(_DATA_POINT_TYPE_ID)) != point.point_type_id:
                continue
            day = _parse_date(row.get(_DATA_DATE))
            if day is None:
                continue
            by_date.setdefault(day, {})[field] = cast(row.get(param))

    return [
        DailyForecast(date=day, **values)
        for day, values in sorted(by_date.items())
    ]


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an hourly ``Date`` cell (``YYYYMMDDHHMM`` UTC) into an aware dt."""
    if value is None:
        return None
    digits = value.strip()
    if len(digits) < 12 or not digits[:12].isdigit():
        return None
    return datetime(
        int(digits[:4]),
        int(digits[4:6]),
        int(digits[6:8]),
        int(digits[8:10]),
        int(digits[10:12]),
        tzinfo=UTC,
    )


def parse_hourly(
    text_by_param: dict[str, str],
    point: ForecastPoint,
    horizon_end: datetime | None = None,
    horizon_start: datetime | None = None,
) -> list[HourlyForecast]:
    """Merge the hourly parameter files into one forecast for ``point``.

    A **plain function** so the backend can hand it to an executor (ADR-0002).
    Each file holds every point's ~220 rows; only the rows matching ``point``
    are kept while iterating, so the file is never materialised as a list of
    rows. Order-independent: rows are keyed by timestamp, then sorted.

    ``horizon_end`` (an aware UTC datetime) trims the forecast to hours **before**
    it, giving one consistent horizon across all files (issue #50): the
    date-major files are already Range-limited to it, and the point-major files
    — always fetched as the point's whole run — are trimmed here. ``None`` keeps
    every hour (the "full run" option).

    ``horizon_start`` (an aware UTC datetime, floored to the current hour) drops
    hours **before** it — the lower-bound twin of ``horizon_end`` (issue #92).
    Passing the start of the current hour keeps the running hour and discards
    everything earlier, symmetric to how ``horizon_end`` caps the tail.
    """
    by_time: dict[datetime, dict[str, float | int | None]] = {}

    for param, text in text_by_param.items():
        field = _HOURLY_FIELDS.get(param)
        if field is None:
            continue  # a parameter this forecast does not model
        cast = _to_int if field == "symbol" else _to_float
        for row in _reader(text):
            if _to_int(row.get(_DATA_POINT_ID)) != point.point_id:
                continue
            if _to_int(row.get(_DATA_POINT_TYPE_ID)) != point.point_type_id:
                continue
            when = _parse_datetime(row.get(_DATA_DATE))
            if when is None:
                continue
            if horizon_start is not None and when < horizon_start:
                continue
            if horizon_end is not None and when >= horizon_end:
                continue
            by_time.setdefault(when, {})[field] = cast(row.get(param))

    return [
        HourlyForecast(time=when, **values)
        for when, values in sorted(by_time.items())
    ]


def aggregate_daily_wind(
    text_by_param: dict[str, str],
    point: ForecastPoint,
) -> dict[date, tuple[float | None, float | None, float | None]]:
    """Aggregate hourly wind block texts into per-day (max speed, max gust, bearing).

    A **plain function** so the backend can run it in an executor (ADR-0002).
    The hourly ``Date`` stamps are UTC; grouping uses Europe/Zurich local
    calendar days — the same boundary as the daily ``p``-variants.

    Returns a dict mapping each local calendar date to a triple:
    - ``native_wind_speed``: maximum hourly mean wind speed of the day (km/h)
    - ``native_wind_gust_speed``: maximum hourly gust of the day (km/h, or
      ``None`` when the gust file carried no data for the day)
    - ``wind_bearing``: direction (°) at the hour of maximum wind speed (or
      ``None`` when the direction file had no matching row)
    """
    tz = ZoneInfo(FORECAST_TIMEZONE)

    speed_by_hour: dict[datetime, float] = {}
    gust_by_hour: dict[datetime, float] = {}
    dir_by_hour: dict[datetime, float] = {}

    for param, text in text_by_param.items():
        cast = _to_int if param == HOURLY_WIND_DIRECTION else _to_float
        target = (
            speed_by_hour if param == HOURLY_WIND_SPEED
            else gust_by_hour if param == HOURLY_GUST
            else dir_by_hour if param == HOURLY_WIND_DIRECTION
            else None
        )
        if target is None:
            continue
        for row in _reader(text):
            if _to_int(row.get(_DATA_POINT_ID)) != point.point_id:
                continue
            if _to_int(row.get(_DATA_POINT_TYPE_ID)) != point.point_type_id:
                continue
            when = _parse_datetime(row.get(_DATA_DATE))
            if when is None:
                continue
            val = cast(row.get(param))
            if val is not None:
                target[when] = val  # type: ignore[assignment]

    if not speed_by_hour:
        return {}

    # Find the hour of maximum wind speed and the maximum gust, per local day.
    max_speed: dict[date, float] = {}
    max_speed_hour: dict[date, datetime] = {}
    max_gust: dict[date, float] = {}

    for when, speed in speed_by_hour.items():
        local_day = when.astimezone(tz).date()
        if local_day not in max_speed or speed > max_speed[local_day]:
            max_speed[local_day] = speed
            max_speed_hour[local_day] = when

    for when, gust in gust_by_hour.items():
        local_day = when.astimezone(tz).date()
        if local_day not in max_gust or gust > max_gust[local_day]:
            max_gust[local_day] = gust

    return {
        day: (
            max_speed[day],
            max_gust.get(day),
            dir_by_hour.get(max_speed_hour[day]),
        )
        for day in max_speed
    }
