# Configuration and Usage

## Setup

The integration uses a simple setup flow, with no YAML required. Go to **Settings → Devices & Services → Add Integration** and search for *MeteoSwiss Weather*.

### Step 1: Location Type

Choose the type of forecast point:

- **Postal code** (default) — forecast for a Swiss town or city, identified by its 4-digit PLZ.
- **Mountain point** — forecast for a summit, mountain pass, or ski resort from the MeteoSwiss alpine point list (631 points).

### Postal-Code Path

#### Step 2a: Postal Code

Enter your Swiss postal code (4 digits). The setup flow pre-fills this with the postal code of your Home Assistant location, but you can override it.

#### Step 3a: Forecast Point (if needed)

If your postal code has multiple forecast points, you'll be asked to choose one. This is typical for larger cities where the weather can differ by neighbourhood. The default is the first point; most users never see this screen.

### Mountain-Point Path

#### Step 2b: Mountain Point

Choose a mountain point from the dropdown list. All 631 alpine forecast points are listed alphabetically, labelled `"<name> (<altitude> m)"`. The nearest point to your Home Assistant location is pre-selected.

**Example (ski-area use case):** Add a second config entry for the mountain above your ski area. Pick the nearest summit or glacier point — you'll get a 9-day forecast for that exact altitude, including temperature, snow, wind, and the MeteoSwiss weather symbol. Pair it with the nearest mountain SwissMetNet station for current conditions; once snow-depth support lands (#47) you'll have live snow depth too.

### Step 3: Weather Station

Choose a SwissMetNet weather station to provide current conditions. The setup flow shows the three nearest stations with the closest one pre-selected. You can override it if you prefer a different station (e.g. one with better elevation or terrain match).

#### Optional: separate precipitation station

The same step offers an optional **precipitation station** from the automatic precipitation-only network (`ch.meteoschweiz.ogd-smn-precip`, ~141 gauges — denser than SwissMetNet). Rain is hyper-local, so a nearer rain gauge is often more representative than the main station's. The three nearest precipitation stations are offered and **none is selected by default** — the feature is opt-in.

When set:

