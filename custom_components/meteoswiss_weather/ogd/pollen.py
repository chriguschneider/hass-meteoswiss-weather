"""Pollen monitoring client: metadata, nearest-station search, current values.

Reads the official ``ch.meteoschweiz.ogd-pollen`` files (ADR-0001, ADR-0005).
Column positions are never hard-coded: every file is parsed by its header, so
an added column upstream does not silently shift a value (docs/ogd.md §Pollen).

The taxon codes in the ``_h_now.csv`` header (e.g. ``kabetuh0``) are used
directly as keys in :class:`~.models.PollenObservation`; their English names
come from the parameter metadata file, not from a hard-coded mapping.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

import aiohttp

from .const import (
    CSV_SEPARATOR,
    META_POLLEN_DATAINVENTORY_URL,
    META_POLLEN_PARAMETERS_URL,
    META_POLLEN_STATIONS_URL,
    POLLEN_ENCODING,
    pollen_now_url,
)
from .geo import haversine_km
from .http import CachedResponse, get_text
from .models import OgdParseError, PollenObservation, PollenStation

# Header columns of ogd-pollen_meta_stations.csv the client depends on.
# The pollen meta CSV follows the same shape as the SwissMetNet A1 meta CSV
# (docs/ogd.md §Pollen).
_ABBR = "station_abbr"
_NAME = "station_name"
_CANTON = "station_canton"
_HEIGHT = "station_height_masl"
_LAT = "station_coordinates_wgs84_lat"
_LON = "station_coordinates_wgs84_lon"

# Header columns of ogd-pollen_meta_datainventory.csv (same shape as A1 inv).
_INV_ABBR = "station_abbr"
_INV_PARAM = "parameter_shortname"
_INV_TILL = "data_till"

# Header columns of ogd-pollen_meta_parameters.csv.
_PARAM_CODE = "parameter_shortname"
_PARAM_NAME_EN = "parameter_description_en"

# Fields that are NOT taxon values in the _h_now.csv row.
_NON_TAXON_FIELDS = frozenset({"station_abbr", "reference_timestamp"})
_TIMESTAMP = "reference_timestamp"


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


async def fetch_pollen_stations(
    session: aiohttp.ClientSession,
) -> list[PollenStation]:
    """Fetch and parse the pollen station metadata list."""
    response = await get_text(
        session, META_POLLEN_STATIONS_URL, encoding=POLLEN_ENCODING
    )
    reader = _reader(response.body)
    if reader.fieldnames is None or _ABBR not in reader.fieldnames:
        raise OgdParseError("pollen station metadata is missing its header")

    stations: list[PollenStation] = []
    for row in reader:
        abbr = (row.get(_ABBR) or "").strip()
        lat = _to_float(row.get(_LAT))
        lon = _to_float(row.get(_LON))
        if not abbr or lat is None or lon is None:
            continue
        stations.append(
            PollenStation(
                abbr=abbr,
                name=(row.get(_NAME) or "").strip(),
                canton=(row.get(_CANTON) or "").strip(),
                lat=lat,
                lon=lon,
                height_masl=_to_float(row.get(_HEIGHT)),
            )
        )

    if not stations:
        raise OgdParseError("pollen station metadata contained no usable stations")
    return stations


async def fetch_pollen_parameters(
    session: aiohttp.ClientSession,
) -> dict[str, str]:
    """Fetch and parse the pollen parameter metadata.

    Returns a mapping ``parameter_code → English description``. Codes that
    lack an English description are omitted rather than mapped to an empty
    string. The caller uses this to resolve taxon codes from a
    ``_h_now.csv`` header to human-readable names.
    """
    response = await get_text(
        session, META_POLLEN_PARAMETERS_URL, encoding=POLLEN_ENCODING
    )
    reader = _reader(response.body)
    if (
        reader.fieldnames is None
        or _PARAM_CODE not in reader.fieldnames
        or _PARAM_NAME_EN not in reader.fieldnames
    ):
        raise OgdParseError("pollen parameter metadata is missing its header")

    params: dict[str, str] = {}
    for row in reader:
        code = (row.get(_PARAM_CODE) or "").strip()
        name = (row.get(_PARAM_NAME_EN) or "").strip()
        if code and name:
            params[code] = name
    return params


async def fetch_pollen_datainventory(
    session: aiohttp.ClientSession,
) -> dict[str, frozenset[str]]:
    """Fetch and parse the pollen data inventory.

    Returns a mapping ``STATION_ABBR → frozenset[parameter_code]`` for every
    station that appears in the file. A station absent from the inventory is
    not in the returned dict; callers should treat that as "unknown". Only
    parameters without a ``data_till`` value (i.e. still active) are included.
    """
    response = await get_text(
        session, META_POLLEN_DATAINVENTORY_URL, encoding=POLLEN_ENCODING
    )
    reader = _reader(response.body)
    if (
        reader.fieldnames is None
        or _INV_ABBR not in reader.fieldnames
        or _INV_PARAM not in reader.fieldnames
    ):
        raise OgdParseError("pollen data inventory is missing its header")

    inventory: dict[str, set[str]] = {}
    for row in reader:
        abbr = (row.get(_INV_ABBR) or "").strip().upper()
        param = (row.get(_INV_PARAM) or "").strip()
        ended = (row.get(_INV_TILL) or "").strip()
        if abbr and param and not ended:
            inventory.setdefault(abbr, set()).add(param)

    return {abbr: frozenset(params) for abbr, params in inventory.items()}


def nearest_pollen_station(
    stations: list[PollenStation],
    lat: float,
    lon: float,
) -> PollenStation:
    """The pollen station closest to ``lat``/``lon``."""
    return min(stations, key=lambda s: haversine_km(lat, lon, s.lat, s.lon))


async def fetch_pollen_current(
    session: aiohttp.ClientSession,
    abbr: str,
    *,
    cache: CachedResponse | None = None,
) -> PollenObservation:
    """Latest hourly pollen observation for a station.

    Takes the last row whose ``reference_timestamp`` is non-empty — trailing
    rows in the ``now`` file can have empty cells when the hour has not been
    processed yet. Raises :class:`OgdParseError` on an empty or garbled file.
    Taxon columns are inferred from the file header (everything except
    ``station_abbr`` and ``reference_timestamp``), so added taxa upstream do
    not require a code change.
    """
    response = await get_text(
        session, pollen_now_url(abbr), cache=cache, encoding=POLLEN_ENCODING
    )
    reader = _reader(response.body)
    if reader.fieldnames is None or _TIMESTAMP not in reader.fieldnames:
        raise OgdParseError(f"{abbr}: pollen now file is missing its header")

    taxon_cols = [f for f in reader.fieldnames if f not in _NON_TAXON_FIELDS]

    latest: dict[str, str] | None = None
    for row in reader:
        # Require a timestamp and at least one non-empty taxon cell; trailing
        # rows in the _h_now file can carry a timestamp but no measured values
        # when the hour has not been processed yet.
        if (row.get(_TIMESTAMP) or "").strip() == "":
            continue
        if any((row.get(c) or "").strip() != "" for c in taxon_cols):
            latest = row

    if latest is None:
        raise OgdParseError(f"{abbr}: pollen now file has no row with measurements")

    ts = _parse_timestamp(latest.get(_TIMESTAMP), abbr)
    values = {col: _to_float(latest.get(col)) for col in taxon_cols}
    return PollenObservation(
        station_abbr=(latest.get(_ABBR) or abbr).strip().upper(),
        ts_utc=ts,
        values=values,
    )


def _parse_timestamp(value: str | None, abbr: str) -> datetime:
    """Parse ``dd.mm.yyyy HH:MM`` (UTC) into an aware datetime."""
    if not value or value.strip() == "":
        raise OgdParseError(f"{abbr}: pollen observation row has no timestamp")
    try:
        naive = datetime.strptime(value.strip(), "%d.%m.%Y %H:%M")
    except ValueError as err:
        raise OgdParseError(f"{abbr}: bad timestamp {value!r}") from err
    return naive.replace(tzinfo=UTC)
