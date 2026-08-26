<h1 align="center">MeteoSwiss Weather</h1>

<p align="center"><em>The official MeteoSwiss open data, as a Home Assistant weather entity.</em></p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-yellow.svg" /></a>
  <a href="https://hacs.xyz/"><img alt="HACS Custom" src="https://img.shields.io/badge/HACS-Custom-orange.svg" /></a>
  <a href="https://github.com/chriguschneider/hass-meteoswiss-weather/actions/workflows/ci.yml"><img alt="CI status" src="https://img.shields.io/github/actions/workflow/status/chriguschneider/hass-meteoswiss-weather/ci.yml?branch=master&label=CI" /></a>
  <a href="https://github.com/chriguschneider/hass-meteoswiss-weather/commits/master"><img alt="Last commit" src="https://img.shields.io/github/last-commit/chriguschneider/hass-meteoswiss-weather" /></a>
  <a href="#ai-assisted-development"><img alt="AI Assisted" src="https://img.shields.io/badge/AI-assisted-2196F3.svg" /></a>
</p>

> **Status: under construction.** This repository holds the scaffold, the
> architecture decisions and the plan. There is nothing to install yet.
> Progress is tracked in
> [issue #1](https://github.com/chriguschneider/hass-meteoswiss-weather/issues/1).

## What it will do

- **A `weather` entity per Swiss postal code**: current conditions from the
  nearest SwissMetNet station (10-minute values) and the same 9-day local
  forecast the MeteoSwiss app shows, with the app's weather symbols.
- **Hourly forecast as an option** — off by default, because of what it
  costs (see below).
- **Station sensors**: temperature, humidity, dew point, pressure, wind,
  gusts, precipitation, sunshine, radiation.
- **No YAML.** UI setup, picks the forecast point and station from your
  Home Assistant location, lets you override both.

## Why another MeteoSwiss integration

Every existing one reads the undocumented backend of the MeteoSwiss mobile
app, which breaks whenever the app changes. Since 2025 MeteoSwiss publishes
its data officially — CC BY 4.0, no API key, announced changes — and since
September 2025 that includes the per-postal-code local forecast. This
integration reads **only** that open data ([ADR-0001](docs/adr/0001-official-open-data-only-upstream.md)).

Two honest consequences:

- **Weather warnings are not in the open data.** Use Home Assistant's core
  [MeteoAlarm](https://www.home-assistant.io/integrations/meteoalarm/)
  integration for them (regional, official).
- **The hourly forecast is published as whole-of-Switzerland files, about
  30 MB per parameter per hour.** Daily forecasts are tiny and are the
  default; the hourly option is refreshed at most every three hours and
  costs roughly 1 GB per day. MeteoSwiss has announced a per-point API for
  the end of 2026, after which this limitation goes away
  ([ADR-0002](docs/adr/0002-traffic-budget-bulk-local-forecast.md)).

## Install

Not yet. Once the first release exists it will be installable through HACS
as a custom repository, and the default store will be requested after that.

## The radar

The animated precipitation radar lives in the sibling integration
[**MeteoSwiss Radar**](https://github.com/chriguschneider/hass-meteoswiss-radar)
— same author, same icon, deliberately a separate install
([ADR-0003](docs/adr/0003-sibling-of-the-radar-integration.md)).

## Contributing

Issues and PRs welcome. [AGENTS.md](AGENTS.md) has the working agreement,
[docs/ogd.md](docs/ogd.md) has the measured facts about the upstream files,
and `ruff check custom_components tests scripts` / `pytest -q` run the checks.

Much of the backlog is worked by Claude agents through GitHub Actions — see
[docs/agent-automation.md](docs/agent-automation.md) for how an issue gets
picked up, reviewed and merged.

## AI-assisted development

Built by Chrigu & Claude — a human and an LLM working together. The
architecture calls, the data measurements and the trade-offs are mine; a
good share of the typing, refactors and tests is
[Claude Code](https://claude.com/claude-code), in this clone and as
unattended agents on GitHub.

AI-assisted commits carry a `Co-Authored-By:` trailer, so the history stays
honest.

## Attribution & licence

Code: [MIT](LICENSE).

Data: © MeteoSwiss, published under
[CC BY 4.0](https://opendatadocs.meteoswiss.ch/general/terms-of-use).
Every entity carries the attribution *Source: MeteoSwiss*. This project is
not affiliated with or endorsed by MeteoSwiss.
