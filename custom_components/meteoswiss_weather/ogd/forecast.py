"""Local forecast: resolve a postal code to a point, parse the daily files.

Reads the official ``ch.meteoschweiz.ogd-local-forecasting`` files (ADR-0001).
Columns are addressed by header, never by position (docs/ogd.md §E4). The
daily parameter files are small (order of 2 MB together); the hourly files are
the whole traffic budget and live behind the backend seam (ADR-0002).
"""

from __future__ import annotations

import csv
import io
from datetime import date

import aiohttp

from .const import (
    CSV_SEPARATOR,
    DAILY_PRECIPITATION,
    DAILY_SYMBOL,
    DAILY_TEMP_MAX,
    DAILY_TEMP_MIN,
    FORECAST_ENCODING,
    META_POINT_URL,
    POINT_TYPE_POSTAL_CODE,
)
from .geo import haversine_km
from .http import get_text
from .models import DailyForecast, ForecastPoint, OgdParseError

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
