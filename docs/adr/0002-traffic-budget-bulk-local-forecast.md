# ADR-0002: Traffic budget for the bulk local-forecast files

- **Status:** Accepted
- **Date:** 2026-08-26

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
  an HTTP Range request can shorten the download;
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
