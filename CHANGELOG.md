# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Release tags carry a `v` prefix (e.g. `v0.1.0`); the release workflow
(`.github/workflows/release.yml`) turns an annotated tag into a GitHub release
using the matching section below as release notes.

## [Unreleased]

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

[Unreleased]: https://github.com/chriguschneider/hass-meteoswiss-weather/compare/v0.0.1...HEAD
[v0.0.1]: https://github.com/chriguschneider/hass-meteoswiss-weather/releases/tag/v0.0.1
