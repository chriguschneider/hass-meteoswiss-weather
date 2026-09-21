<h1 align="center">MeteoSwiss Weather</h1>

<p align="center"><em>The MeteoSwiss forecast for your postal code, from the official open data.</em></p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-yellow.svg" /></a>
  <a href="https://hacs.xyz/"><img alt="HACS Custom" src="https://img.shields.io/badge/HACS-Custom-orange.svg" /></a>
  <a href="https://github.com/chriguschneider/hass-meteoswiss-weather/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/chriguschneider/hass-meteoswiss-weather" /></a>
  <a href="https://github.com/chriguschneider/hass-meteoswiss-weather/actions/workflows/ci.yml"><img alt="CI status" src="https://img.shields.io/github/actions/workflow/status/chriguschneider/hass-meteoswiss-weather/ci.yml?branch=master&label=CI" /></a>
  <a href="https://sonarcloud.io/summary/overall?id=chriguschneider_hass-meteoswiss-weather&branch=master"><img alt="Quality Gate Status" src="https://sonarcloud.io/api/project_badges/measure?project=chriguschneider_hass-meteoswiss-weather&metric=alert_status" /></a>
  <a href="https://github.com/chriguschneider/hass-meteoswiss-weather/releases"><img alt="Downloads" src="https://img.shields.io/github/downloads/chriguschneider/hass-meteoswiss-weather/total" /></a>
  <a href="https://github.com/chriguschneider/hass-meteoswiss-weather/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/chriguschneider/hass-meteoswiss-weather?style=flat&label=stars" /></a>
  <a href="https://github.com/chriguschneider/hass-meteoswiss-weather/commits/master"><img alt="Last commit" src="https://img.shields.io/github/last-commit/chriguschneider/hass-meteoswiss-weather" /></a>
  <a href="https://buymeacoffee.com/chriguschneider"><img alt="Buy Me a Coffee" src="https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-FFDD00.svg" /></a>
  <a href="#ai-assisted-development"><img alt="AI Assisted" src="https://img.shields.io/badge/AI-assisted-2196F3.svg" /></a>
</p>

<p align="center">
  <a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=chriguschneider&repository=hass-meteoswiss-weather&category=integration"><img src="https://my.home-assistant.io/badges/hacs_repository.svg" alt="Open in HACS" /></a>
  &nbsp;·&nbsp;
  <a href="docs/CONFIGURATION.md">Configuration</a>
  &nbsp;·&nbsp;
  <a href="docs/comparison.md">Comparison</a>
  &nbsp;·&nbsp;
  <a href="https://github.com/chriguschneider/hass-meteoswiss-weather/issues">Issues</a>
  &nbsp;·&nbsp;
  <a href="CHANGELOG.md">Changelog</a>
</p>

<!-- Screenshot goes here once docs/images/weather-card.png exists:
<p align="center">
  <img
    src="https://raw.githubusercontent.com/chriguschneider/hass-meteoswiss-weather/master/docs/images/weather-card.png"
    alt="The Home Assistant weather card showing the MeteoSwiss forecast for a Swiss postal code"
    width="440"
  />
</p>
-->

If you live in Switzerland, the MeteoSwiss app is probably where you look before
you plan the weekend. This puts that same forecast into Home Assistant, for your
postal code or your favourite summit, read from the data MeteoSwiss publishes
officially.

- **The forecast you know from the app.** Nine days for your postal code, with
  the MeteoSwiss weather symbols, highs and lows, rain, chance of rain and wind.
- **What is happening right now.** Temperature, humidity, pressure, wind, gusts
  and rain from the nearest SwissMetNet station, fresh every 10 minutes.
- **Mountains too.** Pick one of 631 summits, passes and resorts instead of a
  postal code, and add as many places as you like.
- **Hour by hour, if you want it.** Temperature, rain, wind, solar radiation for
  your PV forecast, cloud layers and the zero-degree level, for the next days.
- **Pollen** from the automatic MeteoSwiss pollen network, as an option.
- **No YAML and no API key.** Setup is a few clicks, in English, German, French
  or Italian, and it finds your place from your Home Assistant location.
