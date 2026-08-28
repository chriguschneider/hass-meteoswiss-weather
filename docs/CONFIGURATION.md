# Configuration and Usage

## Setup

The integration uses a simple three-step setup flow, with no YAML required. Go to **Settings → Devices & Services → Add Integration** and search for *MeteoSwiss Weather*.

### Step 1: Postal Code

Enter your Swiss postal code (4 digits). The setup flow pre-fills this with the postal code of your Home Assistant location, but you can override it.

### Step 2: Forecast Point (if needed)

If your postal code has multiple forecast points, you'll be asked to choose one. This is typical for larger cities where the weather can differ by neighbourhood. The default is the first point; most users never see this screen.

### Step 3: Weather Station

Choose a SwissMetNet weather station to provide current conditions. The setup flow shows the three nearest stations with the closest one pre-selected. You can override it if you prefer a different station (e.g. one with better elevation or terrain match).

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

**Forecast:**

- **Daily forecast**: 9 days, always available. Temperature high/low, precipitation, and weather condition for each day.
- **Hourly forecast**: When enabled in options (see below). Hourly temperature, precipitation, wind, and condition. Updated at most every 3 hours.

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
| **Precipitation** | mm | Yes | 10-minute total |
| **Dew Point** | °C | No | Diagnostic |
| **Pressure (QFE)** | hPa | No | Station-level pressure |
| **Sunshine Duration** | min | No | 10-minute total |
| **Global Radiation** | W/m² | No | Solar radiation |

## Options

### Hourly Forecast

**Default:** Off

**Cost:** Roughly **1 GB per day per Home Assistant instance** when enabled. The integration fetches hourly forecast files (30–33 MB each) every 3 hours at most. This is a real cost that sums across the HACS install base, so please enable it only if you actually need an hourly view.

**How it works:** Even when on, the integration checks the forecast run hourly but only downloads the bulk hourly files if:
- Three or more hours have passed since the last successful hourly download, AND
- A new forecast run is available.

This throttling keeps the traffic reasonable while offering a sharp hourly view to those who need it.

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

The hourly forecast is published as whole-of-Switzerland CSV files (30–33 MB per parameter per hour). Even at the throttled rate of once per 3 hours, this costs roughly 1 GB per day per Home Assistant instance. With hundreds of HACS installations, that traffic reaches the scale where swisstopo's fair-use policy applies. MeteoSwiss has announced a per-point API for the end of 2026, which will remove this limitation. Until then, hourly is an informed opt-in.

See [ADR-0002](adr/0002-traffic-budget-bulk-local-forecast.md) for the full context and measured file sizes.

### Why is the domain `meteoswiss_weather` and not `meteoswiss`?

The domain follows Home Assistant's [naming convention for weather integrations](https://developers.home-assistant.io/docs/creating_integration_manifest#manifest-reference): `<source>_weather`. This leaves room for future integrations that might expose other MeteoSwiss datasets (e.g., radar, pollen).

### How does the integration choose the nearest weather station?

The integration uses the haversine formula to calculate distances from your forecast point to all available SwissMetNet stations. It then selects the three nearest and displays them in the setup flow, with the closest pre-selected.

The forecast point is determined by your postal code. If your postal code has multiple forecast points, you choose one during setup; otherwise the setup flow skips to station selection.

**Note:** Not every station measures every parameter. If your chosen station does not measure a parameter (e.g., some precipitation-only sites lack pressure), that sensor will show as unknown. To pick a station with more complete measurements, use **Reconfigure** on the integration entry and choose a different station (see [Can I change the station after setup?](#can-i-change-the-station-after-setup) below).

### What does the weather condition mean?

The condition comes from the MeteoSwiss weather symbol code (`jp2000d0` from the daily forecast, or `jww003i0` from hourly data when available). The integration maps these codes to Home Assistant's standard conditions: `sunny`, `partlycloudy`, `cloudy`, `rainy`, `snowy`, `lightning-rainy`, etc.

The daily forecast uses the daytime symbol variant for consistency. The hourly condition, when available, uses the actual time-of-day symbol from MeteoSwiss (which already accounts for day/night).

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
- **Hourly forecast (if enabled):** Every 3 hours at most, even if new runs arrive more frequently.

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
