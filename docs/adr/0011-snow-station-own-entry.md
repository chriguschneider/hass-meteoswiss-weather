# ADR-0011: A snow station is its own config entry

- **Status:** Accepted
- **Date:** 2026-10-19
- **Amends:** ADR-0009 (where the IMIS station is chosen and where its
  sensors live)

## Context

ADR-0009 added the IMIS snow station as an optional third pick in the
station step of a postal-code or mountain entry, with its sensors on that
entry's device. The owner then asked for every IMIS value as a sensor
(temperature, wind, humidity, snow temperatures, …). Hung off a postal-code
entry, those values sit next to the SwissMetNet values of a different place
and raise a question nobody can answer well: which station's temperature is
"the" temperature of the entry?

The owner's view (2026-10-19): an IMIS station is a weather station of its
own, at its own place, just as a postal code and a mountain point are
different places. It therefore gets its own entry and its own device.

## Decision

- The first step of the setup flow offers a third choice next to postal
  code and mountain point: **snow station**. It lists the nearest
  `SNOW_FLAT` IMIS stations to the Home Assistant location, labelled with
  elevation and distance and, when it has no current data, "currently no
  data" (ADR-0009 amendment). One entry per station; unique id
  `imis-<code>`.
- Entry data of a snow-station entry: `entry_type: "snow_station"`,
  `imis_station_code`, `imis_station_name`, `imis_station_elevation`. An
  entry without `entry_type` is a point entry (every existing entry), so
  no migration is needed.
- A snow-station entry sets up **only** the IMIS coordinator and the sensor
  platform: no weather entity, no MeteoSwiss request of any kind. Its
  device is the station (name = the station label, model = `"IMIS snow
  station, <elevation> m"`, manufacturer SLF). It carries a static
  `elevation` sensor (device class DISTANCE, unit m, unique id
  `imis-<code>_elevation`, always available, SLF attribution) plus every
  IMIS value as a sensor (ADR-0009 amendment).
- Point entries no longer offer or read an IMIS station: the snow field
  leaves the station step of setup and reconfigure, and keys left in
  `entry.data` by v0.5.0–v0.6.2 are ignored.
- No automatic migration. Snow-station picks existed only on the owner's
  instance (v0.5.0 was released the same day); they are converted by hand.

## Consequences

- Traffic is unchanged per station; an IMIS-only user makes no MeteoSwiss
  request at all.
- Entity ids of the snow sensors change on the owner's instance
  (`sensor.<point>_snow_height` → `sensor.<station>_snow_height`); the
  hand conversion checks dashboards and automations first.
- Everything else in ADR-0009 (allowed host and endpoints, cadence, outage
  handling, attribution) stays.
- Implementation: see the issues referencing this ADR.
