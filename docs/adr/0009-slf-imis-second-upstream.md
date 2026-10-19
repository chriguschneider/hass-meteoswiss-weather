# ADR-0009: SLF IMIS snow stations as a second official upstream

- **Status:** Accepted
- **Date:** 2026-10-19

## Context

Users want the snow depth at ski altitude next to the MeteoSwiss values.
The MeteoSwiss open data does not carry it: SwissMetNet has few stations
above 2000 m and no snow-height series in the station files used here.

The WSL Institute for Snow and Avalanche Research SLF runs IMIS, an
automatic network built for avalanche warning: 208 stations (probed
2026-10-19: 128 `SNOW_FLAT`, 67 `WIND`, 7 `FLOWCAPT`, 6 `SNOW_SLOPE`),
most between 2000 and 3000 m, measuring every 30 minutes. Snow-height
stations stand on a flat study plot off-piste, so the value is not the
lifts' "piste snow depth", but it is measured at the same altitude.

The SLF publishes the live data through a documented API,
`https://measurement-api.slf.ch` (OpenAPI, v1.1.0, no key, JSON), under
**CC BY 4.0**. It is not on `data.geo.admin.ch` (STAC catalogue searched
2026-10-19), so ADR-0001 rules it out as written. ADR-0001 was aimed at
private, undocumented app backends; this is an official federal open-data
service with the same licence, but without a public changelog.

Measured 2026-10-19:

- `/public/api/imis/stations`: ~31 KB, code, label, lat/lon, elevation,
  type.
- `/public/api/imis/station/{code}/measurements` (`period_in_days` 1, 3
  or 7): 48 rows per day, ~16 KB; `HS` snow height in cm plus 30-minute
  means of air temperature, humidity, snow surface temperature and wind.
  Sends `ETag`, answers `304` to `If-None-Match`.
- `/public/api/imis/daily-snow`: one row per snow station, `HS` and
  `HN_1D` (new snow 24 h, cm) **modelled by SNOWPACK**, ~10 KB for the
  whole network.
- `/public/api/imis/measurements` (all stations): ~3.2 MB.

## Decision

- ADR-0001 is amended: besides `data.geo.admin.ch`, the integration may
  request **`https://measurement-api.slf.ch`**, and only the three
  endpoints `/public/api/imis/stations`,
  `/public/api/imis/station/{code}/measurements` and
  `/public/api/imis/daily-snow`. Any other SLF endpoint or host needs a
  new ADR. The bulk `/public/api/imis/measurements` is never requested.
- The client lives in **`custom_components/meteoswiss_weather/slf/`**,
  pure Python (aiohttp/stdlib, no Home Assistant imports), separate from
  `ogd/`; the base URL is a constant in `const.py` (`SLF_API_BASE`).
- A config entry may carry an **optional IMIS station**
  (`CONF_IMIS_STATION_CODE`, `CONF_IMIS_STATION_NAME` in `entry.data`,
  like the precipitation station of ADR-0006). Setup and reconfigure offer the nearest
  `SNOW_FLAT` stations with elevation and distance; **none is selected by
  default**. Without it, no SLF request is made.
- Entities: **snow height** (measured `HS`) and **new snow 24 h**
  (modelled `HN_1D`, labelled as modelled). Entities fed by the SLF set
  `attribution = "Source: WSL Institute for Snow and Avalanche Research
  SLF"` instead of the MeteoSwiss one and expose a `station` attribute
  naming the IMIS station; they sit on the entry's device.
- Cadence, in a coordinator of its own: station measurements every **30
  minutes**, conditional, `period_in_days=1`, keeping only the newest
  row; daily snow **once a day**, keeping only the configured station's
  row; the station list on setup/reconfigure only.

## Consequences

- Traffic when set: at most ~16 KB per 30 minutes plus ~10 KB per day,
  well below the station budget of ADR-0002, which is unchanged for the
  MeteoSwiss files.
- A second upstream without a changelog: the weekly smoke test
  (ADR-0004) gains probes for the three SLF endpoints and their fields, so
  a schema change shows up there first. A failing SLF fetch leaves the SLF
  entities unavailable and never affects the MeteoSwiss entities.
- Snow height is near zero in summer and stations may report `null`;
  sensors show unknown rather than inventing a value.
- `ogd/` stays MeteoSwiss-only and keeps its path to PyPI (#20); `slf/`
  can follow the same way later.
- Measured facts and fixtures: `docs/slf.md`. Implementation: #171.
