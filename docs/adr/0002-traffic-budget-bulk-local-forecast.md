# ADR-0002: Traffic budget for the bulk local-forecast files

- **Status:** Accepted
- **Date:** 2026-08-26
- **Revised:** 2026-08-28 (issue #50) — see [Revision](#revision-2026-08-28-issue-50)
- **Revised again:** 2026-08-28 (issue #54) — see [Revision 2](#revision-2-2026-08-28-issue-54)
- **Revised again:** 2026-08-28 (issue #60) — see [Revision 3](#revision-3-2026-08-28-issue-60)
- **Revised again:** 2026-08-28 (issue #55) — see [Revision 4](#revision-4-2026-08-28-issue-55)

## Context

The local forecast (`ch.meteoschweiz.ogd-local-forecasting`) is published
as one CSV per parameter per hourly run, each holding **all ~5,600
forecast points** in Switzerland. Measured on 2026-08-26 (`docs/ogd.md`):

- hourly parameters (temperature, precipitation, symbol, wind, gust,
  direction, sunshine, clouds): **29–33 MB each**, ~1.24 million rows;
- daily parameters (min/max temperature, daily precipitation): the
  station-only UTC-day `d`/`0`-variants are 0.2 MB, but the integration must
  use the all-point local-day `p`-variants (`tre200px`/`tre200pn`/`rka150p0`)
  at ~1.3 MB each — the `d`/`0` files carry no postal-code points at all
  (issue #34); the daily symbol `jp2000d0` is 1.2 MB;
- rows are sorted by **time, not by point** — the ~220 rows of one point
  are spread across the whole file, so neither a streaming early exit nor
  an HTTP Range request can shorten the download (**corrected 2026-08-28:
  this holds for only 6 of the 16 hourly files; see the Revision below**);
- `ETag` / `Last-Modified` are served, but the files change every hour.

A weather entity with an hourly forecast needs at least four hourly
parameters: ~125 MB per refresh. Refreshed hourly that is 3 GB per day per
Home Assistant instance; across a few hundred HACS installs it reaches the
scale at which swisstopo's fair-use clause applies. MeteoSwiss has
announced an OGC Features API with per-point queries for the end of 2026,
which removes the problem — but not before.

## Decision

- **Daily forecast is the default** and is the only thing fetched unless
  the user opts in. It uses daily parameter files only (order of 5 MB per
  refresh: three all-point `p`-variant files at ~1.3 MB plus the 1.2 MB
  symbol).
- **Hourly forecast is an option, off by default.** When on, the client
  fetches at most the documented minimum parameter set (`tre200h0`,
  `rre150h0`, `jww003i0`, `fu3010h0`; gusts `fu3010h1` and direction
  `dkl010h0` only if an entity exposes them) and **never more often than
  every 3 hours**. The interval constant lives in `const.py` and is
  asserted by a test.
- **Every download is conditional** (`If-None-Match` / `If-Modified-Since`)
  and the run timestamp from the STAC item is compared first, so an
  unchanged run costs one small request.
- **CSV parsing runs in the executor**, never on the event loop; the
  parser keeps only the rows of the configured point.
- The client exposes the forecast through an interface (`fetch_daily`,
  `fetch_hourly` on the point) that a future backend for the per-point
  API can implement without touching the coordinator or the entities.
- Station measurements (`ch.meteoschweiz.ogd-smn`) are small per-station
  files and are refreshed every 10 minutes, matching their cadence.

## Consequences

- Users who want an hourly graph pay ~1 GB/day and know it: the option's
  description says so, and so does the README.
- Anything that raises the traffic (a new hourly parameter, a shorter
  interval, a second point per entry) needs to revisit this ADR, not just
  change a constant.
- When the point API ships, the backend swap is a contained change; this
  ADR is then superseded rather than edited.
- The daily default uses the local-calendar-day `p`-variants
  (`tre200px`/`tre200pn`/`rka150p0`), **not** the UTC-day `d`/`0`-variants
  (`tre200dx`/`tre200dn`/`rka150d0`): the `d`/`0` daily files are published for
  weather stations only, so the default postal-code point would get no
  temperatures or precipitation from them (issue #34). This raised the daily
  refresh from ~2 MB to ~5 MB — still three orders of magnitude below the
  hourly opt-in, so the decision above is unchanged.

## Revision (2026-08-28, issue #50)

The original Context said an HTTP `Range` request "cannot help" because the
files are sorted by time. Measuring the run `202608280400` at fixed byte
offsets corrected that premise: **the hourly files come in two layouts.**

| layout | files |
|---|---|
| **date-major** (all points per hour, ~150 KB/h) | `tre200h0`, `treq10h0`, `treq90h0`, `nprohihs`, `npromths`, `nprolohs` |
| **point-major** (one point's ~220 rows contiguous, ~5 KB) | `jww003i0`, `rre150h0`, `rre003i0`, `rp0003i0`, `fu3010h0`, `fu3010h1`, `dkl010h0`, `zprfr0hs`, `gre000h0`, `sre000h0` |

The origin (CloudFront over S3) answers `Range` with 206 and honours
`If-None-Match` **together with** `Range` (304 when unchanged). So each file
can be fetched with a strategy matched to its layout:

- **point-major → the point's block only.** A binary search over byte offsets
  with tiny `Range` probes locates the contiguous block, then one `Range` GET
  (~5 KB) fetches it. Three of the four minimum-set files become kilobytes.
- **date-major → a horizon prefix.** The earliest hours of all points sit at
  the file start, so `Range: bytes=0-<budget>` covers a chosen horizon.
  `tre200h0` is the only minimum-set file that stays in the megabytes.
- **The layout is detected at runtime** (offset probes classify the order),
  never hard-coded; an unrecognised order falls back to the full download and
  logs a warning. The weekly smoke test asserts the layout of every file.

**New option `hourly_horizon_days`** (options flow, only shown with the hourly
opt-in): how far ahead the hourly forecast is fetched, in **full local calendar
days** (Europe/Zurich — the boundary the daily `p`-variants and the app use).
Default **2** = the rest of today plus two full days (49–72 h). Choices 0–8 plus
"full run" (all ~220 h). It scales the date-major prefix; point-major files
always deliver the point's full run (they are cheap) and the entity trims to
the horizon so the forecast is consistent across files.

**Revised budget** (hourly opt-in, minimum set, default horizon): **~7–11 MB
per refresh** instead of ~125 MB — roughly **60–90 MB/day** instead of ~1 GB.
This does **not** change the decisions above: daily stays the default, hourly
stays opt-in and throttled to at most every 3 h, every request stays
conditional, and parsing stays in the executor keeping only the point's rows.
The seam and the announced per-point OGC Features API are unaffected. The
constants (`hourly_horizon_days`, the Range budget) live in `const.py`; raising
the traffic still means revisiting this ADR, not just editing a constant.

## Revision 2 (2026-08-28, issue #54)

Revision 1 kept the rule "never more often than every 3 hours". Measuring
all 24 runs of 2026-08-27 for two points (`docs/ogd.md`, "Change rhythm
across runs") showed that the hourly files change on model rhythms, not
hourly: the near term (today and tomorrow) moves at the runs of 02, 05, 08,
11, 14, 17, 20 and 23 UTC (ICON-CH1, every 3 h), days 2–5 at 04/05, 10/11,
16/17 and 22/23 UTC (ICON-CH2, every 6 h), and six runs a day change
nothing at all. Home Assistant, for its part, calls `async_forecast_hourly`
only while a subscriber exists or a `weather.get_forecasts` call asks.

Decisions that replace the flat 3-hour throttle:

- **Lazy fetch.** The hourly data is fetched from inside
  `async_forecast_hourly`, cached by run stamp and tier; the coordinator
  only tracks the run stamp. No subscriber and no service call means **no
  hourly download**, whatever the option says.
- **Near tier** — the date-major prefix up to the end of tomorrow (local
  day): refreshed when a new run exists and its UTC hour is one of
  02/05/08/11/14/17/20/23, or when the last near fetch is older than 3 h.
- **Far tier** — the rest of the chosen horizon (`hourly_horizon_days`):
  refreshed at the runs of 05/11/17/23 UTC, or when the last far fetch is
  older than 6 h.
- **Point-major files** (symbol, precipitation, wind, gusts, direction):
  refreshed with every new run — the point block costs ~5 KB.
- The landing hours and the two cadences live in `const.py` and are
  asserted by tests, like the old interval was.

Budget at the default horizon with a permanent subscriber: 8 near fetches
(~7 MB of `tre200h0` each) plus 4 far fetches (~4 MB more each) ≈ **70 MB
per day** — the same order as Revision 1, but the near term is at most one
run behind the model instead of up to three hours, and an instance nobody
looks at pays nothing. The seam and the announced per-point OGC Features
API remain the eventual replacement for all of this.

## Revision 3 (2026-08-28, issue #60)

**Daily wind fields on by default.** The three point-major wind files
(`fu3010h0`, `fu3010h1`, `dkl010h0`) are fetched concurrently with every
daily refresh and their per-point blocks aggregated into the daily forecast:

| field | meaning |
|---|---|
| `native_wind_speed` | maximum hourly mean wind speed of the day (km/h) |
| `native_wind_gust_speed` | maximum hourly gust of the day (km/h) |
| `wind_bearing` | direction at the hour of maximum wind speed (°) |

**Cost per run:** three block fetches at ~5 KB each ≈ **~15 KB** added to
the default daily refresh (~5 MB total). This is negligible next to the
existing daily files and well inside the budget for a default feature
(issue #34 set the bar at "order of 5 MB"; this raises it to ~5.015 MB).

**Guardrail:** `fetch_wind_block()` returns `None` for any file that is not
point-major. `_get_wind_texts()` in the backend logs a warning and sets a
sentinel value so no full-file download is ever triggered for a default
feature; wind fields remain `None` for that run.

**Cache sharing:** the wind block texts are cached by run stamp on the
backend instance. When the lazy hourly fetch runs for the same run, it
reuses the cached texts without a second download; when no cache hit exists
(e.g. hourly fetch before the first daily refresh), all `HOURLY_REQUIRED_PARAMS`
are fetched as before (pre-#60 behaviour).

The decisions above — daily stays default, hourly stays opt-in, every
request stays conditional, parsing stays in the executor — are unchanged.
The wind block fetches are guarded by the same point-major detection that
the hourly fetch uses (Revision 1); adding a non-point-major wind file in a
future run would only suppress wind fields, never trigger a full download.

## Revision 4 (2026-08-28, issue #55)

**Three additional point-major files join the hourly minimum set.** The files
`rp0003i0` (3-hour precipitation probability, integer %), `zprfr0hs` (zero-degree
level, m) and `gre000h0` (global radiation, W/m²) are all point-major (measured
2026-08-28: ~5 KB per point block). They are fetched whenever the hourly opt-in
is on — no per-entity gating, because each point block costs the same order as
the existing point-major files (symbol, precipitation, wind).

**Revised budget** (hourly opt-in, default horizon): the point-major group grows
from 5 to 8 files, adding ~15 KB per refresh of the point-major tier (~5 KB each).
Total point-major cost per run: **~40 KB** instead of ~25 KB — still negligible.
The date-major temperature file and the near/far tier schedule are unchanged.

**New hourly fields** exposed by the weather entity and the `HourlyForecast` model:
- `precipitation_probability` (B7): the 3-hour rolling probability ending at the
  forecast hour, mapped to the standard HA `precipitation_probability` forecast key.
- `zero_degree_level` (B8): m above sea level; additionally exposed as a sensor
  showing the current hour's value (snow-line material; hourly opt-in required).
- `radiation` (B10): global radiation (W/m²); hourly forecast attribute only.

The decisions above — daily stays default, hourly stays opt-in, every request
stays conditional, parsing stays in the executor — are unchanged.
