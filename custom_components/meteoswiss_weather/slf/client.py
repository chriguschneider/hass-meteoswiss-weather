"""SLF IMIS client: station list, latest measurement, daily snow (ADR-0009).

Three endpoints only (ADR-0009); the bulk ``/measurements`` endpoint is
never requested. JSON parsing is cheap (~16 KB per response), so no
executor is needed. The ``cache`` arguments carry the ``ETag`` between
polls so an unchanged response costs only a 304.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import aiohttp

from ..ogd.geo import haversine_km
from ..ogd.http import CachedResponse, get_text
from .const import DAILY_SNOW_URL, SNOW_FLAT, STATIONS_URL, measurements_url
from .models import (
    ImisDailySnow,
    ImisMeasurement,
    ImisStation,
    SlfConnectionError,
    SlfParseError,
)


def _opt_float(value: object) -> float | None:
    """Return a float from a JSON value that may be ``null``."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_station(obj: dict) -> ImisStation:
    return ImisStation(
        code=obj["code"],
        label=obj["label"],
        lat=float(obj["lat"]),
        lon=float(obj["lon"]),
        elevation=float(obj["elevation"]),
        canton=obj["canton_code"],
        type=obj["type"],
    )


def _parse_measurement(obj: dict) -> ImisMeasurement:
    # measure_date is ISO 8601 with a trailing "Z" (UTC).
    raw_ts = obj["measure_date"]
    if raw_ts.endswith("Z"):
        raw_ts = raw_ts[:-1]
    ts = datetime.fromisoformat(raw_ts).replace(tzinfo=UTC)
    return ImisMeasurement(
        time=ts,
        snow_height_cm=_opt_float(obj.get("HS")),
        air_temperature=_opt_float(obj.get("TA_30MIN_MEAN")),
        wind_speed_ms=_opt_float(obj.get("VW_30MIN_MEAN")),
        gust_ms=_opt_float(obj.get("VW_30MIN_MAX")),
        wind_bearing=_opt_float(obj.get("DW_30MIN_MEAN")),
    )


def _parse_daily_snow(obj: dict) -> ImisDailySnow:
    # measure_date carries no timezone suffix; treat as UTC (docs/slf.md).
    dt = datetime.fromisoformat(obj["measure_date"])
    return ImisDailySnow(
        date=dt.date(),
        snow_height_cm=_opt_float(obj.get("HS")),
        new_snow_24h_cm=_opt_float(obj.get("HN_1D")),
    )


async def fetch_imis_stations(
    session: aiohttp.ClientSession,
) -> list[ImisStation]:
    """Fetch and parse the full IMIS station list.

    Called once at setup and on reconfigure (ADR-0009); no caching argument
    because the station list is only needed for the picker, not polled.
    """
    try:
        cached = await get_text(session, STATIONS_URL)
    except Exception as exc:
        raise SlfConnectionError(f"fetch IMIS stations failed: {exc}") from exc
    try:
        data = json.loads(cached.body)
        return [_parse_station(obj) for obj in data]
    except (ValueError, KeyError, TypeError) as exc:
        raise SlfParseError(f"IMIS stations parse failed: {exc}") from exc


def nearest_snow_stations(
    stations: list[ImisStation],
    lat: float,
    lon: float,
    limit: int = 3,
) -> list[ImisStation]:
    """Return the ``limit`` nearest ``SNOW_FLAT`` stations, closest first."""
    flat = [s for s in stations if s.type == SNOW_FLAT]
    flat.sort(key=lambda s: haversine_km(lat, lon, s.lat, s.lon))
    return flat[:limit]


async def fetch_imis_latest(
    session: aiohttp.ClientSession,
    code: str,
    *,
    cache: CachedResponse | None = None,
) -> ImisMeasurement:
    """Fetch the newest 30-minute row for one IMIS station.

    ``cache`` carries the ``ETag`` from the previous call so an unchanged
    response costs only a 304. An empty measurement list raises
    :class:`SlfParseError`. An unknown station code raises
    :class:`SlfConnectionError` (the API returns HTTP 400).
    """
    url = measurements_url(code)
    try:
        result = await get_text(session, url, cache=cache)
    except Exception as exc:
        raise SlfConnectionError(
            f"fetch IMIS measurements for {code!r} failed: {exc}"
        ) from exc
    try:
        rows = json.loads(result.body)
        if not rows:
            raise SlfParseError(f"empty measurement list for {code!r}")
        return _parse_measurement(rows[-1])
    except SlfParseError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        raise SlfParseError(
            f"IMIS measurements parse failed for {code!r}: {exc}"
        ) from exc


async def fetch_imis_daily_snow(
    session: aiohttp.ClientSession,
    code: str,
    *,
    cache: CachedResponse | None = None,
) -> ImisDailySnow | None:
    """Fetch the SNOWPACK-modelled daily snow row for one station.

    ``cache`` carries the ``ETag`` from the previous call. Returns ``None``
    when the station has no row in the response (e.g. ``SCH2``). A row that
    is present but all-null (e.g. ``BOG4``) returns an
    :class:`ImisDailySnow` with both numeric fields set to ``None``.
    """
    try:
        result = await get_text(session, DAILY_SNOW_URL, cache=cache)
    except Exception as exc:
        raise SlfConnectionError(f"fetch IMIS daily snow failed: {exc}") from exc
    try:
        rows = json.loads(result.body)
        for row in rows:
            if row.get("station_code") == code:
                return _parse_daily_snow(row)
        return None
    except (ValueError, KeyError, TypeError) as exc:
        raise SlfParseError(f"IMIS daily-snow parse failed: {exc}") from exc
