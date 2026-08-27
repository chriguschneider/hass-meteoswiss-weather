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

# SwissMetNet station files are served as Windows-1252; forecast files are
# Latin-1. Both use ";" as the separator (docs/ogd.md).
STATION_ENCODING = "cp1252"
FORECAST_ENCODING = "iso-8859-1"
CSV_SEPARATOR = ";"

# The three station metadata CSVs live at the collection root; only the
# station list is needed to place the nearest station.
META_STATIONS_URL = f"{OGD_FILE_BASE}/{COLLECTION_STATIONS}/ogd-smn_meta_stations.csv"

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
# whole cost of a daily refresh (order of 2 MB).
DAILY_TEMP_MAX = "tre200dx"
DAILY_TEMP_MIN = "tre200dn"
DAILY_PRECIPITATION = "rka150d0"
DAILY_SYMBOL = "jp2000d0"
DAILY_REQUIRED_PARAMS: tuple[str, ...] = (
    DAILY_TEMP_MAX,
    DAILY_TEMP_MIN,
    DAILY_PRECIPITATION,
    DAILY_SYMBOL,
)


def station_now_url(abbr: str) -> str:
    """URL of a station's 10-minute ``now`` file (the one to poll)."""
    lower = abbr.lower()
    return f"{OGD_FILE_BASE}/{COLLECTION_STATIONS}/{lower}/ogd-smn_{lower}_t_now.csv"


def stac_items_url(collection: str) -> str:
    """STAC items listing for a collection (assets carry the file hrefs)."""
    return f"{OGD_STAC_BASE}/collections/{collection}/items"
