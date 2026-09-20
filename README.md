<h1 align="center">MeteoSwiss Weather</h1>

<p align="center"><em>The official MeteoSwiss open data, as a Home Assistant weather entity.</em></p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-yellow.svg" /></a>
  <a href="https://hacs.xyz/"><img alt="HACS Custom" src="https://img.shields.io/badge/HACS-Custom-orange.svg" /></a>
  <a href="https://github.com/chriguschneider/hass-meteoswiss-weather/actions/workflows/ci.yml"><img alt="CI status" src="https://img.shields.io/github/actions/workflow/status/chriguschneider/hass-meteoswiss-weather/ci.yml?branch=master&label=CI" /></a>
  <a href="https://sonarcloud.io/summary/overall?id=chriguschneider_hass-meteoswiss-weather&branch=master"><img alt="Quality Gate Status" src="https://sonarcloud.io/api/project_badges/measure?project=chriguschneider_hass-meteoswiss-weather&metric=alert_status" /></a>
  <a href="https://github.com/chriguschneider/hass-meteoswiss-weather/commits/master"><img alt="Last commit" src="https://img.shields.io/github/last-commit/chriguschneider/hass-meteoswiss-weather" /></a>
  <a href="#ai-assisted-development"><img alt="AI Assisted" src="https://img.shields.io/badge/AI-assisted-2196F3.svg" /></a>
</p>

## What it does

- **A `weather` entity per Swiss postal code**: current conditions from the
  nearest SwissMetNet station (10-minute values) and the same 9-day local
  forecast the MeteoSwiss app shows, with the app's weather symbols — each day
  carrying temperature high/low, precipitation, precipitation probability and
  wind.
- **Hourly forecast as an option** — off by default, because of what it
  costs (see below).
- **Station sensors**: temperature, humidity, dew point, pressure, wind,
  gusts, precipitation, sunshine and radiation from the chosen SwissMetNet
  station, refreshed every 10 minutes.
- **No YAML.** UI setup, picks the forecast point and station from your
  Home Assistant location, lets you override both.

## What data you get

Everything below comes from three MeteoSwiss open-data collections: the
SwissMetNet station files (10-minute observations), the per-point local
forecast (republished hourly, ~220 hours and 9 days ahead) and, as an opt-in,
the automatic pollen network. Every entity carries the attribution
*Source: MeteoSwiss*. Entities marked **off** are created but disabled in the
entity registry — enable them on the device page when you need them.

### The `weather` entity

One per config entry, named after the forecast point.

**Current conditions** — from the chosen SwissMetNet station, refreshed every
10 minutes:

| Attribute | Description | Unit |
|---|---|---|
| `temperature` | Air temperature at 2 m | °C |
| `humidity` | Relative humidity at 2 m | % |
| `dew_point` | Dew point at 2 m | °C |
| `pressure` | Pressure reduced to sea level (QFF) | hPa |
| `wind_speed` | 10-minute mean wind speed | km/h |
| `wind_bearing` | 10-minute mean wind direction, 0 = north | ° |
| `wind_gust_speed` | Peak gust (1 s) in the last 10 minutes | km/h |
| `condition` | Home Assistant condition (`sunny`, `rainy`, `snowy`, …) mapped from the MeteoSwiss weather symbol: the current hour's hourly symbol when the hourly option is on and cached, otherwise today's daily symbol. The day/night variant follows `sun.sun`. | — |
| `current_precipitation` | Precipitation sum of the last 10 minutes | mm |
| `precipitation_station` | Name of the precipitation-only station, present only when one is configured; `current_precipitation` then comes from it | — |

**Daily forecast** — 9 days, always on, refreshed when a new forecast run
lands (checked hourly). Per day:

| Field | Description | Unit |
|---|---|---|
| `condition` | Daytime variant of the day's MeteoSwiss symbol | — |
| `temperature` / `templow` | Daily maximum / minimum temperature (local calendar day) | °C |
| `precipitation` | Daily precipitation sum | mm |
| `wind_speed` | Highest hourly mean wind speed of the day | km/h |
| `wind_gust_speed` | Highest hourly gust of the day | km/h |
| `wind_bearing` | Wind direction at the hour of the strongest wind | ° |

