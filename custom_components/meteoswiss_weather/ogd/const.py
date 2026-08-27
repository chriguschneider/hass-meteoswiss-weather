"""Constants for the pure-Python OGD client.

Duplicated from the integration's ``const.py`` on purpose: this package
imports nothing from Home Assistant (ADR-0001) so it can move to PyPI
unchanged. Keep the values here identical to the ones there.
"""

from __future__ import annotations

# Official MeteoSwiss open data (ADR-0001). Every request goes here.
OGD_FILE_BASE = "https://data.geo.admin.ch"
COLLECTION_STATIONS = "ch.meteoschweiz.ogd-smn"

# SwissMetNet station files are served as Windows-1252; forecast files are
# Latin-1. Both use ";" as the separator (docs/ogd.md).
STATION_ENCODING = "cp1252"
CSV_SEPARATOR = ";"

# The three station metadata CSVs live at the collection root; only the
# station list is needed to place the nearest station.
META_STATIONS_URL = f"{OGD_FILE_BASE}/{COLLECTION_STATIONS}/ogd-smn_meta_stations.csv"


def station_now_url(abbr: str) -> str:
    """URL of a station's 10-minute ``now`` file (the one to poll)."""
    lower = abbr.lower()
    return f"{OGD_FILE_BASE}/{COLLECTION_STATIONS}/{lower}/ogd-smn_{lower}_t_now.csv"
