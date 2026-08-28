"""SwissMetNet stations: metadata, nearest-station search, current values.

Reads the official ``ch.meteoschweiz.ogd-smn`` files (ADR-0001). Column
positions are never hard-coded: every file is parsed by its header, so an
added column upstream does not silently shift a value (docs/ogd.md §A1).
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

import aiohttp

from .const import (
    CSV_SEPARATOR,
    META_DATAINVENTORY_URL,
    META_STATIONS_URL,
    STATION_ENCODING,
    station_now_url,
)
from .geo import haversine_km
from .http import CachedResponse, get_text
from .models import Observation, OgdParseError, Station

# Header columns of ogd-smn_meta_datainventory.csv the client depends on.
_INV_ABBR = "station_abbr"
_INV_PARAM = "parameter"

# Header columns of ogd-smn_meta_stations.csv the client depends on.
_ABBR = "station_abbr"
_NAME = "station_name"
_CANTON = "station_canton"
_HEIGHT = "station_height_masl"
_LAT = "station_coordinates_wgs84_lat"
_LON = "station_coordinates_wgs84_lon"

# Observation fields mapped to their 10-minute parameter codes (docs/ogd.md).
_TIMESTAMP = "reference_timestamp"
_TEMPERATURE = "tre200s0"
_OBSERVATION_CODES: dict[str, str] = {
    "temperature": "tre200s0",
    "humidity": "ure200s0",
    "dew_point": "tde200s0",
    "pressure_qff": "pp0qffs0",
    "pressure_qfe": "prestas0",
    "wind_speed_kmh": "fu3010z0",
    "wind_bearing": "dkl010z0",
    "gust_kmh": "fu3010z1",
    "precipitation_10min": "rre150z0",
    "sunshine_10min": "sre000z0",
    "global_radiation": "gre000z0",
}


def _reader(body: str) -> csv.DictReader:
    return csv.DictReader(io.StringIO(body), delimiter=CSV_SEPARATOR)


def _to_float(value: str | None) -> float | None:
    """Parse a numeric cell; empty means "not measured", not zero."""
    if value is None or value.strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


async def fetch_stations(session: aiohttp.ClientSession) -> list[Station]:
    """Fetch and parse the station metadata list."""
    response = await get_text(session, META_STATIONS_URL, encoding=STATION_ENCODING)
    reader = _reader(response.body)
    if reader.fieldnames is None or _ABBR not in reader.fieldnames:
        raise OgdParseError("station metadata is missing its header")

    stations: list[Station] = []
    for row in reader:
        abbr = (row.get(_ABBR) or "").strip()
        lat = _to_float(row.get(_LAT))
        lon = _to_float(row.get(_LON))
        # A station without an identifier or coordinates cannot be placed.
        if not abbr or lat is None or lon is None:
            continue
        stations.append(
            Station(
                abbr=abbr,
                name=(row.get(_NAME) or "").strip(),
                canton=(row.get(_CANTON) or "").strip(),
                lat=lat,
                lon=lon,
                height_masl=_to_float(row.get(_HEIGHT)),
            )
        )

    if not stations:
        raise OgdParseError("station metadata contained no usable stations")
    return stations


async def fetch_datainventory(
    session: aiohttp.ClientSession,
) -> dict[str, frozenset[str]]:
    """Fetch and parse the data inventory.

    Returns a mapping ``STATION_ABBR → frozenset[parameter_code]`` for every
    station that appears in the file. A station absent from the inventory is not
    in the returned dict; callers should treat that as "unknown" and fall back to
    the full sensor set.
    """
    response = await get_text(
        session, META_DATAINVENTORY_URL, encoding=STATION_ENCODING
    )
    reader = _reader(response.body)
    if (
        reader.fieldnames is None
        or _INV_ABBR not in reader.fieldnames
        or _INV_PARAM not in reader.fieldnames
    ):
        raise OgdParseError("data inventory is missing its header")

    inventory: dict[str, set[str]] = {}
    for row in reader:
        abbr = (row.get(_INV_ABBR) or "").strip().upper()
        param = (row.get(_INV_PARAM) or "").strip()
        if abbr and param:
            inventory.setdefault(abbr, set()).add(param)

    return {abbr: frozenset(params) for abbr, params in inventory.items()}


def nearest_stations(
    stations: list[Station], lat: float, lon: float, *, limit: int = 3
) -> list[Station]:
    """The ``limit`` stations closest to ``lat``/``lon``, nearest first."""
    ordered = sorted(stations, key=lambda s: haversine_km(lat, lon, s.lat, s.lon))
    return ordered[:limit]


async def fetch_current(
    session: aiohttp.ClientSession,
    abbr: str,
    *,
    cache: CachedResponse | None = None,
) -> Observation:
    """Latest 10-minute observation for a station.

    Takes the last row whose air-temperature cell is non-empty — the
    ``now`` file's final rows can trail with not-yet-measured values
    (docs/ogd.md). Raises :class:`OgdParseError` on an empty or garbled
    file rather than returning a half-filled observation.
    """
    response = await get_text(
        session, station_now_url(abbr), cache=cache, encoding=STATION_ENCODING
    )
    reader = _reader(response.body)
    if reader.fieldnames is None or _TEMPERATURE not in reader.fieldnames:
        raise OgdParseError(f"{abbr}: current-values file is missing its header")

    latest: dict[str, str] | None = None
    for row in reader:
        if (row.get(_TEMPERATURE) or "").strip() != "":
            latest = row
    if latest is None:
        raise OgdParseError(f"{abbr}: no row carried an air temperature")

    timestamp = _parse_timestamp(latest.get(_TIMESTAMP), abbr)
    values = {
        field: _to_float(latest.get(code))
        for field, code in _OBSERVATION_CODES.items()
    }
    return Observation(
        station_abbr=(latest.get(_ABBR) or abbr).strip().upper(),
        timestamp=timestamp,
        **values,
    )


def _parse_timestamp(value: str | None, abbr: str) -> datetime:
    """Parse ``dd.mm.yyyy HH:MM`` (UTC) into an aware datetime."""
    if not value or value.strip() == "":
        raise OgdParseError(f"{abbr}: observation row has no timestamp")
    try:
        naive = datetime.strptime(value.strip(), "%d.%m.%Y %H:%M")
    except ValueError as err:
        raise OgdParseError(f"{abbr}: bad timestamp {value!r}") from err
    return naive.replace(tzinfo=UTC)
