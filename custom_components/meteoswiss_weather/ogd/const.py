"""Constants for the pure-Python OGD client.

Duplicated from the integration's ``const.py`` on purpose: this package
imports nothing from Home Assistant (ADR-0001) so it can move to PyPI
unchanged. Keep the values here identical to the ones there.
"""

from __future__ import annotations

# Official MeteoSwiss open data (ADR-0001). Every request goes here.
OGD_FILE_BASE = "https://data.geo.admin.ch"
# STAC catalogue for discovering the newest local-forecast run (docs/ogd.md).
OGD_STAC_BASE = f"{OGD_FILE_BASE}/api/stac/v1"
COLLECTION_STATIONS = "ch.meteoschweiz.ogd-smn"
COLLECTION_FORECAST = "ch.meteoschweiz.ogd-local-forecasting"
COLLECTION_POLLEN = "ch.meteoschweiz.ogd-pollen"

# SwissMetNet station files are served as Windows-1252; forecast files are
# Latin-1; pollen files are also Windows-1252 (docs/ogd.md). All use ";" as
# the separator.
STATION_ENCODING = "cp1252"
FORECAST_ENCODING = "iso-8859-1"
POLLEN_ENCODING = "cp1252"
CSV_SEPARATOR = ";"

# The three station metadata CSVs live at the collection root.
META_STATIONS_URL = f"{OGD_FILE_BASE}/{COLLECTION_STATIONS}/ogd-smn_meta_stations.csv"
# Which station measures which parameter since when; used to skip sensors
# for parameters the chosen station does not carry (issue #46).
META_DATAINVENTORY_URL = (
    f"{OGD_FILE_BASE}/{COLLECTION_STATIONS}/ogd-smn_meta_datainventory.csv"
)

# Pollen metadata (download once, cache): station list, parameter names, and
# inventory of which station measures which taxon (docs/ogd.md §Pollen).
META_POLLEN_STATIONS_URL = (
    f"{OGD_FILE_BASE}/{COLLECTION_POLLEN}/ogd-pollen_meta_stations.csv"
)
META_POLLEN_PARAMETERS_URL = (
    f"{OGD_FILE_BASE}/{COLLECTION_POLLEN}/ogd-pollen_meta_parameters.csv"
)
META_POLLEN_DATAINVENTORY_URL = (
    f"{OGD_FILE_BASE}/{COLLECTION_POLLEN}/ogd-pollen_meta_datainventory.csv"
)

# Local-forecast metadata (download once, cache): the point list resolves a
# postal code to a forecast point, the parameter list names the columns.
META_POINT_URL = (
    f"{OGD_FILE_BASE}/{COLLECTION_FORECAST}/ogd-local-forecasting_meta_point.csv"
)
META_PARAMETERS_URL = (
    f"{OGD_FILE_BASE}/{COLLECTION_FORECAST}/ogd-local-forecasting_meta_parameters.csv"
)

# Point types in the point metadata (docs/ogd.md §E4): a postal-code centre
# is type 2, its point_id is ``PLZ * 100 + n``.
POINT_TYPE_STATION = 1
POINT_TYPE_POSTAL_CODE = 2
POINT_TYPE_MOUNTAIN = 3

# Daily parameter codes fetched for the default forecast (ADR-0002): max/min
# temperature, precipitation sum, weather symbol. These four files are the
# whole cost of a daily refresh (order of 5 MB).
#
# The ``p``-variants (``…px``/``…pn``/``…p0``) are used deliberately, not the
# ``d``/``0``-variants (``tre200dx``/``tre200dn``/``rka150d0``): the daily
# ``d`` files are aggregated over the UTC day and are published **for stations
# only** (point_type_id 1). The default configuration is a postal-code centre
# (type 2), for which those files carry no rows at all, so the forecast would
# silently have no temperatures or precipitation (issue #34). The ``p``-variants
# are aggregated over the **local calendar day** — the boundary the MeteoSwiss
# app uses — and are the only daily files that contain non-station points
# (types 1, 2 and 3). ``jp2000d0`` (the symbol) already covers all types.
DAILY_TEMP_MAX = "tre200px"
DAILY_TEMP_MIN = "tre200pn"
DAILY_PRECIPITATION = "rka150p0"
DAILY_SYMBOL = "jp2000d0"
DAILY_REQUIRED_PARAMS: tuple[str, ...] = (
    DAILY_TEMP_MAX,
    DAILY_TEMP_MIN,
    DAILY_PRECIPITATION,
    DAILY_SYMBOL,
)

