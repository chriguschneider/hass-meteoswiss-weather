# MeteoSwiss open data: what the integration reads

Measured facts about the upstream files, recorded so that nobody (human or
agent) has to rediscover them. Verified on 2026-08-26 unless noted. The
authoritative documentation is https://opendatadocs.meteoswiss.ch — when
this file and the docs disagree, the docs win and this file gets a PR.

## Access

- **Catalogue:** STAC API at `https://data.geo.admin.ch/api/stac/v1/`.
  One collection per dataset; `GET /collections/<id>/items` lists items,
  each with `assets` whose `href` is a plain HTTPS URL.
- **Files:** `https://data.geo.admin.ch/<collection>/...`, no API key, no
  registration, CORS irrelevant (we fetch server-side). Served from S3:
  `ETag`, `Last-Modified`, `Accept-Ranges: bytes`, `Cache-Control:
  max-age=10, public` on the station files.
- **Licence:** CC BY 4.0. Attribution `"Source: MeteoSwiss"` is required on
  every entity. Terms: https://opendatadocs.meteoswiss.ch/general/terms-of-use
- **Fair use:** swisstopo may throttle clients that "strain geo.admin.ch to
  a disproportionately wide extent". ADR-0002 exists because of this.
- **Changes** are announced at https://opendatadocs.meteoswiss.ch/changelog.
  Roadmap (https://opendatadocs.meteoswiss.ch/general/roadmap): an OGC
  Features API with per-point local-forecast queries is announced as a beta
  for the end of 2026. Until then, everything below is bulk files.

Encoding differs per dataset: station CSVs are **Windows-1252**, local
forecast CSVs are **Latin-1 (ISO-8859-1)**. Separator is `;` everywhere.

## A1 — SwissMetNet automatic weather stations (`ch.meteoschweiz.ogd-smn`)

Current conditions. ~160 stations with the full parameter set.

### Metadata (three CSVs, download once, cache)

- `https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/ogd-smn_meta_stations.csv`
  Header (verified):
  `station_abbr;station_name;station_canton;station_wigos_id;station_type_de;…;station_dataowner;station_data_since;station_height_masl;station_height_barometer_masl;station_coordinates_lv95_east;station_coordinates_lv95_north;station_coordinates_wgs84_lat;station_coordinates_wgs84_lon;station_exposition_*;station_url_*`
  Example row: `ABO;Adelboden;BE;0-20000-0-06735;…;1321.0;1326.0;2609372.0;1148939.0;46.491703;7.560703;…`
- `…/ogd-smn_meta_parameters.csv` — parameter code, description (de/fr/it/en),
  unit, decimals, interval.
- `…/ogd-smn_meta_datainventory.csv` — which station measures which
  parameter since when.

Nearest-station selection: haversine over `station_coordinates_wgs84_*`,
filtered to stations that actually carry the needed parameters
(datainventory). Not every station has every column (e.g. no pressure at
some precipitation-only sites; collection `ch.meteoschweiz.ogd-smn-precip`
is a separate, precipitation-only network).

### Per-station files (STAC item id = lowercase abbreviation, e.g. `ber`)

`https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/<abbr>/ogd-smn_<abbr>_<granularity>_<window>.csv`

| granularity | meaning | files |
|---|---|---|
| `t` | 10-minute values | `_t_now.csv` (recent, **the one to poll**), `_t_historical_<decade>.csv` |
| `h` | hourly | `_h_now.csv`, `_h_recent.csv`, `_h_historical_<decade>.csv` |
| `d` | daily | `_d_recent.csv`, `_d_historical.csv` |
| `m` | monthly | `_m.csv` |

Verified for BER: `ogd-smn_ber_t_now.csv` is **17.6 KB**, refreshed about
every 10 minutes (docs say the `now` file may lag up to ~20 min), header:

```
station_abbr;reference_timestamp;tre200s0;tre005s0;tresurs0;xchills0;ure200s0;tde200s0;pva200s0;prestas0;pp0qnhs0;pp0qffs0;ppz850s0;ppz700s0;fkl010z1;fve010z0;fkl010z0;dkl010z0;wcc006s0;fu3010z0;fkl010z3;fu3010z1;fu3010z3;rre150z0;htoauts0;gre000z0;ods000z0;oli000z0;olo000z0;osr000z0;sre000z0;tso005s0;tso010s0;tso020s0
BER;26.08.2026 00:00;18;16.8;16.9;18;93.4;16.9;19.3;951.9;1016.9;1015.1;;;1.3;0.9;0.9;20;;3.2;1.3;4.7;4.7;0;0;0;;375;;;0;20.7;21.2;21.3
```

- `reference_timestamp` is `dd.mm.yyyy HH:MM` in **UTC**.
- Empty field = not measured / missing. The latest row is the last line;
  take the last row whose needed fields are non-empty.
- Codes the integration cares about (10-minute values):

| code | meaning | unit |
|---|---|---|
| `tre200s0` | air temperature 2 m | °C |
| `ure200s0` | relative humidity 2 m | % |
| `tde200s0` | dew point 2 m | °C |
| `prestas0` | pressure at station level (QFE) | hPa |
| `pp0qffs0` | pressure reduced to sea level (QFF) | hPa |
| `pp0qnhs0` | pressure reduced to sea level (QNH) | hPa |
| `dkl010z0` | wind direction, 10-min mean | ° |
| `fu3010z0` | wind speed, 10-min mean | km/h |
| `fu3010z1` | gust peak (1 s) | km/h |
| `fkl010z0` / `fkl010z1` | the same in m/s | m/s |
| `rre150z0` | precipitation, 10-min total | mm |
| `sre000z0` | sunshine duration, 10-min total | min |
| `gre000z0` | global radiation, 10-min mean | W/m² |
| `tso005s0` … | soil temperature at 5/10/20 cm | °C |

The legacy all-station snapshot
`https://data.geo.admin.ch/ch.meteoschweiz.messwerte-aktuell/VQHA80.csv`
(used by the app-API integrations) still exists but is **not** used here:
per-station `_t_now.csv` files are the documented OGD path.

## E4 — Local forecast (`ch.meteoschweiz.ogd-local-forecasting`)

The forecast the MeteoSwiss app shows, for ~5,627 points, 9 days, refreshed
**every hour**. This is the dataset ADR-0002 is about.

### Points (metadata, download once, cache)

`https://data.geo.admin.ch/ch.meteoschweiz.ogd-local-forecasting/ogd-local-forecasting_meta_point.csv`

Header (verified):
`point_id;point_type_id;station_abbr;postal_code;point_name;point_type_de;point_type_fr;point_type_it;point_type_en;point_height_masl;point_coordinates_lv95_east;point_coordinates_lv95_north;point_coordinates_wgs84_lat;point_coordinates_wgs84_lon`

| `point_type_id` | what | `point_id` scheme |
|---|---|---|
| 1 | weather station | numeric station id, `station_abbr` filled |
| 2 | postal code centre | **`PLZ * 100 + n`**, e.g. `309800` = 3098 Köniz, `309801` = 3098 Schliern b. Köniz |
| 3 | mountain point of interest | numeric |

**Only `(point_id, point_type_id)` is unique.** A postal code can have
several points (`n` = 00, 01, …); default to `n = 00` and let the config
flow offer the others.

Parameters metadata:
`…/ogd-local-forecasting_meta_parameters.csv` (code, description, unit).
Read it rather than hard-coding descriptions.

### Data files

One CSV **per parameter per hourly run**, each containing **all points**:

```
https://data.geo.admin.ch/ch.meteoschweiz.ogd-local-forecasting/<YYYYMMDD>-ch/vnut12.lssw.<YYYYMMDDHHMM>.<param>.csv
```

e.g. `…/20260826-ch/vnut12.lssw.202608262000.tre200h0.csv`. The run
timestamp is UTC. The STAC items listing (`/collections/ch.meteoschweiz.ogd-local-forecasting/items`)
held 2,208 assets = 69 runs × 32 files on 2026-08-26, i.e. about three
days of runs; take the **newest run that has all the files you need**
(files of one run land over a few minutes, `Last-Modified` differs by
parameter).

Header: `point_id;point_type_id;Date;<param>` — `Date` is `YYYYMMDDHHMM`
UTC. Example rows for temperature, hourly:

```
point_id;point_type_id;Date;tre200h0
1;1;202608252100;11.5
309800;2;202608252100;19.0
```

### Measured sizes and layout (run `202608262000`, from a Raspberry Pi 5)

| file | size | rows |
|---|---|---|
| `tre200h0` temperature, hourly | 32.5 MB | 1,237,940 |
| `rre150h0` precipitation, hourly | 31.3 MB | |
| `jww003i0` weather symbol, hourly | 29.8 MB | |
| `fu3010h0` wind speed, hourly | 31.4 MB | |
| `fu3010h1` gust, hourly | 32.3 MB | |
| `dkl010h0` wind direction, hourly | 31.1 MB | |
| `sre000h0` sunshine, hourly | 29.4 MB | |
| `nprohihs` high clouds, hourly | 32.5 MB | |
| `tre200dx` / `tre200dn` daily max / min temperature (UTC day, **stations only**) | 0.21 MB | 8,352 = 928 type-1 points × 9 days; **no type 2/3 rows** |
| `rka150d0` daily precipitation (UTC day, **stations only**) | 0.20 MB | stations only, as above |
| `tre200px` / `tre200pn` daily max / min temperature (**local day, all points**) | 1.33 MB | 50,670 = 8,352 type 1 + 36,639 type 2 + 5,679 type 3 |
| `rka150p0` daily precipitation (**local day, all points**) | 1.29 MB | all point types, as above |
| `jp2000d0` daily weather symbol (all points) | 1.2 MB | 50,670 = types 1 + 2 + 3 |

**The integration fetches the `p`-variants, not the `d`/`0`-variants.**
Measured on run `202608271100` (2026-08-27): `tre200dx`/`tre200dn`/`rka150d0`
contain **only** station points (`point_type_id=1`). The config-flow default is
a postal-code centre (`point_type_id=2`), which has **no rows at all** in those
files, so a daily forecast built from them silently has `temp_max=temp_min=
precipitation=None` (issue #34). The `p`-variants are aggregated over the
**local calendar day** (00:00–24:00 local, the boundary the MeteoSwiss app uses)
and are the only daily files that carry non-station points. On the 8,352 station
rows present in both files of a pair, `tre200dx` and `tre200px` agree on 97.3 %
of values — the same quantity on a better day boundary. Live `309800;2` values
on 2026-08-27: `tre200px=29.1`, `tre200pn=16.7`, `rka150p0=0.1`, `jp2000d0=2`.

- Download of a 32 MB file: 0.3 s on a fibre connection; naive Python
  split-parse: 1.8 s on a Pi 5. Parse in the executor.
- **Rows are sorted by `Date`, then point** — the 220 rows of point
  `309800` start at byte ~70 KB and end at byte ~32.4 MB. There is no way
  to download only one point: no early exit, and `Range` requests (which
  the server does honour, HTTP 206) cannot help.
- 220 hourly timestamps per point = 9 days + a few hours.
- Daily files are cheap; hourly files are the whole budget. See ADR-0002.

### Parameter codes (from the docs; confirm against the meta CSV)

| hourly | meaning |
|---|---|
| `tre200h0` | temperature 2 m; `treq10h0` / `treq90h0` = 10 % / 90 % percentile |
| `rre150h0` | precipitation sum; `rreq10h0` / `rreq90h0` percentiles |
| `rre003i0`, `rp0003i0` | precipitation over a 3-hour interval and its probability |
| `jww003i0` | **weather symbol** (MeteoSwiss icon code, day/night variants) |
| `fu3010h0` | wind speed km/h; `fu3010h1` gust; `fu3q10h0` … percentiles |
| `dkl010h0` | wind direction ° |
| `sre000h0` | sunshine duration |
| `gre000h0`, `ods000h0` | global / diffuse radiation |
| `nprohihs`, `npromths`, `nprolohs` | high / mid / low cloud cover |
| `zprfr0hs` | zero-degree level |

| daily | meaning |
|---|---|
| `tre200px` / `tre200pn` | max / min temperature, **local calendar day (00:00–24:00 local), all point types** — the ones the integration fetches |
| `rka150p0` | precipitation total, **local calendar day, all point types** — the one the integration fetches |
| `tre200dx` / `tre200dn` | max / min temperature, **UTC day, stations only** — do **not** use for postal-code points (issue #34) |
| `rka150d0` | precipitation total, **UTC day (0 UTC – 0 UTC), stations only** — do **not** use for postal-code points (issue #34) |
| `jp2000d0` | **daily weather symbol** (all point types) |

**Trap (issue #34):** the `p`-suffix on `tre200px`/`tre200pn` and the `p0` on
`rka150p0` are *not* percentiles/probabilities — the official
`ogd-local-forecasting_meta_parameters.csv` descriptions are "daily maximum /
minimum / total 00:00 – 24:00 local time". They are the same quantity as the
`d`/`0`-variants on the local-day boundary, and — unlike the `d`/`0` files —
they cover every point type. The `d`/`0` daily files are **station-only**, so
using them for the default postal-code point yields no temperatures at all.

The symbol codes must be mapped to Home Assistant conditions
(`sunny`, `partlycloudy`, `rainy`, `snowy`, `lightning-rainy`, …). The
mapping used by `Rudd-O/hamsclientfork` (MIT) is a usable reference for
the code list; verify against the app's icon set before trusting it.

## Adding the OGC Features backend

MeteoSwiss has announced a per-point OGC Features API (beta, end of 2026).
When it ships, swapping it in is a contained change — the coordinator and
the entities never need to know. The seam is `ForecastBackend` in
`ogd/backend.py` and the factory hook `_backend_factory` in `__init__.py`.

Steps to add a new backend:

1. **New module** `ogd/ogc_features.py` — implement a class with the same two
   async methods as `BulkCsvBackend`:
   ```python
   async def fetch_daily(self, point: ForecastPoint) -> list[DailyForecast]: ...
   async def fetch_hourly(self, point: ForecastPoint) -> list[HourlyForecast]: ...
   ```
   The class must satisfy the `ForecastBackend` protocol (`ogd/backend.py`).
   Keep it in the `ogd/` package (pure Python, ADR-0001). The coordinator
   passes a `ForecastPoint` — `(point_id, point_type_id)` identify the
   forecast row in the upstream API.

2. **Wire it up** in `__init__.py` by changing `_backend_factory`. A feature
   flag in the config entry options is the natural gate while both backends
   coexist.

3. **Test it** with the same `FakeBackend` pattern from
   `tests/test_backend_seam.py`: the coordinator and entities need no changes
   and their existing tests continue to pass.

The `ForecastCoordinator._async_update_data` will still call `latest_run`
to check the STAC run stamp (cheap, a single small JSON request) before
deciding whether to call `fetch_daily`. A per-point API backend can skip
the run-staleness check by always fetching; alternatively, the coordinator
can be taught to skip the STAC call when the configured backend does not
need it — that is an ADR-worthy change.

## What is NOT in the open data

- **Weather warnings.** No dataset, not on the 2026 roadmap. The README
  points to Home Assistant's core `meteoalarm` integration (regional CAP
  feed). Do not add the app API for this (ADR-0001).
- **Pollen** is there (`ch.meteoschweiz.ogd-pollen`) but out of scope for
  now; `frimtec/hass-swiss-pollen` covers it.
- **Radar / nowcast** (`ch.meteoschweiz.ogd-radar-precip`, INCA) is the
  radar integration's territory (ADR-0003).

## Other collections, for reference

| collection | content | format |
|---|---|---|
| `ch.meteoschweiz.ogd-smn-precip` | precipitation-only stations | CSV |
| `ch.meteoschweiz.ogd-radar-precip` | radar composites, 5 min | ODIM HDF5 |
| `ch.meteoschweiz.ogd-forecasting-icon-ch1` / `-ch2` | ICON-CH1/CH2-EPS model output | GRIB2 |
| `ch.meteoschweiz.ogd-pollen` | pollen measurements | CSV |
