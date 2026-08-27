# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Release tags carry a `v` prefix (e.g. `v0.1.0`); the release workflow
(`.github/workflows/release.yml`) turns an annotated tag into a GitHub release
using the matching section below as release notes.

## [Unreleased]

### Fixed

- **Daily forecast now has temperatures and precipitation for postal-code
  points.** The daily client fetched `tre200dx`/`tre200dn`/`rka150d0`, which
  MeteoSwiss publishes for weather stations only — so the default configuration
  (a postal-code point) silently got `temp_max`/`temp_min`/`precipitation` of
  `None` and only a symbol. It now fetches the local-calendar-day
  `tre200px`/`tre200pn`/`rka150p0` variants, which cover every point type. The
  daily refresh grows from ~2 MB to ~5 MB, still far below the hourly opt-in
  (ADR-0002). (#34)

### Added

- **Hourly forecast** as an opt-in option (off by default; ADR-0002). When
  enabled, the weather entity advertises `FORECAST_HOURLY` and serves
  temperature, precipitation, symbol, wind speed, gust and bearing per hour
  from the bulk local-forecast files. The download (~1.5 GB/day) is throttled
  to `HOURLY_FORECAST_MIN_INTERVAL` (3 h) regardless of how often a new run
  appears, and the current hour's symbol drives the entity `condition` when
  available. Toggling the option reloads the entry.

## [v0.1.0] — 2026-08-27

First release: the integration produces a live `weather` entity per Swiss
postal code.

### Added

- **`weather` entity** with current conditions (temperature, humidity,
  pressure, wind speed and direction, precipitation) sourced from the
  nearest SwissMetNet station (10-minute values) and a 9-day daily forecast
  sourced from the MeteoSwiss local-forecast file for the configured point.
  Hourly forecast is a per-entry option (off by default; ADR-0002).
- **`ogd/` client package** — pure Python (no HA imports): STAC catalogue
  discovery, station CSV download and parsing, local-forecast CSV download
  and parsing, weather-symbol mapping to HA condition strings.
- **Three-step config flow**: postal-code entry → forecast point selection
  → nearest-station confirmation. Options flow lets users toggle hourly
  forecast.
- Station and forecast `DataUpdateCoordinator`s with conditional HTTP
  requests and executor-offloaded CSV parsing.

### Fixed

- CI: the Claude agent workflows check out with `persist-credentials: false`.
  `actions/checkout@v7` keeps `GITHUB_TOKEN` in an `includeIf` credentials
  file that `claude-code-action` does not clear, so the reviewer's fix commit
  on PR #26 was pushed as `github-actions[bot]` and its CI runs waited for a
  manual approval instead of letting auto-merge proceed.

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

[Unreleased]: https://github.com/chriguschneider/hass-meteoswiss-weather/compare/v0.1.0...HEAD
[v0.1.0]: https://github.com/chriguschneider/hass-meteoswiss-weather/compare/v0.0.1...v0.1.0
[v0.0.1]: https://github.com/chriguschneider/hass-meteoswiss-weather/releases/tag/v0.0.1
