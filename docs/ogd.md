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

### History files (measured 2026-08-28, station BER) — for the backfill (#51)

The STAC item of a station lists, next to `_t_now.csv`:

| file | content | size (BER) | updated |
|---|---|---|---|
| `ogd-smn_ber_h_now.csv` | hourly values, today | 1.5 KB | hourly |
| `ogd-smn_ber_h_recent.csv` | hourly values, 1 January of this year → yesterday 23:00 | 829 KB (5,736 rows) | daily, ~02:15 UTC |
| `ogd-smn_ber_h_historical_2020-2029.csv` | hourly values 2020-01-01 → 2025-12-31 | 7.6 MB | yearly (February) |
| `…_h_historical_2010-2019.csv` … `_1980-1989.csv` | one file per decade | 12.5 MB (2010s) | yearly |
| `ogd-smn_ber_d_recent.csv` / `_d_historical.csv` | daily values | 40 KB | daily |
| `ogd-smn_ber_t_recent.csv` / `_t_historical_<decade>.csv` | 10-minute values | — | daily / yearly |

Hourly header (verified):
`station_abbr;reference_timestamp;tre200h0;tre200hn;tre200hx;tre005h0;tre005hn;ure200h0;pva200h0;tde200h0;prestah0;pp0qffh0;pp0qnhh0;ppz700h0;ppz850h0;fkl010h1;dkl010h0;fkl010h0;fu3010h0;fu3010h1;fkl010h3;fu3010h3;wcc006h0;fve010h0;rre150h0;htoauths;gre000h0;oli000h0;olo000h0;osr000h0;ods000h0;sre000h0;…`
— the hourly **mean, min and max** of temperature (`tre200h0`/`hn`/`hx`)
are exactly what Home Assistant's long-term statistics store per hour.
`reference_timestamp` is `dd.mm.yyyy HH:MM` UTC, encoding Windows-1252,
`;`-separated, not gzip-encoded. A full backfill of one station is
`_h_recent` plus the decade files (8–13 MB each; 1980 onwards ≈ 45 MB,
one-off); `_h_recent` alone covers the current year.

## A2 — Precipitation stations (`ch.meteoschweiz.ogd-smn-precip`) — measured 2026-08-28

The rain-only network next to SwissMetNet: **141 automatic precipitation
stations** (`station_type_en` "Automatic precipitation stations"), meta
CSVs with the same columns as A1 (`…/ogd-smn-precip_meta_stations.csv`,
134 KB, with WGS84 coordinates and height; `_meta_parameters.csv`;
`_meta_datainventory.csv`). Parameters: `rre150z0` (10-minute sum),
`rre150h0`, `rre150d0` (6–6 UTC), `rka150d0` (0–0 UTC), `rre150m0`,
`rre150y0`.

Per-station files follow the A1 pattern:
`…/ogd-smn-precip/<abbr>/ogd-smn-precip_<abbr>_t_now.csv` — verified ABE:
1.2 KB, header `station_abbr;reference_timestamp;rre150z0`, 10-minute rows
since 00:00 UTC, the 07:30 row present at 07:48 (~15 min lag) — plus
`_t_recent`, `_t_historical_<decade>`, `_h_*`, `_d_*`, `_m`, `_y`.
Encoding Windows-1252.

