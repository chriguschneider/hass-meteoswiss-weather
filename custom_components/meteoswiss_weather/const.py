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

# Reconfigure flow (A9, #52): what to do with the recorded station history when
# the station changes. ``keep`` leaves values and statistics untouched and logs
# the switch; ``discard`` purges the station sensors' states and clears their
# long-term statistics; ``backfill`` rewrites the statistics from the new
# station's official history (ADR-0007) — gated behind ``BACKFILL_AVAILABLE``.
CONF_HISTORY_ACTION = "history_action"
HISTORY_KEEP = "keep"
HISTORY_DISCARD = "discard"
HISTORY_BACKFILL = "backfill"
# The backfill choice needs the recorder-import layer that follows ADR-0007's
# parser (issue #51 shipped the parser only; the import layer is its follow-up).
# Keep/discard ship now; flip this to ``True`` when the import layer lands so the
# backfill option lights up in the reconfigure flow — no other flow change needed.
BACKFILL_AVAILABLE = False

# Options-entry keys (ADR-0002: hourly is off by default; ~1 GB/day when on).
CONF_HOURLY_FORECAST = "hourly_forecast"
# How far ahead the hourly forecast is fetched, in full local calendar days
# beyond today (issue #50). Only shown/used when the hourly option is on. The
# HTTP-Range prefix on the date-major file scales with this, so a smaller
# horizon means less traffic. ``HOURLY_HORIZON_FULL_RUN`` (-1) fetches all
# ~220 h. Default 2 = the rest of today plus two full days (49–72 h).
CONF_HOURLY_HORIZON_DAYS = "hourly_horizon_days"
HOURLY_HORIZON_FULL_RUN = -1
DEFAULT_HOURLY_HORIZON_DAYS = 2
# The choices offered in the options flow: 0–8 days ahead plus the full run.
HOURLY_HORIZON_CHOICES: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6, 7, 8,
                                           HOURLY_HORIZON_FULL_RUN)

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
