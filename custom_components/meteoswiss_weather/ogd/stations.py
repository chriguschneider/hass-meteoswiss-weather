"""SwissMetNet stations: metadata, nearest-station search, current values.

Reads the official ``ch.meteoschweiz.ogd-smn`` and
``ch.meteoschweiz.ogd-smn-precip`` files (ADR-0001, ADR-0006). Column
positions are never hard-coded: every file is parsed by its header, so an
added column upstream does not silently shift a value (docs/ogd.md §A1/A2).

The internal ``_fetch_*`` helpers are parameterised by URL and validity field
so both collections share a single parser; the public wrappers pick the
collection-specific arguments.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

import aiohttp

from .const import (
    CSV_SEPARATOR,
    META_DATAINVENTORY_URL,
    META_PRECIP_DATAINVENTORY_URL,
    META_PRECIP_STATIONS_URL,
    META_STATIONS_URL,
    STATION_ENCODING,
    precip_station_now_url,
    station_now_url,
)
from .geo import haversine_km
from .http import CachedResponse, get_text
from .models import Observation, OgdParseError, Station

# Header columns of ogd-smn_meta_datainventory.csv the client depends on.
# The upstream file names the parameter column ``parameter_shortname`` and marks
# a retired measurement with a non-empty ``data_till`` (verified 2026-08-28:
# header ``station_abbr;parameter_shortname;meas_cat_nr;data_since;data_till;owner``).
_INV_ABBR = "station_abbr"
_INV_PARAM = "parameter_shortname"
_INV_TILL = "data_till"

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
_PRECIP_10MIN = "rre150z0"
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
    # B1
    "snow_depth": "htoauts0",
    # B2
    "wind_chill": "xchills0",
    "pressure_qnh": "pp0qnhs0",
    # B3
    "soil_temp_5cm": "tso005s0",
    "soil_temp_10cm": "tso010s0",
    "soil_temp_20cm": "tso020s0",
    # B4
    "air_temp_5cm": "tre005s0",
    # B5
    "diffuse_radiation": "ods000z0",
    "longwave_radiation": "oli000z0",
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


# ---------------------------------------------------------------------------
# Private helpers — shared parser logic parameterised by collection.
# ---------------------------------------------------------------------------


async def _fetch_stations_impl(
    session: aiohttp.ClientSession, url: str, encoding: str
) -> list[Station]:
    response = await get_text(session, url, encoding=encoding)
    reader = _reader(response.body)
    if reader.fieldnames is None or _ABBR not in reader.fieldnames:
        raise OgdParseError("station metadata is missing its header")

    stations: list[Station] = []
    for row in reader:
        abbr = (row.get(_ABBR) or "").strip()
        lat = _to_float(row.get(_LAT))
        lon = _to_float(row.get(_LON))
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


async def _fetch_datainventory_impl(
    session: aiohttp.ClientSession, url: str, encoding: str
) -> dict[str, frozenset[str]]:
    response = await get_text(session, url, encoding=encoding)
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
        # A non-empty ``data_till`` marks a measurement that has ended; the
        # station no longer carries that parameter, so exclude it.
        ended = (row.get(_INV_TILL) or "").strip()
        if abbr and param and not ended:
            inventory.setdefault(abbr, set()).add(param)

    return {abbr: frozenset(params) for abbr, params in inventory.items()}


async def _fetch_current_impl(
    session: aiohttp.ClientSession,
    url: str,
    abbr: str,
    validity_field: str,
    encoding: str,
    *,
    cache: CachedResponse | None = None,
) -> Observation:
    """Latest 10-minute observation.

    Takes the last row whose ``validity_field`` cell is non-empty. For the
    full SwissMetNet collection this is ``tre200s0`` (air temperature); for
    the precipitation-only collection it is ``rre150z0``. Parameter codes
    absent from the file (e.g. temperature in a precip-only file) produce
    ``None`` in the returned :class:`~.models.Observation`.
    """
    response = await get_text(session, url, cache=cache, encoding=encoding)
    reader = _reader(response.body)
    if reader.fieldnames is None or validity_field not in reader.fieldnames:
        raise OgdParseError(f"{abbr}: current-values file is missing its header")

    latest: dict[str, str] | None = None
    for row in reader:
        if (row.get(validity_field) or "").strip() != "":
            latest = row
    if latest is None:
        raise OgdParseError(
            f"{abbr}: no row carried a non-empty {validity_field!r} value"
        )

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


# ---------------------------------------------------------------------------
# Public API — ogd-smn (full SwissMetNet, unchanged from issue #4).
# ---------------------------------------------------------------------------


async def fetch_stations(session: aiohttp.ClientSession) -> list[Station]:
    """Fetch and parse the station metadata list."""
    return await _fetch_stations_impl(session, META_STATIONS_URL, STATION_ENCODING)


async def fetch_datainventory(
    session: aiohttp.ClientSession,
) -> dict[str, frozenset[str]]:
    """Fetch and parse the data inventory.

    Returns a mapping ``STATION_ABBR → frozenset[parameter_code]`` for every
    station that appears in the file. A station absent from the inventory is not
    in the returned dict; callers should treat that as "unknown" and fall back to
    the full sensor set.
    """
    return await _fetch_datainventory_impl(
        session, META_DATAINVENTORY_URL, STATION_ENCODING
    )


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
    """Latest 10-minute observation for a full SwissMetNet station.

    Takes the last row whose air-temperature cell is non-empty — the
    ``now`` file's final rows can trail with not-yet-measured values
    (docs/ogd.md). Raises :class:`OgdParseError` on an empty or garbled
    file rather than returning a half-filled observation.
    """
    return await _fetch_current_impl(
        session,
        station_now_url(abbr),
        abbr,
        _TEMPERATURE,
        STATION_ENCODING,
        cache=cache,
    )


# ---------------------------------------------------------------------------
# Public API — ogd-smn-precip (precipitation-only network, ADR-0006, #56).
# ---------------------------------------------------------------------------


async def fetch_precip_stations(session: aiohttp.ClientSession) -> list[Station]:
    """Fetch and parse the precipitation-station metadata list.

    The meta CSV has the same shape as the full SwissMetNet ``A1`` file;
    ``nearest_stations`` works on the returned list unchanged (ADR-0006).
    """
    return await _fetch_stations_impl(
        session, META_PRECIP_STATIONS_URL, STATION_ENCODING
    )


async def fetch_precip_datainventory(
    session: aiohttp.ClientSession,
) -> dict[str, frozenset[str]]:
    """Fetch and parse the precipitation-station data inventory."""
    return await _fetch_datainventory_impl(
        session, META_PRECIP_DATAINVENTORY_URL, STATION_ENCODING
    )


async def fetch_precip_current(
    session: aiohttp.ClientSession,
    abbr: str,
    *,
    cache: CachedResponse | None = None,
) -> Observation:
    """Latest 10-minute precipitation observation for a precip-only station.

    The ``_t_now.csv`` for this collection has only ``rre150z0`` (10-minute
    precipitation sum); the returned :class:`~.models.Observation` has
    ``precipitation_10min`` set and every other field ``None`` (docs/ogd.md
    §A2). Takes the last row where ``rre150z0`` is non-empty.
    """
    return await _fetch_current_impl(
        session,
        precip_station_now_url(abbr),
        abbr,
        _PRECIP_10MIN,
        STATION_ENCODING,
        cache=cache,
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
