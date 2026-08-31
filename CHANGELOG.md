# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Release tags carry a `v` prefix (e.g. `v0.1.0`); the release workflow
(`.github/workflows/release.yml`) turns an annotated tag into a GitHub release
using the matching section below as release notes.

## [Unreleased]

## [v0.2.2] — 2026-08-31

### Fixed

- **`cloud_coverage` and the cloud-layer attributes now carry correct
  percentages** (#97). MeteoSwiss silently changed the three cloud-cover
  files (`nprohihs`, `npromths`, `nprolohs`) from percent (0–100) to fraction
  (0–1) between 2026-08-27 and 2026-08-31. The parser now applies a tolerant
  per-file heuristic: if every non-`None` value is ≤ 1.0 the file is treated
  as fraction-encoded and multiplied by 100, so a future silent revert to
  percent encoding is also handled correctly.

## [v0.2.1] — 2026-08-29

### Fixed

- **The hourly forecast no longer starts in the past, and no longer
  shows blank leading hours** (#92). The hourly parser bounded only the
  end of the window, so every refresh delivered the hours the model run
  covers *before* now — up to a full day of them — and the first hours
  came out ragged, because the parameter files do not all begin at the
  same hour: an entry would carry a temperature but no icon, no
  precipitation and no wind, and a weather card rendered it as a blank
  slot. Hours before the current hour are now discarded at parse time
  (the running hour is kept), and an hour is only delivered when it
  carries temperature, symbol, precipitation and wind speed. The
  precipitation-probability, zero-degree-level, radiation, cloud-layer
  and temperature-percentile fields stay optional, so a disabled option
  never drops an otherwise good hour. Present since v0.1.1 and made more
  visible by the extra parameter files v0.2.0 added.

## [v0.2.0] — 2026-08-28

### Added

- **Pollen sensors** (#53, #67, ADR-0005). An opt-in pollen option in the
  options flow picks the nearest station of the `ch.meteoschweiz.ogd-pollen`
  network (the three nearest offered) and creates one sensor per taxon that
  station actually measures — grasses and birch enabled by default, alder,
  hazel, beech, ash and oak shipped but disabled. Concentrations in
  grains/m³, refreshed at most once an hour with conditional requests. The
  taxon codes come from the file header and their names from the parameter
  metadata, so nothing about the taxon list is hard-coded. Pollen setup is
  non-fatal: a failure there never blocks the entry.
- **Reconfigure instead of delete-and-re-add** (#52). The forecast point and
  the weather station can now be changed through Home Assistant's standard
  reconfigure step, which re-offers both with the current choices
  pre-selected and updates the entry in place — history and automations
  survive a change that previously meant deleting the entry. When the
  station really changes, a history step asks what to do with the values
  recorded so far: **keep** them (the default; the switch is written to the
  logbook so the seam stays findable), **discard** them (purges the station
  sensors' recorded states and clears their long-term statistics), or
  **backfill** them from the official station history. A point-only change
  never touches history.
- **Mountain forecast points** (#59). Setup and reconfigure now open with a
  mode step: a postal-code point as before, or one of the 631 mountain
  points of interest, offered in a dropdown with altitude labels and
  pre-selected to the one nearest the Home Assistant location.
- **Wind on the daily forecast, on by default** (#60, ADR-0002 revision 3).
  Daily entries now carry wind speed, gust speed and bearing. The three
  point-major wind files are fetched as ~5 KB per-point blocks alongside
  every daily refresh and aggregated per local calendar day. Wind stays
  best-effort: a file that is not point-major, has not been published yet
  for the run, or fails to fetch degrades wind to `None` instead of failing
  the daily refresh — the ~30 MB full file is never downloaded for a
  default feature.
- **Hourly precipitation probability, zero-degree level and radiation**
  (#55, ADR-0002 revision 4). Three more point-major files join the hourly
  set at roughly 5 KB each: precipitation probability is exposed on the
  hourly forecast under Home Assistant's standard key, the zero-degree
  level as its own sensor (disabled by default), and global radiation on
  the hourly data.
- **Cloud layers and temperature percentiles, per-entity gated** (#69).
  Hourly cloud cover in three layers (high, medium, low) and the p10/p90
  temperature percentiles, both off by default. These are date-major files
  — the expensive path, one horizon prefix each — so they are fetched only
  while their option is on, the per-entity gating of ADR-0002. With neither
  enabled the fetch set is byte-for-byte the one before. `cloud_coverage`,
  Home Assistant's single number, is the maximum of the three layers.
- **Optional separate precipitation station** (#70, ADR-0006). The station
  step of setup and reconfigure now offers an optional second station from
  the automatic precipitation-only network (`ch.meteoschweiz.ogd-smn-precip`,
  ~141 gauges), with the three nearest offered and **none selected by
  default**. When set, the `precipitation` sensor and the weather entity's
  `current_precipitation` attribute read from it — its attribution and a
  `station` attribute name the station — while every other value stays with
  the main station. It is polled every 10 minutes, conditionally, only while
  the option is set (~1.2 KB per poll, inside the ADR-0002 station budget);
  unset means zero requests to the precipitation collection.
- **Service `import_history`: backfill long-term statistics from the
  official station history** (#66, ADR-0007). A one-off, user-triggered
  service imports a station's hourly history (`_h_recent` plus the decade
  files) into Home Assistant's long-term statistics under the integration's
  own sensor statistic ids: mean/min/max for temperature, mean for humidity,
  dew point, pressure, wind, gust and radiation, and an hourly sum for
  precipitation. Optional `start`/`end` (default: the current year);
  overlapping periods are replaced, not duplicated. Long-term statistics
  only — the raw states are untouched. Nothing polls the history files, so
  this stays outside the recurring ADR-0002 budget. The shared
  `async_backfill` layer also powers the reconfigure flow's backfill choice
  (#52).

### Changed

- **Hourly forecast now fetched with HTTP Range, plus a horizon option**
  (#50). The bulk hourly files come in two layouts: point-major files are
  fetched as the configured point's contiguous ~5 KB block (located by a
  binary search over byte offsets), and the one date-major minimum-set file
  (`tre200h0`) as a `Range` prefix covering the chosen horizon. Layout is
  detected at runtime and falls back to a full download if unrecognised. A
  new `hourly_horizon_days` option (options flow, shown only with the hourly
  opt-in) chooses how far ahead to fetch, in full local calendar days —
  default 2 (the rest of today plus two full days), plus a "full run" choice.
  The hourly opt-in now costs roughly 7–11 MB per refresh at the default
  horizon instead of ~125 MB. Revises ADR-0002.
- **The hourly forecast is only downloaded when something asks for it**
  (#54, ADR-0002 revision 2). The bulk hourly fetch moved out of the
  coordinator's polling path into `async_forecast_hourly`, so it happens
  only while a card, an automation or a `weather.get_forecasts` call is
  actually subscribed. An instance nobody looks at pays nothing, even with
  the hourly option on.
- **Hourly refresh follows the measured model-run rhythm** (#68, ADR-0002
  revision 2). The flat 3 h staleness floor gave way to three independently
  scheduled groups: a **near tier** (temperature to the end of tomorrow) at
  the ICON-CH1 landing hours or after 3 h, a **far tier** (temperature out
  to the configured horizon) at the ICON-CH2 hours or after 6 h, and the
  **point-major group** (precipitation, symbol, wind, gust, direction) with
  every new run. The three merge by hour into one forecast. At the default
  horizon a permanently subscribed instance settles around 70 MB/day.

## [v0.1.1] — 2026-08-27

### Fixed

- **Weather symbol table was invented, so most forecast conditions were
  wrong** (#44). The `jp2000d0`/`jww003i0` icon-code → HA-condition map in
  `symbols.py` did not describe the MeteoSwiss icon set: code `2` reported
  `sunny` instead of `partlycloudy`, `26` reported `snowy` (a 22 °C
  September day) instead of `sunny`, `27`/`28` were rain/thunder instead of
  `fog`, `38` was `hail` instead of `lightning-rainy`, and more — roughly
  every second forecast day in Switzerland got a wrong icon. The table is
  now copied faithfully from the reference in
  `Rudd-O/homeassistant-meteoswiss` (MIT), which dumps the official
  MeteoSwiss weather-icon spreadsheet, and credited in the module docstring
  and `docs/symbols.md`.
- **Night codes are now mapped from their own entries** instead of being
  synthesised as `day − 100` with only `sunny → clear-night`. The icon set
  assigns 101–142 independently (e.g. `26` is `sunny` but `126` is
  `cloudy`), and night codes do occur in the hourly file.
- The symbol test no longer validates the table against itself; it pins a
  set of codes to the reference condition and asserts every code 1–42 and
  101–142 resolves, so a gap can no longer make the entity report no
  condition at all.

## [v0.1.0] — 2026-08-27

First release: the integration produces a live `weather` entity per Swiss
postal code, plus the sensors of the chosen SwissMetNet station.

### Added

- **`weather` entity** with current conditions (temperature, humidity,
  pressure, wind speed and direction, precipitation) sourced from the
  nearest SwissMetNet station (10-minute values) and a 9-day daily forecast
  sourced from the MeteoSwiss local-forecast file for the configured point.
- **Hourly forecast** as an opt-in option (off by default; ADR-0002). When
  enabled, the weather entity advertises `FORECAST_HOURLY` and serves
  temperature, precipitation, symbol, wind speed, gust and bearing per hour
  from the bulk local-forecast files. The download (~1.5 GB/day) is throttled
  to `HOURLY_FORECAST_MIN_INTERVAL` (3 h) regardless of how often a new run
  appears, and the current hour's symbol drives the entity `condition` when
  available. Toggling the option reloads the entry.
- **Station sensors**: temperature, humidity, dew point, pressure (QFF, and
  QFE as a diagnostic), wind speed, bearing, gust, 10-minute precipitation,
  sunshine duration and global radiation, refreshed every 10 minutes.
- **`ogd/` client package** — pure Python (no HA imports): STAC catalogue
  discovery, station CSV download and parsing, local-forecast CSV download
  and parsing, weather-symbol mapping to HA condition strings.
- **Three-step config flow**: postal-code entry → forecast point selection
  → nearest-station confirmation. Options flow lets users toggle hourly
  forecast.
- Station and forecast `DataUpdateCoordinator`s with conditional HTTP
  requests and executor-offloaded CSV parsing.
- **Weekly upstream smoke test** (`tests/tools/smoke_test.py`, ADR-0004): the
  only check that touches live data. It reads the parameter codes from the
  integration itself and requires the configured postal-code point in every
  daily file — the property whose absence caused the defect below.

### Fixed

- **Daily forecast now has temperatures and precipitation for postal-code
  points.** The daily client fetched `tre200dx`/`tre200dn`/`rka150d0`, which
  MeteoSwiss publishes for weather stations only — so the default configuration
  (a postal-code point) silently got `temp_max`/`temp_min`/`precipitation` of
  `None` and only a symbol. It now fetches the local-calendar-day
  `tre200px`/`tre200pn`/`rka150p0` variants, which cover every point type. The
  daily refresh grows from ~2 MB to ~5 MB, still far below the hourly opt-in
  (ADR-0002). Caught by the smoke test before this release. (#34)
- CI: the Claude agent workflows check out with `persist-credentials: false`.
  `actions/checkout@v7` keeps `GITHUB_TOKEN` in an `includeIf` credentials
  file that `claude-code-action` does not clear, so the reviewer's fix commit
  on PR #26 was pushed as `github-actions[bot]` and its CI runs waited for a
  manual approval instead of letting auto-merge proceed.
- CI: `claude-review.yml` guarded on `draft == true`, which is false by
  definition on a `ready_for_review` event, so an agent marking its own draft
  ready skipped the independent review entirely.

## [v0.0.1] — 2026-08-26

Repository scaffold. Not released: the first release is cut once the
integration produces a weather entity (see the tracking issue in the README).

### Added

- Integration skeleton `custom_components/meteoswiss_weather` with a
  postal-code config flow, brand icons and English strings; no platforms yet
- ADR-0001 (official Open Data is the only upstream), ADR-0002 (traffic
  budget for the bulk local-forecast files), ADR-0003 (sibling of the radar
  integration, not a merge), ADR-0004 (quality gates and release process
  inherited from the radar repo)
- `docs/ogd.md` with the measured facts about the MeteoSwiss open data
  files, so contributors and agents do not have to rediscover them
- CI (hassfest, HACS validation, ruff, pytest), CodeQL, SonarCloud, the
  tag-triggered release gate with a zip asset, and the opt-in Claude agent
  workflows (label, mention, autopilot, reviewer)

[Unreleased]: https://github.com/chriguschneider/hass-meteoswiss-weather/compare/v0.2.2...HEAD
[v0.2.2]: https://github.com/chriguschneider/hass-meteoswiss-weather/compare/v0.2.1...v0.2.2
[v0.2.1]: https://github.com/chriguschneider/hass-meteoswiss-weather/compare/v0.2.0...v0.2.1
[v0.2.0]: https://github.com/chriguschneider/hass-meteoswiss-weather/compare/v0.1.1...v0.2.0
[v0.1.1]: https://github.com/chriguschneider/hass-meteoswiss-weather/compare/v0.1.0...v0.1.1
[v0.1.0]: https://github.com/chriguschneider/hass-meteoswiss-weather/compare/v0.0.1...v0.1.0
[v0.0.1]: https://github.com/chriguschneider/hass-meteoswiss-weather/releases/tag/v0.0.1
