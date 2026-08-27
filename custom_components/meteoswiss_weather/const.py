"""Constants for the MeteoSwiss Weather integration."""

from datetime import timedelta

DOMAIN = "meteoswiss_weather"

# Keep in sync with manifest.json (tests/test_metadata.py enforces it).
VERSION = "0.1.1"

# Required by the CC BY 4.0 terms of the MeteoSwiss open data.
ATTRIBUTION = "Source: MeteoSwiss"

# Config-entry data keys stored by the config flow.
CONF_POSTAL_CODE = "postal_code"
CONF_POINT_ID = "point_id"
CONF_POINT_TYPE_ID = "point_type_id"
CONF_POINT_NAME = "point_name"
CONF_STATION_ABBR = "station_abbr"
CONF_STATION_NAME = "station_name"

# Options-entry keys (ADR-0002: hourly is off by default; ~1 GB/day when on).
CONF_HOURLY_FORECAST = "hourly_forecast"

# Official MeteoSwiss open data (ADR-0001). The STAC catalogue lists the
# files; the files themselves are plain HTTPS downloads under OGD_FILE_BASE.
OGD_STAC_BASE = "https://data.geo.admin.ch/api/stac/v1"
OGD_FILE_BASE = "https://data.geo.admin.ch"
COLLECTION_STATIONS = "ch.meteoschweiz.ogd-smn"
COLLECTION_LOCAL_FORECAST = "ch.meteoschweiz.ogd-local-forecasting"

# Update cadences (ADR-0002).
# Station files carry 10-minute values, so polling faster buys nothing.
STATION_UPDATE_INTERVAL = timedelta(minutes=10)
# The local forecast is republished hourly; the coordinator checks the run
# stamp each hour and only downloads when it changed.
FORECAST_CHECK_INTERVAL = timedelta(hours=1)
# Hard floor for the opt-in hourly forecast: its bulk files are the whole
# traffic budget (~1 GB/day), so they are never fetched more often than this.
# tests/test_const.py asserts this stays at least 3 hours.
HOURLY_FORECAST_MIN_INTERVAL = timedelta(hours=3)