Density: the nearest precipitation stations to Köniz (3098) are Belp
7.2 km, Laupen 13.3 km, Kiesen 16.7 km, next to the full SwissMetNet
station BER. This is the dataset behind the optional second
"precipitation station" (ADR-0006, #56).

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
- 220 hourly timestamps per point = 9 days + a few hours.
- Daily files are cheap; hourly files are the whole budget. See ADR-0002.

#### Row order — two layouts (measured 2026-08-28 run `202608280400`; cloud files re-measured 2026-09-01 run `202609010800`)

An earlier note here claimed rows are always sorted by `Date`, so a `Range`
request "cannot help". That is true for only a minority of the 16 hourly
files. Sampling rows at fixed byte offsets across each file shows two layouts:

| layout | sort key | files |
|---|---|---|
| **date-major** (all points per hour block, ~150 KB/h) | `Date`, then point | `tre200h0`, `treq10h0`, `treq90h0` |
| **point-major** (one point's ~220 rows contiguous, ~5 KB) | `(point_type_id, point_id, Date)` — ends with type-3 rows | `rre150h0`, `rre003i0`, `rp0003i0`, `fu3010h0`, `fu3010h1`, `dkl010h0`, `zprfr0hs`, `gre000h0`, `sre000h0` |
| **point-major, id-sorted** (types mixed) | `(point_id, Date)` | `jww003i0` (`834;3` at 3 MB, `5025;1` at 6 MB); `nprohihs`, `npromths`, `nprolohs` since 2026-08-31 (see below) |

**Cloud-layer unit change (issue #97):** `nprohihs`, `npromths`, `nprolohs`
changed from **percent (0–100)** to **fraction (0–1, two decimal places)**
between the runs of 2026-08-27 and 2026-08-31. The change was not announced;
`ogd-local-forecasting_meta_parameters.csv` declares the unit as `-`. The
integration applies a per-file heuristic: if `max(values for point) ≤ 1.0`
the file is fraction-encoded and is multiplied by 100 before storage, so Home
Assistant always receives percent values. A file with any value > 1.0 is
already in percent and is left unchanged.

**Cloud-layer row-order change (issues #97/#100):** the same silent 2026-08-31
change also re-sorted the three cloud files from date-major to point-major
(id-sorted, `jww003i0` style). The runtime layout detection absorbed it, but
these files have now demonstrably flipped layout once, so the weekly smoke
test accepts **either** layout for them (failing only on "other") and prints
the observed layout instead of pinning one.

- The origin is **CloudFront over S3**. `Range` is answered with 206 and
  `Accept-Ranges: bytes`; `If-None-Match` **plus** `Range` answers 304 when
  unchanged; `If-Range` with a matching ETag answers 206, a mismatch 200 with
  the full body. **No gzip** on the 30 MB files (small files like `meta_point`
  do arrive gzip-encoded). `Cache-Control: max-age=7200`. S3 does not serve
  multi-range requests.
- Date-major files of one UTC day all start at **21:00 UTC of the previous
  day** (they carry past hours), so byte offsets are stable across a day's
  runs.

**How the integration uses this (issue #50, `ogd/hourly.py`):** it classifies
each file's layout at runtime from offset probes, then

- **point-major →** binary-searches the point's contiguous block by byte
  offset and fetches it with one `Range` GET (~5 KB); the block offset is
  cached per file and re-verified with a single probe on the next run;
- **date-major →** fetches a `Range: bytes=0-<budget>` prefix sized from the
  chosen horizon (`hourly_horizon_days`), extending it if a probe shows the
  horizon was not reached;
- **anything unrecognised →** downloads the whole file and logs a warning.

For the minimum set at the default horizon this is **~7–11 MB per refresh**
instead of ~125 MB. See ADR-0002 (revised) for the budget and the option.

#### Change rhythm across runs (measured 2026-08-27, all 24 runs, `tre200h0`)

A new file is published every hour, but the content of one point moves in
rhythms tied to the model cycles behind it. Consecutive runs compared for
the postal-code point `309800` and the station point `1` (same picture):

| forecast range | changes at (UTC run hour) | rhythm |
|---|---|---|
| today + tomorrow (d0–d1) | 02, 05, 08, 11, 14, 17, 20, 23 (small d0-only touches at 06, 15, 18, 19) | every 3 h — ICON-CH1 cycles (00/03/06/…) landing ~2 h later |
| days 2–5 | 04, 10, 16, 22, and again 05, 11, 17, 23 | every 6 h — ICON-CH2 cycles (00/06/12/18) landing ~4 h later, refined by the next CH1 run |
| days 6–9 | 05, 08, 11, 17, 20, 23 | with the runs above |
| past hours (the file starts at 21:00 UTC of the previous day) | never | — |

The runs at 01, 03, 07, 09, 12 and 13 UTC changed **nothing** for either
point. Fetching the near term at 02/05/08/…/23 UTC and the far range at
05/11/17/23 UTC catches every observed change with 8 near and 4 far
fetches per day; fetching every run wastes 6 of 24 downloads outright.
This is the basis of the tiered refresh (ADR-0002, revision 2; #54).

### Parameter codes (from the docs; confirm against the meta CSV)

| hourly | meaning |
|---|---|
| `tre200h0` | temperature 2 m; `treq10h0` / `treq90h0` = 10 % / 90 % percentile |
| `rre150h0` | precipitation sum; `rreq10h0` / `rreq90h0` percentiles |
| `rre003i0`, `rp0003i0` | precipitation over a 3-hour interval and its probability. **Measured 2026-08-28:** `rp0003i0` has a row for **every hour** (217 rows per point, integer %, e.g. `0,0,0,2,6,18,41,57`), so it is a rolling 3-hour window; the meta description says only "during 3 hours", the sibling `jww003i0` says "preceding 3 hours" — treat the value as the window **ending** at `Date` unless the docs say otherwise |
| `jww003i0` | **weather symbol** (MeteoSwiss icon code, day/night variants) |
| `fu3010h0` | wind speed km/h; `fu3010h1` gust; `fu3q10h0` … percentiles |
| `dkl010h0` | wind direction ° |
| `sre000h0` | sunshine duration |
| `gre000h0`, `ods000h0` | global / diffuse radiation |
| `nprohihs`, `npromths`, `nprolohs` | high / mid / low cloud cover (%; fraction 0–1 since 2026-08-31, scaled ×100 by the integration — issue #97) |
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

## Pollen (`ch.meteoschweiz.ogd-pollen`) — measured 2026-08-28

The automatic pollen network: **15 stations**, abbreviations `P…` (PBE
Bern, PBS Basel, PBU Buchs SG, PCF La Chaux-de-Fonds, PDS Davos, PGE
Genève, PLO Locarno, PLS Lausanne, PLU Lugano, PLZ Luzern, PMU
Münsterlingen, PNE Neuchâtel, PPY Payerne, PSN Sion, PZH Zürich). Meta CSVs
in the A1 shape: `ogd-pollen_meta_stations.csv` (12.5 KB, WGS84
coordinates and height), `_meta_parameters.csv` (9 KB, names in
de/fr/it/en), `_meta_datainventory.csv` (20 KB).

**28 parameters = 7 taxa × 4 granularities.** Taxon prefixes: `kaalnu`
alder, `kabetu` birch, `kacory` hazel, `kafagu` beech, `kafrax` ash,
`kaquer` oak, `khpoac` grasses. Suffixes: `h0` hourly mean, `d0` daily
mean 6–6 UTC, `d1` daily mean 0–0 UTC, `y0` annual integral. Unit `No/m³`
(grains per cubic metre), integer.

Per-station files (STAC item id = lowercase abbreviation):
`…/ogd-pollen/<abbr>/ogd-pollen_<abbr>_h_now.csv` — verified PBE: 391
bytes, header
`station_abbr;reference_timestamp;kabetuh0;khpoach0;kaalnuh0;kacoryh0;kafaguh0;kafraxh0;kaquerh0`,
rows for today 00:00 → 07:00 UTC present at 07:48, so **hourly with about
one hour of lag** — plus `_h_recent.csv` (215 KB), `_h_historical_2020-2029.csv`,
`_d_recent.csv` (13 KB; the 6–6 UTC `d0` values fill in a day late, the
0–0 UTC `d1` values for yesterday are present), `_d_historical.csv`,
`_y.csv`. Encoding Windows-1252; timestamps `dd.mm.yyyy HH:MM` UTC. Daily
data since 1990, hourly since 2023 (datainventory).

Dataset behind the pollen platform (ADR-0005, #53).

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
- **Pollen** is in the open data (`ch.meteoschweiz.ogd-pollen`, section
  above) and in scope since 2026-08-28 (ADR-0005, #53);
  `frimtec/hass-swiss-pollen` is the standalone alternative.
- **Radar / nowcast** (`ch.meteoschweiz.ogd-radar-precip`, INCA) is the
  radar integration's territory (ADR-0003).

## Other collections, for reference

| collection | content | format |
|---|---|---|
| `ch.meteoschweiz.ogd-smn-precip` | precipitation-only stations — see A2 | CSV |
| `ch.meteoschweiz.ogd-radar-precip` | radar composites, 5 min | ODIM HDF5 |
| `ch.meteoschweiz.ogd-forecasting-icon-ch1` / `-ch2` | ICON-CH1/CH2-EPS model output | GRIB2 |
| `ch.meteoschweiz.ogd-pollen` | pollen measurements — see the Pollen section | CSV |