- the current **precipitation** (the `precipitation` sensor and the weather entity's `current_precipitation` attribute) is read from this station;
- **every other value** — temperature, humidity, pressure, wind, condition — stays with the main station;
- the precipitation sensor's attribution and a `station` attribute name the precipitation station it reads;
- a second small (~1.2 KB) file is polled every 10 minutes, conditionally, only while the option is set — inside the station traffic budget ([ADR-0002](adr/0002-traffic-budget-bulk-local-forecast.md), [ADR-0006](adr/0006-optional-precipitation-station.md)).

The pick can be changed (or cleared) later with **Reconfigure**.

## Entities

### Weather Entity

One `weather` entity per config entry.

**Attributes:**

| Attribute | Value | Unit |
|-----------|-------|------|
| `temperature` | Current temperature from the station | °C |
| `humidity` | Relative humidity from the station | % |
| `dew_point` | Dew point from the station | °C |
| `pressure` | Atmospheric pressure (QFF, reduced to sea level) from the station | hPa |
| `wind_speed` | 10-minute mean wind speed from the station | km/h |
| `wind_bearing` | Wind direction from the station | ° (0–360, where 0 is north) |
| `wind_gust_speed` | Peak wind gust from the station | km/h |
| `condition` | Weather condition (`sunny`, `partlycloudy`, `cloudy`, `rainy`, `snowy`, etc.) | — |
| `current_precipitation` | Current 10-minute precipitation (from the precipitation station when one is configured, otherwise the main station) | mm |

**Forecast:**

- **Daily forecast**: 9 days, always available. Temperature high/low, precipitation, precipitation probability, wind (speed, gust, bearing), and weather condition for each day.
- **Hourly forecast**: When enabled in options (see below). Per hour:

  | Key | Description | Unit |
  |---|---|---|
  | `condition` | MeteoSwiss hourly symbol (day/night variant as sent) | — |
  | `temperature` | Air temperature at 2 m (median) | °C |
  | `precipitation` | Hourly precipitation sum | mm |
  | `precipitation_probability` | Probability of precipitation in the 3-hour window ending at that hour | % |
  | `wind_speed` / `wind_gust_speed` / `wind_bearing` | Hourly mean wind, gust and direction | km/h, km/h, ° |
  | `radiation` | Global (incoming short-wave) solar radiation | W/m² |
  | `zero_degree_level` | Altitude of the 0 °C isotherm (snow-line material) | m |
  | `cloud_coverage` | Total cloud cover, maximum of the three layers — **only with the cloud-layers option** | % |
  | `cloud_coverage_high` / `_mid` / `_low` | The three cloud layers — **only with the cloud-layers option** | % |
  | `temperature_p10` / `temperature_p90` | 10th/90th percentile of the temperature forecast — **only with the temperature-percentiles option** | °C |

  **Example — next-hour radiation from a template:**
  ```yaml
  {{ state_attr('weather.MY_ENTITY', 'forecast') }}
  ```
  Or with the `weather.get_forecasts` service:
  ```yaml
  action: weather.get_forecasts
  data:
    type: hourly
  target:
    entity_id: weather.MY_ENTITY
  response_variable: hourly
  ```
  Each entry in `hourly['weather.MY_ENTITY']['forecast']` carries `radiation` (W/m²)
  and `zero_degree_level` (m) alongside the standard fields.

### Station Sensors

One sensor entity per measured field from the SwissMetNet station. All are disabled by default except the most common ones (temperature, humidity, pressure, wind speed, wind bearing, gust speed, precipitation).

| Sensor | Unit | Enabled by Default | Notes |
|--------|------|-------|-------|
| **Temperature** | °C | Yes | 2-metre temperature |
| **Humidity** | % | Yes | Relative humidity |
| **Pressure (QFF)** | hPa | Yes | Sea-level reduced pressure |
| **Wind Speed** | km/h | Yes | 10-minute mean |
| **Wind Bearing** | ° | Yes | 0–360, where 0 is north |
| **Gust Speed** | km/h | Yes | Peak gust |
| **Precipitation** | mm | Yes | 10-minute total; from the precipitation station when one is configured (see Step 3) |
| **Dew Point** | °C | No | Diagnostic |
| **Pressure (QFE)** | hPa | No | Station-level pressure |
| **Sunshine Duration** | min | No | 10-minute total |
| **Global Radiation** | W/m² | No | Solar radiation |

## Options

Open **Settings → Devices & Services → MeteoSwiss Weather → the entry → Configure**.
The options dialog is a menu with three entries:

- **Hourly forecast** — the toggle, horizon, cloud layers and temperature
  percentiles on one page (the horizon and extras are ignored while the toggle
  is off).
- **Pollen monitoring** — the toggle and the pollen station on one page.
- **Overview** — a read-only summary of what is on now, which forecast fields it
  produces, and how many of the entry's entities are currently created disabled
  (see [Entities that are disabled by default](#entities-that-are-disabled-by-default)).

Each page saves only its own options, so changing the hourly settings never
resets the pollen settings or vice versa.

### Hourly Forecast

**Default:** Off

**Gives you:** an hourly forecast on the weather entity — condition, temperature, precipitation and its probability, wind, gusts, direction, global radiation (`radiation`) and the zero-degree level (`zero_degree_level`) — and the entity's current condition follows the hourly symbol. **Not needed for** the daily forecast, the zero-degree level sensor and the "today" sensors; those work without it.

**How it works:** with the option on, the hourly data is fetched on the integration's own forecast refresh, whether or not a card is open, so it is always ready (an idle instance pays for it too — ADR-0008). Every hour the integration checks for a new run and reads a few rows as a canary; only when those differ from what it holds does it refresh. MeteoSwiss adjusts the coming hours about hourly, so in practice the hourly data refreshes about once an hour while the daily files are re-read far less often.

**What it costs** (measured 2026-09-20 for one location, warm, default horizon):

| part | per refresh |
|---|---|
| without the hourly option (daily forecast, daily wind and probability, zero-degree level) | ~0.35 MB |
| hourly forecast | ~0.3 MB |
| + cloud cover layers | ~0.05 MB |
| + temperature percentiles | ~0.5 MB |
| everything on | **~1.2 MB, about 30 MB a day** |

The integration's diagnostics download lists, per file, the run, the bytes and the number of requests of the last fetch, so you can see the real numbers for your location.

**Horizon (`hourly_horizon_days`):** counted in full local calendar days; the default is the rest of today plus two full days (49–72 hours). Up to there every file is read row by row. Longer horizons and "Full run" (~220 hours) read a much larger part of the ~30 MB files on every refresh — choose them only if you need them.

**Cloud cover layers:** adds `cloud_coverage` (the maximum of the three layers) and `cloud_coverage_high` / `_mid` / `_low` to every forecast hour.

**Temperature percentiles:** adds `temperature_p10` and `temperature_p90` to every forecast hour. This is the most expensive extra.

### Pollen

**Default:** Off. Adds one sensor per pollen type the chosen station measures (grains/m³). Grass and birch are enabled; the other types are created disabled — enable them in the entity settings. One small file per hour.

### Entities that are disabled by default

Many sensors are created but **disabled** until you enable them under *Settings → Devices & Services → MeteoSwiss Weather → entities*: the zero-degree level, the measurement time, most station sensors (radiation, soil temperatures, pressure variants, snow depth, …) and most pollen types. No option in the dialog controls them, and enabling one costs no extra traffic.

### Diagnostic traffic sensors

Two diagnostic sensors are always created under the forecast device:

| Entity key | Default | Unit | Notes |
|---|---|---|---|
| `data_fetched_today` | **Enabled** | MB | Bytes transferred since local midnight; `DATA_SIZE` device class, `total_increasing` state class |
| `requests_today` | Disabled | — | HTTP request count since local midnight; same reset behaviour |

Both counters reset at local midnight and survive a restart within the same day (state is persisted to `.storage`). They count bytes and requests that the fetch ladder already tracks — daily files, blocks, hourly files, canaries, and run discovery — so they add no extra network traffic. The counters feed from `BulkCsvBackend.pop_fetch_totals()`, which is called after each coordinator refresh.

## Services

### `meteoswiss_weather.import_history`

Imports the configured station's **official hourly history** into Home Assistant's **long-term statistics**, so the statistics graphs and the Energy/Statistics cards show data from before the integration was installed.

**What it writes:** long-term statistics only — **not** the raw short-term states. Under the integration's own sensor statistic ids it writes hourly mean/min/max for temperature, hourly mean for humidity, dew point, pressure, wind speed, gust and global radiation, and an hourly **sum** for precipitation. Only sensors that already exist for the entry are written.

**Overlaps:** re-running over a period you already imported **replaces** those hours rather than duplicating them, so the service is safe to run again.

**Fields:**

| field | required | default | meaning |
|---|---|---|---|
| `config_entry_id` | yes | — | which MeteoSwiss Weather entry (station) to import |
| `start` | no | 1 January of the current year | earliest time to import (local time when no timezone is given) |
| `end` | no | now | latest time to import (local time when no timezone is given) |

**Traffic (one-off, outside the recurring budget):** this is the only place the integration reads the history files, and only when you call it. The download is one file at a time (largest ≈ 13 MB), parsed in the executor. Rough sizes per station (ADR-0007): the **current year ≈ 1 MB**, a **past decade ≈ 8–13 MB**, and **everything since 1980 ≈ 45 MB**. Pick the smallest range you need.

The outcome (rows imported, statistics written, or the file that failed) is shown as a persistent notification.

```yaml
# Import the current year for one entry (find the entry id in the service UI).
service: meteoswiss_weather.import_history
data:
  config_entry_id: 0123456789abcdef0123456789abcdef

# Import a specific past range.
service: meteoswiss_weather.import_history
data:
  config_entry_id: 0123456789abcdef0123456789abcdef
  start: "2020-01-01 00:00:00"
  end: "2024-12-31 23:00:00"
```

## Dashboard Examples

### Weather Forecast Card

The standard Home Assistant weather card works out of the box:

```yaml
type: weather-forecast
entity: weather.postal_code_location
show_forecast_period: true
```

This displays the current conditions, today's forecast summary, and a daily forecast timeline.

### Daily Forecast with ApexCharts

For a more detailed daily view, use [ApexCharts Card](https://github.com/RomRider/apexcharts-card):

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: 9-Day Forecast
series:
  - entity: weather.postal_code_location
    type: line
    data_generator: |
      return entity.attributes.forecast.map((forecast) => {
        return [new Date(forecast.datetime).getTime(), forecast.temperature];
      });
    name: High
  - entity: weather.postal_code_location
    type: line
    data_generator: |
      return entity.attributes.forecast.map((forecast) => {
        return [new Date(forecast.datetime).getTime(), forecast.templow];
      });
    name: Low
```

### Current Conditions Grid

Display the key station measurements:

```yaml
type: grid
columns: 2
cards:
  - type: gauge
    entity: sensor.postal_code_location_temperature
    min: -20
    max: 40
  - type: gauge
    entity: sensor.postal_code_location_humidity
    min: 0
    max: 100
  - type: entity
    entity: sensor.postal_code_location_wind_speed
  - type: entity
    entity: sensor.postal_code_location_wind_bearing
```

## FAQ

### Why no weather warnings?

Weather warnings (thunderstorms, hail, heavy snow) are not in the official MeteoSwiss open data, and MeteoSwiss has not published a roadmap for them. For official regional warnings, use Home Assistant's core [MeteoAlarm](https://www.home-assistant.io/integrations/meteoalarm/) integration, which carries the authoritative CAP (Common Alerting Protocol) feed.

### Why is the hourly forecast off by default?

The hourly forecast is published as whole-of-Switzerland CSV files (30–33 MB per parameter per hour). The integration reads only your point's rows out of them (about 1 MB per refresh with everything on, see [Hourly Forecast](#hourly-forecast)), but that rests on how MeteoSwiss happens to sort the files, which is undocumented and has changed without notice. When a cheap read cannot prove it delivered every hour, the integration reads more, up to the whole file ([ADR-0008](adr/0008-run-scoped-forecast-store.md)), and raises a repair issue so you notice. MeteoSwiss has announced a per-point API (a beta by the end of 2026), which will remove the problem. Until then, hourly is an informed opt-in.

See [ADR-0002](adr/0002-traffic-budget-bulk-local-forecast.md) for the full context and measured file sizes.

### Why is the domain `meteoswiss_weather` and not `meteoswiss`?

The domain follows Home Assistant's [naming convention for weather integrations](https://developers.home-assistant.io/docs/creating_integration_manifest#manifest-reference): `<source>_weather`. This leaves room for future integrations that might expose other MeteoSwiss datasets (e.g., radar, pollen).

### How does the integration choose the nearest weather station?

The integration uses the haversine formula to calculate distances from your forecast point to all available SwissMetNet stations. It then selects the three nearest and displays them in the setup flow, with the closest pre-selected.

The forecast point is determined by your postal code. If your postal code has multiple forecast points, you choose one during setup; otherwise the setup flow skips to station selection.

**Note:** Not every station measures every parameter. If your chosen station does not measure a parameter (e.g., some precipitation-only sites lack pressure), that sensor will show as unknown. To pick a station with more complete measurements, use **Reconfigure** on the integration entry and choose a different station (see [Can I change the station after setup?](#can-i-change-the-station-after-setup) below).

### What does the weather condition mean?

The condition comes from the MeteoSwiss weather symbol code (`jp2000d0` from the daily forecast, or `jww003i0` from hourly data when available). The integration maps these codes to Home Assistant's standard conditions: `sunny`, `partlycloudy`, `cloudy`, `rainy`, `snowy`, `lightning-rainy`, etc.

The daily forecast uses the daytime symbol variant for consistency. The hourly condition, when available, uses the actual time-of-day symbol from MeteoSwiss. Whichever symbol is used, the entity's current condition is reconciled with `sun.sun`: with the sun up you get the day variant (`sunny`), with the sun down the night one (`clear-night`), even when the MeteoSwiss symbol still says otherwise. The conditions inside the *hourly forecast list* are left as MeteoSwiss sends them, since those are future hours.

### Can I change the station after setup?

Yes. Open **Settings → Devices & Services → MeteoSwiss Weather**, then the entry's three-dot menu → **Reconfigure**. The flow re-offers the postal code, forecast point and weather station with your current choices pre-selected, and updates the entry in place — the same entities and their history are kept, and no automations break.

When you change the **weather station** (but not when you only change the forecast point), the flow asks what to do with the history recorded so far, because those values came from the previous station:

- **Keep** (default): the entities and their history stay as they are. The values recorded before the switch came from the old station; a logbook entry records the moment of the switch so the seam is findable later.
- **Discard**: the station sensors' recorded states are purged and their long-term statistics are cleared — a clean start at the new station.
- **Backfill** *(when available)*: the long-term statistics are cleared and then rewritten from the new station's official historical files. Backfill affects **long-term statistics only**, not the raw short-term states. This choice appears once the statistics-import machinery ([ADR-0007](adr/0007-station-history-backfill.md)) ships; keep and discard are available now.

Changing only the **forecast point** never touches history — forecast entities carry no meaningful measurement history.

### How often does the data update?

- **Current conditions (station):** Every 10 minutes. The station file is polled, but unchanged files cost only a single 304 (Not Modified) response.
- **Daily forecast:** Every hour. The integration checks the forecast run stamp hourly and only downloads the daily files if the run changed.
- **Hourly forecast (if enabled):** Checked every hour on the integration's own forecast refresh, whether or not a card is open. A small canary read decides whether the new run changed anything; MeteoSwiss adjusts the coming hours about hourly, so expect roughly one refresh per hour. See the [Hourly Forecast](#hourly-forecast) option above.

See [ADR-0002](adr/0002-traffic-budget-bulk-local-forecast.md) for details on traffic optimization.

### What are QFE and QFF?

Both are atmospheric pressure measurements, differing in how they account for altitude:

- **QFE** (pressure at station level): The actual pressure at your station's elevation. Rarely used outside aviation.
- **QFF** (pressure reduced to sea level): The pressure adjusted as if measured at sea level. This is the standard for weather forecasts and is what the integration reports in the main `pressure` attribute.

The integration exposes both as separate sensor attributes if you need them; QFE is disabled by default.

### Is there an automation example?

Sure. To trigger a notification when it's about to rain:

```yaml
automation:
  - alias: "Rain warning"
    trigger:
      platform: numeric_state
      entity_id: sensor.postal_code_location_precipitation
      above: 0.1
    action:
      service: notify.notify
      data:
        message: "Rain detected at the station"
```

To turn on a light when it gets dark (using the condition attribute):

```yaml
automation:
  - alias: "Get dark, turn on the light"
    trigger:
      platform: state
      entity_id: weather.postal_code_location
      attribute: condition
      to: "cloudy"
    action:
      service: light.turn_on
      entity_id: light.my_light
```

Replace entity IDs with your actual integration entity IDs. Find them in **Settings → Devices & Services → MeteoSwiss Weather**.