# Hourly parameter codes fetched for the opt-in hourly forecast (ADR-0002).
# Each of these files is ~30 MB and holds every point, so the set is the whole
# traffic budget: it is the documented minimum (temperature, precipitation,
# symbol, wind speed) plus gust and wind direction, which are included only
# because the hourly forecast entity exposes gust and bearing per hour.
HOURLY_TEMPERATURE = "tre200h0"
HOURLY_PRECIPITATION = "rre150h0"
HOURLY_SYMBOL = "jww003i0"
HOURLY_WIND_SPEED = "fu3010h0"
HOURLY_GUST = "fu3010h1"
HOURLY_WIND_DIRECTION = "dkl010h0"
HOURLY_REQUIRED_PARAMS: tuple[str, ...] = (
    HOURLY_TEMPERATURE,
    HOURLY_PRECIPITATION,
    HOURLY_SYMBOL,
    HOURLY_WIND_SPEED,
    HOURLY_GUST,
    HOURLY_WIND_DIRECTION,
)

# Per-file HTTP-Range strategy for the hourly bulk files (issue #50, ADR-0002
# revision). The 30 MB files have two layouts, detected at runtime:
#   - "date-major": rows sorted by Date, so the earliest hours of every point
#     sit at the file start and a prefix Range covers the wanted horizon;
#   - "point-major": one point's ~220 rows are contiguous (~5 KB) and a single
#     Range request fetches them after a binary search over byte offsets.
# On anything the classifier does not recognise, fall back to the full file.

# The forecast day boundary the app and the daily p-variants use; the hourly
# horizon is counted in full local calendar days against it (docs/ogd.md §E4).
FORECAST_TIMEZONE = "Europe/Zurich"

# "Full run" sentinel for the horizon option: fetch and keep every hour
# (~220 h, today's behaviour) instead of trimming to a day horizon.
HOURLY_HORIZON_FULL_RUN = -1

# Date-major prefix budget. A date-major hour block is ~150 KB (all ~5,600
# points for one hour); size the prefix from the number of hours to the horizon
# with headroom, and extend it if a probe shows the horizon was not reached.
HOURLY_BYTES_PER_HOUR = 200_000
HOURLY_RANGE_SAFETY = 1.5

# A single data row is short (`id;type;YYYYMMDDHHMM;value`); this window is wide
# enough to always contain a complete row plus its neighbouring boundaries.
HOURLY_ROW_PROBE_BYTES = 1024
# Chunk size for reading a point-major block forward until the key changes.
HOURLY_BLOCK_CHUNK_BYTES = 16_384


def station_now_url(abbr: str) -> str:
    """URL of a station's 10-minute ``now`` file (the one to poll)."""
    lower = abbr.lower()
    return f"{OGD_FILE_BASE}/{COLLECTION_STATIONS}/{lower}/ogd-smn_{lower}_t_now.csv"


def station_stac_item_url(abbr: str) -> str:
    """STAC item URL for a single station (id = lowercase abbreviation)."""
    return f"{OGD_STAC_BASE}/collections/{COLLECTION_STATIONS}/items/{abbr.lower()}"


def stac_items_url(collection: str) -> str:
    """STAC items listing for a collection (assets carry the file hrefs)."""
    return f"{OGD_STAC_BASE}/collections/{collection}/items"


def pollen_now_url(abbr: str) -> str:
    """URL of a pollen station's hourly ``now`` file (the one to poll)."""
    lower = abbr.lower()
    return (
        f"{OGD_FILE_BASE}/{COLLECTION_POLLEN}/{lower}"
        f"/ogd-pollen_{lower}_h_now.csv"
    )