- **Built on the official open data**, not on the app's private backend, so it
  does not break when the app changes.

## Install

The integration is installed through [HACS](https://hacs.xyz/) as a custom
repository. The button does the first two steps for you:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=chriguschneider&repository=hass-meteoswiss-weather&category=integration)

Or by hand:

1. In Home Assistant, open **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/chriguschneider/hass-meteoswiss-weather` as an **Integration**.
3. Search for **MeteoSwiss Weather** and download it.
4. Restart Home Assistant.
5. Go to **Settings → Devices & Services → Add integration** and search for
   **MeteoSwiss Weather**.

Setup asks three things: postal code or mountain point, which forecast point if
your postal code has several, and which weather station to use for the current
values. The nearest one is preselected every time, so you can mostly press
*Next*.

Needs Home Assistant **2024.7.0 or newer**.

## Put it on a dashboard

The standard weather card works as it is. Set `forecast_type: hourly` once the
hourly option is on:

```yaml
type: weather-forecast
entity: weather.your_place
forecast_type: daily
```

More examples, including an ApexCharts forecast and a current-conditions grid,
are in [docs/CONFIGURATION.md](docs/CONFIGURATION.md#dashboard-examples).

## Options, and what they cost

Everything beyond the daily forecast is optional. You find the options under
**Settings → Devices & Services → MeteoSwiss Weather → Configure**; the dialog
tells you for every choice what you get and shows an estimate of the traffic
before you save.

| Option | Gives you | Per refresh |
|---|---|---|
| *(always on)* | daily forecast, current conditions, zero-degree level | ~0.35 MB |
| Hourly forecast | temperature, rain, chance of rain, wind, radiation and zero-degree level for every hour | + ~0.3 MB |
| Cloud cover layers | total cloud cover and the high, mid and low layers per hour | + ~0.05 MB |
| Temperature percentiles | the uncertainty band of the temperature forecast per hour | + ~0.5 MB |
| Pollen | one sensor per pollen type the station measures | one small file per hour |

Measured in September 2026 for one place. MeteoSwiss adjusts the coming hours
about once an hour, so with everything switched on expect around **1.2 MB per
hour, some 30 MB a day**. The sensor *Data fetched today* shows the real number
for your installation.

Why this matters: MeteoSwiss publishes the hourly forecast as files of about
30 MB for the whole of Switzerland, one per value and hour. The integration
reads only the rows of your place out of them instead of downloading them. How
that works, and what happens when MeteoSwiss rearranges a file, is written down
in [ADR-0008](docs/adr/0008-run-scoped-forecast-store.md).

## What you get

Every entity carries the attribution *Source: MeteoSwiss*. Many sensors are
created **disabled** so your entity list stays tidy: open the device page and
enable the ones you want. The complete map from each setting to the entities
and fields it produces is in
[docs/CONFIGURATION.md](docs/CONFIGURATION.md#what-each-setting-controls).

<details>
<summary><b>The <code>weather</code> entity: current conditions, daily and hourly forecast</b></summary>

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

</details>

<details>
<summary><b>Station sensors (SwissMetNet, every 10 minutes)</b></summary>

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

</details>

<details>
<summary><b>Forecast sensors, including the zero-degree level</b></summary>

Derived from the local forecast for the configured point.

| Sensor | Description | Unit | Default |
|---|---|---|---|
| High temperature today | Today's forecast maximum; flips to the new day at local midnight | °C | on |
| Low temperature today | Today's forecast minimum | °C | on |
| Precipitation today | Today's forecast precipitation sum | mm | on |
| Zero-degree level | Forecast altitude of the 0 °C isotherm for the current hour — snow-line material; advances every hour. Works without the hourly forecast option | m | off |

</details>

<details>
<summary><b>Pollen sensors (opt-in)</b></summary>

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

</details>

<details>
<summary><b>Diagnostic traffic sensors</b></summary>

Two diagnostic sensors on the forecast device let you watch the integration's daily traffic budget:

| Sensor | Default | Unit |
|---|---|---|
| Data fetched today | on | MB |
| Requests today | off | — |

Both reset at local midnight and survive a restart. No extra traffic — they count bytes/requests the fetch ladder already tracks.

</details>

<details>
<summary><b>Service: import the station's history</b></summary>

`meteoswiss_weather.import_history` imports the station's official hourly
history into Home Assistant's long-term statistics (temperature mean/min/max,
means for humidity, dew point, pressure, wind, gust and radiation, hourly sums
for precipitation), so statistics graphs reach back before the install. One-off
download, details in [CONFIGURATION.md](docs/CONFIGURATION.md#services).

</details>

## Good to know

- **Switzerland only.** The data covers Swiss postal codes, mountain points and
  stations.
- **No weather warnings.** They are not part of the open data. Home Assistant's
  own [MeteoAlarm](https://www.home-assistant.io/integrations/meteoalarm/)
  integration has them.
- **It never shows "unknown" to save traffic.** If a cheap read cannot prove it
  got every hour, the integration reads more, up to the whole file, and raises a
  repair notice so you see it. MeteoSwiss has rearranged its files twice without
  notice, which is why every read is checked.
- **A simpler source is coming.** MeteoSwiss has announced a service that answers
  for a single place, as a beta by the end of 2026. The integration is built so
  that switching to it will not change your entities.
- **Something looks wrong?** *Settings → Devices & Services → MeteoSwiss
  Weather → ⋮ → Download diagnostics* lists every file, when it was read and
  how. Attach it to an [issue](https://github.com/chriguschneider/hass-meteoswiss-weather/issues).

## Why another MeteoSwiss integration

The other integrations read the undocumented backend of the MeteoSwiss mobile
app, which can change with any app release. Since 2025 MeteoSwiss publishes its
data officially, free to use with attribution and with announced changes, and
since September 2025 that includes the forecast per postal code. This
integration reads **only** that open data
([ADR-0001](docs/adr/0001-official-open-data-only-upstream.md)).

[docs/comparison.md](docs/comparison.md) puts it side by side with
`Rudd-O/homeassistant-meteoswiss` and `izacus/hass-swissweather`, feature by
feature, including the things they do better.

## Goes well with

- [**MeteoSwiss Radar**](https://github.com/chriguschneider/hass-meteoswiss-radar):
  the app's animated rain radar as a dashboard card. Same author, same icon,
  deliberately a separate install
  ([ADR-0003](docs/adr/0003-sibling-of-the-radar-integration.md)).
- [**Weather Station Card**](https://github.com/chriguschneider/weather-station-card):
  one chart with what was measured on the left and what is forecast on the
  right. This integration delivers both halves.

## Contributing

Issues and pull requests are welcome. [AGENTS.md](AGENTS.md) has the working
agreement, [docs/ogd.md](docs/ogd.md) the measured facts about the MeteoSwiss
files, and `ruff check custom_components tests scripts` and `pytest -q` run the
checks. Corrections to the German, French and Italian texts from native
speakers are a good first contribution.

Much of the backlog is worked by Claude agents through GitHub Actions.
[docs/agent-automation.md](docs/agent-automation.md) explains how an issue gets
picked up, reviewed and merged.

## AI-assisted development

Built by Chrigu & Claude, a human and an LLM working together. The architecture
calls, the measurements of the MeteoSwiss data and the trade-offs are mine; a
good share of the typing, refactors and tests is
[Claude Code](https://claude.com/claude-code), on my machine and as unattended
agents on GitHub.

AI-assisted commits carry a `Co-Authored-By:` trailer, so the history stays
honest. The badge is there because being upfront about how software gets made
beats pretending otherwise.

If the integration earned a place in your Home Assistant,
[a coffee](https://buymeacoffee.com/chriguschneider) is a nice way to say
thanks. (Claude doesn't drink coffee. More for me.)

## Attribution & licence

Data: © [MeteoSwiss](https://www.meteoswiss.admin.ch), published under
[CC BY 4.0](https://opendatadocs.meteoswiss.ch/general/terms-of-use). Every
entity carries the attribution *Source: MeteoSwiss*. An independent community
project, not affiliated with or endorsed by MeteoSwiss.

Code: MIT, see [LICENSE](LICENSE).
