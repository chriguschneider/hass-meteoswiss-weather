# SLF IMIS: measured facts

Measured 2026-10-19 against the live API. The decision to use it is
ADR-0009; this file is the contract the `slf/` client is written against.
Re-probe before changing a fetch: the API has no public changelog.

## Host and licence

- Base: `https://measurement-api.slf.ch` (FastAPI, OpenAPI at
  `/openapi.json`, docs at `/`). Version 1.1.0. No key.
- Licence CC BY 4.0, terms at
  <https://www.slf.ch/en/services-and-products/slf-data-service>.
  Attribution string used here:
  `Source: WSL Institute for Snow and Avalanche Research SLF`.
- Responses carry a weak `ETag` and `Cache-Control: max-age` of a few
  minutes; `If-None-Match` with the last `ETag` answers `304`.

## Endpoints used (ADR-0009)

### `GET /public/api/imis/stations`

JSON array, 208 entries, ~31 KB:

```json
{"code": "STH2", "label": "Vorderstocken", "lon": 7.52, "lat": 46.68,
 "elevation": 1791.0, "country_code": "CH", "canton_code": "BE",
 "type": "SNOW_FLAT"}
```

`type` counts: `SNOW_FLAT` 128, `WIND` 67, `FLOWCAPT` 7, `SNOW_SLOPE` 6.
Only `SNOW_FLAT` stations measure a snow height on a flat study plot;
`WIND` stations return `HS: null` (see `slf-imis_jau1_measurements.json`).
The pick offers `SNOW_FLAT` only.

### `GET /public/api/imis/station/{code}/measurements?period_in_days=1`

`period_in_days` is one of 1, 3, 7 (default 1). 48 rows per day, sorted
ascending by `measure_date` (ISO 8601 with `Z`, UTC, on the half hour),
~16 KB. An unknown code answers HTTP 400.

| Field | Meaning | Unit |
|---|---|---|
| `HS` | snow height, instantaneous | cm |
| `TA_30MIN_MEAN` | air temperature | °C |
| `RH_30MIN_MEAN` | relative humidity | % |
| `TSS_30MIN_MEAN` | snow surface temperature | °C |
| `TS0/25/50/100_30MIN_MEAN` | snow temperature at 0/25/50/100 cm | °C |
| `RSWR_30MIN_MEAN` | reflected short-wave radiation | W/m² |
| `VW_30MIN_MEAN` | wind speed, vectorial mean | **m/s** |
| `VW_30MIN_MAX` | max 5 s gust | **m/s** |
| `DW_30MIN_MEAN` | wind direction | ° |

Every value field may be `null`. The newest row is last.

### `GET /public/api/imis/daily-snow`

One row per snow station, ~10 KB for the whole network:

```json
{"station_code": "STH2", "measure_date": "2026-10-18T00:00:00",
 "HS": 0.0, "HN_1D": 0.0}
```

- Values are **modelled by SNOWPACK**, not measured: `HS` snow height and
  `HN_1D` new snow over the last 24 h, both cm.
- `measure_date` has **no time-zone suffix** here, unlike the measurements
  endpoint; the OpenAPI schema says UTC.
- 129 rows: 122 `SNOW_FLAT`, 6 `SNOW_SLOPE`, 1 `FLOWCAPT`. Six `SNOW_FLAT`
  stations have no row (e.g. `SCH2`), and some rows are all `null`
  (`BOG4`). A missing or null row means "unknown", not zero.

## Not used

- `/public/api/imis/measurements`: all stations, 24 h, ~3.2 MB.
- `/public/api/imis/*measurements-precipitation`, `/public/api/study-plot/*`.

## Fixtures

Trimmed real responses in `tests/fixtures/`:

- `slf-imis_stations.json`: STH2, JAU2, SHE2, CSL2 (the four nearest
  `SNOW_FLAT` to Köniz, 28–36 km), JAU1 (`WIND`), DAV2, SCH2, BOG4.
- `slf-imis_sth2_measurements.json`: last 6 rows of STH2.
- `slf-imis_jau1_measurements.json`: last 2 rows of a `WIND` station.
- `slf-imis_daily-snow.json`: the rows of the stations above (SCH2 absent,
  BOG4 all null).
