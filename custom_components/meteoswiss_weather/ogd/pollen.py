"""Pollen monitoring client: metadata, nearest-station search, current values.

Reads the official ``ch.meteoschweiz.ogd-pollen`` files (ADR-0001, ADR-0005).
Column positions are never hard-coded: every file is parsed by its header, so
an added column upstream does not silently shift a value (docs/ogd.md §Pollen).

The taxon codes in the ``_h_now.csv`` header (e.g. ``kabetuh0``) are used
directly as keys in :class:`~.models.PollenObservation`; their English names
come from the parameter metadata file, not from a hard-coded mapping.
"""

from __future__ import annotations

import aiohttp

from .const import (
    META_POLLEN_DATAINVENTORY_URL,
    META_POLLEN_PARAMETERS_URL,
    META_POLLEN_STATIONS_URL,
    POLLEN_ENCODING,
    pollen_now_url,
)
from .geo import nearest
from .http import CachedResponse, get_text
from .models import OgdParseError, PollenObservation, PollenStation
from .parse import csv_reader, parse_timestamp, to_float
from .stations import _fetch_datainventory_impl, _fetch_stations_impl

# Station metadata and data inventory share the SwissMetNet A1 shape
# (docs/ogd.md §Pollen) and are parsed by ``stations``.
_ABBR = "station_abbr"

# Header columns of ogd-pollen_meta_parameters.csv.
_PARAM_CODE = "parameter_shortname"
_PARAM_NAME_EN = "parameter_description_en"

# Fields that are NOT taxon values in the _h_now.csv row.
_NON_TAXON_FIELDS = frozenset({"station_abbr", "reference_timestamp"})
_TIMESTAMP = "reference_timestamp"


async def fetch_pollen_stations(
    session: aiohttp.ClientSession,
) -> list[PollenStation]:
    """Fetch and parse the pollen station metadata list."""
    return await _fetch_stations_impl(
        session,
        META_POLLEN_STATIONS_URL,
        POLLEN_ENCODING,
        PollenStation,
        "pollen ",
    )


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
    reader = csv_reader(response.body)
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
    return await _fetch_datainventory_impl(
        session, META_POLLEN_DATAINVENTORY_URL, POLLEN_ENCODING, "pollen "
    )


def nearest_pollen_station(
    stations: list[PollenStation],
    lat: float,
    lon: float,
) -> PollenStation:
    """The pollen station closest to ``lat``/``lon``."""
    return nearest(stations, lat, lon, 1)[0]


def nearest_pollen_stations(
    stations: list[PollenStation],
    lat: float,
    lon: float,
    limit: int = 3,
) -> list[PollenStation]:
    """The ``limit`` pollen stations closest to ``lat``/``lon``, nearest first."""
    return nearest(stations, lat, lon, limit)


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
    reader = csv_reader(response.body)
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

    ts = parse_timestamp(latest.get(_TIMESTAMP), abbr, subject="pollen observation")
    values = {col: to_float(latest.get(col)) for col in taxon_cols}
    return PollenObservation(
        station_abbr=(latest.get(_ABBR) or abbr).strip().upper(),
        ts_utc=ts,
        values=values,
    )