**Hourly forecast** — opt-in ([why](#why-another-meteoswiss-integration)).
With the option on it is fetched on every forecast refresh, so the data is
always there when a card opens. Per hour:

| Field | Description | Unit |
|---|---|---|
| `condition` | MeteoSwiss hourly symbol, day/night variant as sent | — |
| `temperature` | Air temperature at 2 m (median forecast) | °C |
| `precipitation` | Hourly precipitation sum | mm |
| `precipitation_probability` | Probability of precipitation in the 3-hour window ending at that hour | % |
| `wind_speed` / `wind_gust_speed` / `wind_bearing` | Hourly mean wind, gust and direction | km/h, km/h, ° |
| `radiation` | Global (incoming short-wave) solar radiation | W/m² |
| `zero_degree_level` | Altitude of the 0 °C isotherm (snow-line material) | m |
| `cloud_coverage` | Total cloud cover, the maximum of the three layers — **only with the cloud-layers option** | % |
| `cloud_coverage_high` / `_mid` / `_low` | The three cloud layers — **only with the cloud-layers option** | % |
| `temperature_p10` / `temperature_p90` | 10th / 90th percentile of the temperature forecast, the uncertainty band — **only with the temperature-percentiles option** | °C |

### Station sensors

One sensor per measured parameter of the chosen SwissMetNet station, refreshed
every 10 minutes. Only sensors the station actually measures are created; not
every station has every instrument.

| Sensor | Description | Unit | Default |
|---|---|---|---|
| Temperature | Air temperature at 2 m | °C | on |
| Humidity | Relative humidity at 2 m | % | on |
| Pressure QFF | Pressure reduced to sea level (the value forecasts use) | hPa | on |
| Wind speed | 10-minute mean | km/h | on |
| Wind bearing | 10-minute mean direction, 0 = north | ° | on |
| Wind gust speed | Peak gust (1 s) in the last 10 minutes | km/h | on |
| Precipitation (10 min) | Sum of the last 10 minutes; from the optional precipitation-only station when one is configured (a `station` attribute then names it) | mm | on |
| Dew point | Dew point at 2 m | °C | off |
| Pressure QFE | Pressure at station level, not reduced | hPa | off |
| Pressure QNH | Pressure reduced to sea level with the ICAO standard atmosphere (aviation) | hPa | off |
| Sunshine duration (10 min) | Minutes of sunshine in the last 10 minutes | min | off |
| Global radiation | Incoming short-wave solar radiation, 10-minute mean | W/m² | off |
| Diffuse radiation | Diffuse part of the solar radiation | W/m² | off |
| Longwave radiation | Incoming long-wave (thermal) radiation | W/m² | off |
| Snow depth | Automatically measured snow depth | cm | off |
| Wind chill | Perceived temperature from wind and air temperature | °C | off |
| Air temperature (5 cm) | Temperature just above the ground — ground-frost indicator | °C | off |
| Soil temperature (5 / 10 / 20 cm) | Three soil temperature depths, one sensor each | °C | off |
| Measurement time | Timestamp of the last delivered observation (diagnostic) | — | off |

### Forecast sensors

Derived from the local forecast for the configured point.

| Sensor | Description | Unit | Default |
|---|---|---|---|
| High temperature today | Today's forecast maximum; flips to the new day at local midnight | °C | on |
| Low temperature today | Today's forecast minimum | °C | on |
| Precipitation today | Today's forecast precipitation sum | mm | on |
| Zero-degree level | Forecast altitude of the 0 °C isotherm for the current hour — snow-line material; advances every hour. Works without the hourly forecast option | m | off |

### Pollen sensors

Opt-in in the options. Hourly concentrations from the nearest of the 15
automatic pollen stations (you can pick another), only for the taxa that
station measures.

| Sensor | Default |
|---|---|
| Grass pollen | on |
| Birch pollen | on |
| Alder pollen | off |
| Hazel pollen | off |
| Beech pollen | off |
| Ash pollen | off |
| Oak pollen | off |

All in grains/m³.

### Diagnostic traffic sensors

Two diagnostic sensors on the forecast device let you watch the integration's daily traffic budget:

| Sensor | Default | Unit |
|---|---|---|
| Data fetched today | on | MB |
| Requests today | off | — |

Both reset at local midnight and survive a restart. No extra traffic — they count bytes/requests the fetch ladder already tracks.

### Service

`meteoswiss_weather.import_history` imports the station's official hourly
history into Home Assistant's long-term statistics (temperature mean/min/max,
means for humidity, dew point, pressure, wind, gust and radiation, hourly sums
for precipitation), so statistics graphs reach back before the install. One-off
download, details in [CONFIGURATION.md](docs/CONFIGURATION.md#services).

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
  default. The hourly option is opt-in; with it on, the data is fetched on the
  integration's own forecast refresh (whether or not a card is open), using
  HTTP Range requests so only the configured point's rows are downloaded: a
  contiguous block where a file is sorted by point, the point's row inside
  each hour block where it is sorted by time. A small canary read decides
  whether a new run changed anything; MeteoSwiss adjusts the coming hours
  about every hour, so expect roughly one refresh per hour. Measured in
  September 2026 with everything switched on: about **1.2 MB per refresh,
  some 30 MB a day**, instead of the ~1 GB/day a full hourly download would
  cost. A `hourly_horizon_days` option trades horizon for traffic; horizons
  beyond the default read much larger parts of the files. MeteoSwiss has announced a per-point API for the end of 2026, after
  which even this goes away
  ([ADR-0002](docs/adr/0002-traffic-budget-bulk-local-forecast.md)).

[**docs/comparison.md**](docs/comparison.md) puts this side by side with
`Rudd-O/homeassistant-meteoswiss` and `izacus/hass-swissweather`, feature
by feature, including the things they do better.

## Install

Install via [HACS](https://hacs.xyz/) as a **custom repository**:

1. In Home Assistant, go to **HACS → Integrations → ⋮ → Custom repositories**.
2. Add `https://github.com/chriguschneider/hass-meteoswiss-weather` as an
   **Integration**.
3. Search for *MeteoSwiss Weather* and install it.
4. Restart Home Assistant, then go to **Settings → Devices & Services → Add
   integration** and search for *MeteoSwiss Weather*.

Or use the My-link shortcut:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=chriguschneider&repository=hass-meteoswiss-weather&category=integration)

## Configuration

See [**CONFIGURATION.md**](docs/CONFIGURATION.md) for setup steps, the hourly forecast option and its cost, entity references, dashboard examples, and a FAQ (warnings, traffic, station selection, etc.).

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
