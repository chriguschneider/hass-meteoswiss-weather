"""Constants for the MeteoSwiss Weather integration."""

from datetime import timedelta

DOMAIN = "meteoswiss_weather"

# Keep in sync with manifest.json (tests/test_metadata.py enforces it).
VERSION = "0.3.1"

# Required by the CC BY 4.0 terms of the MeteoSwiss open data.
ATTRIBUTION = "Source: MeteoSwiss"

# Config-entry data keys stored by the config flow.
CONF_POSTAL_CODE = "postal_code"
CONF_POINT_ID = "point_id"
CONF_POINT_TYPE_ID = "point_type_id"
CONF_POINT_NAME = "point_name"
CONF_STATION_ABBR = "station_abbr"
CONF_STATION_NAME = "station_name"

# Optional second station from the precipitation-only network (ADR-0006, #70).
# Empty/absent means the feature is off — precipitation then comes from the main
# station. When set, a second 10-minute conditional poll runs and the
# precipitation sensor reads from this station instead.
CONF_PRECIP_STATION_ABBR = "precip_station_abbr"
CONF_PRECIP_STATION_NAME = "precip_station_name"

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

# B9/B11 date-major hourly additions behind per-entity gating (issue #69). Both
# are only meaningful with the hourly opt-in on, and each turns on extra
# date-major files (a horizon prefix each, ~7–11 MB) — the expensive path — so
# they are off by default (ADR-0002 gating rule). ``cloud_layers`` fetches the
# three cloud-cover files (high/mid/low) and exposes ``cloud_coverage`` (the max
# of the layers) plus the three layers as hourly attributes; ``temp_percentiles``
# fetches the p10/p90 temperature files and exposes them as hourly attributes.
CONF_HOURLY_CLOUD_LAYERS = "hourly_cloud_layers"
CONF_HOURLY_TEMP_PERCENTILES = "hourly_temp_percentiles"

# Official MeteoSwiss open data (ADR-0001). The STAC catalogue lists the
# files; the files themselves are plain HTTPS downloads under OGD_FILE_BASE.
OGD_STAC_BASE = "https://data.geo.admin.ch/api/stac/v1"
OGD_FILE_BASE = "https://data.geo.admin.ch"
COLLECTION_STATIONS = "ch.meteoschweiz.ogd-smn"
COLLECTION_LOCAL_FORECAST = "ch.meteoschweiz.ogd-local-forecasting"

# Options-entry key for the pollen opt-in (ADR-0005).
# The pollen station abbreviation is stored separately so the coordinator can
# be rebuilt from the entry without a network call.
CONF_POLLEN = "pollen"
CONF_POLLEN_STATION = "pollen_station"

# Update cadences (ADR-0002).
# Station files carry 10-minute values, so polling faster buys nothing.
STATION_UPDATE_INTERVAL = timedelta(minutes=10)
# The local forecast is republished hourly; the coordinator checks the run
# stamp each hour and only downloads when it changed.
FORECAST_CHECK_INTERVAL = timedelta(hours=1)

# Tiered hourly refresh (ADR-0002 revision 2, issue #68). Measuring all 24 runs
# of 2026-08-27 for two points (docs/ogd.md, "Change rhythm across runs") showed
# the bulk hourly files change on the model run rhythm, not hourly: the near term
# (today + tomorrow) moves at the ICON-CH1 landing hours, days 2+ move at the
# ICON-CH2 landing hours, and six runs a day change nothing.
#
# What triggers a refresh is now a **canary read**, not that timetable (ADR-0008
# section 3, owner decision 2, issue #125): every new run reads the point's next
# few hours of one representative file per group (a few KB, with the remembered
# byte positions) and compares them with the store. Equal values keep the stored
# series and re-stamp it to the run; different values, or a canary that cannot be
# read, refresh the group. The landing-hour sets that used to decide are gone —
# one August measurement is not a contract — leaving only the max-age fallbacks
# below, which still force a refresh so a canary blind spot can never let a
# series go stale unbounded. tests/test_const.py asserts these.
#
# Near window: the part of the date-major horizon that fits row addressing under
# the request cap (issue #143), counted in full local calendar days beyond today.
# 2 days is up to ~72 h, which stays within SERIES_REQUEST_CAP (96) as a single
# row-addressed window, so the default horizon is entirely "near" and refreshes
# on every changed run at ~100–150 KB. The near window is refreshed on a changed
# canary — MeteoSwiss adjusts the near term about hourly — and its staleness
# fallback is 3 h. tests/test_const.py asserts the value fits the cap.
HOURLY_NEAR_HORIZON_DAYS = 2
HOURLY_NEAR_MAX_AGE = timedelta(hours=3)
# Far remainder: the rest of a horizon that reaches beyond the near window (only
# horizons of 3+ days and the full run). It was the multi-MB part of the pre-#143
# refresh (any window of ~80 h or more fell to a prefix on every changed run), so
# it rides this cadence alone — never a canary change — and is fetched by row
# addressing in consecutive cap-sized windows. Its staleness fallback is 6 h.
HOURLY_FAR_MAX_AGE = timedelta(hours=6)
# Point-major group (symbol, precipitation, wind, gust, direction, probability,
# zero-degree, radiation): one ~5 KB block per file. Its staleness fallback,
# after which a run refreshes even when the canary reports no change.
HOURLY_POINT_MAJOR_MAX_AGE = timedelta(hours=3)
# How many of the point's coming hours the canary reads from the representative
# file to decide whether a new run changed a group (ADR-0008 section 3). Small
# on purpose: a date-major file is that many row reads, a point-major file is its
# whole block regardless, so the check is a few KB either way.
HOURLY_CANARY_HOURS = 6
# Pollen data is published hourly; one request per hour per station is enough
# (ADR-0005). Conditional requests (If-None-Match) keep most polls to a 304.
POLLEN_UPDATE_INTERVAL = timedelta(hours=1)

# Persisted fetch-ladder hints (issue #133, ADR-0008). The backend's per-file
# hints (layout, learned byte positions) live in memory and are lost on every
# restart or reload, making the next refresh cold (~800 requests / ~2.7 MB of
# re-discovery). They are saved per config entry with ``helpers.storage.Store``
# under the key below (``.storage/<key>``) and restored at setup. The save is
# debounced by HINTS_SAVE_DELAY seconds so an unchanged tick never writes.
HINTS_STORAGE_VERSION = 1
HINTS_STORAGE_KEY = f"{DOMAIN}.hints"
HINTS_SAVE_DELAY = 30
