# ADR-0008: One run-scoped forecast store, filled by an escalating fetch

- **Status:** Accepted
- **Date:** 2026-09-18
- **Supersedes:** the "never a full download for a default
  feature" guardrail of ADR-0002 revisions 3, 5 and 6, and the lazy hourly
  provider of ADR-0002 revision 2. The rest of ADR-0002 stays.

## Context

v0.3.0 shipped the zero-degree sensor on the daily refresh (issue #107) and
it still reads `unknown` on the only live instance. The cause is structural,
not a bug in that change. Measured 2026-09-18 against live data:

- **Two orchestrators, two tool sets, one one-way bridge.** The eager path
  (`ForecastCoordinator` → `fetch_daily`) can fetch a whole small file or a
  point-major block, else it gives up. The lazy path
  (`HourlyForecastProvider` → `fetch_hourly`) can also do a date-major prefix
  and a full download, but only runs while a card subscribes or a service
  call arrives, so it guarantees nothing. The only shared state is the
  backend's block cache, and it flows daily → hourly only.
- **Upstream re-sorts files without notice.** `zprfr0hs` was point-major on
  2026-08-28 and is date-major since at least 2026-09-16; the three cloud
  files flipped the other way on 2026-08-31 (issue #100). MeteoSwiss
  documents no row order (research 2026-09-18). A strategy that only works
  for one layout will break again.
- **The guardrail trades quality for bytes.** When the block strategy does
  not apply, the eager path degrades to `None`. With the hourly option on,
  the same file is meanwhile fetched by the lazy path as a ~10 MB prefix per
  run, because it still sits in the "point-major, ~5 KB" group.
- **Overhead hides in discovery.** One `latest_run()` listing is 608 KB,
  unconditional, and is issued by the coordinator, by `fetch_daily` and by
  every `fetch_hourly` call: 15 to 50+ MB a day. A "5 KB" block really costs
  30 to 54 KB and 14 to 38 requests, because the layout is re-classified on
  every fetch.
- **A date-major file is still addressable per row.** In `zprfr0hs` every
  hour block has the same row count (5632) and the point sits at the same
  row index (2832) in every block; 17 hours across 48 h, three runs and
  three days were fetched with 160-byte Range reads, all correct.
- **One fetch carries nine days.** Every file holds the whole run from
  21:00 UTC of the previous day, and a point's values move only at the model
  landing hours (docs/ogd.md, "Change rhythm across runs"). An entity can
  step through cached hours without any request.
- The per-point OGC Features API is announced as a *beta* by the end of
  2026, as intent only. The bulk files stay the upstream for now.

The owner's principle for this decision: **load as little as possible, but
never at the cost of quality. When the cheap way cannot prove it delivered
everything, load more, up to the whole file.**

## Decision

### 1. One store per config entry is the only source for forecast entities

Add a `ForecastStore` (`custom_components/meteoswiss_weather/store.py`),
owned by `ForecastCoordinator`. For the current run it holds, per parameter,
the configured point's series `{hour → value}` plus provenance (run stamp,
escalation level used, bytes, requests, fetched at).

- Every forecast consumer — daily forecast, daily wind and probability
  aggregates, zero-degree sensor, hourly forecast, condition sharpening —
  **reads the store and nothing else**. No entity, provider or service call
  triggers a download of its own.
- The store keeps the **last good series per parameter** until the new run's
  series has passed its completeness check. A failed or partial refresh never
  blanks an entity: the previous run still covers the coming hours, and the
  parameter is flagged stale in diagnostics.
- Entities that show "the current hour" re-evaluate at the top of each hour
  from the store.

### 2. A demand registry decides what a run must deliver

Each consumer declares `Demand(param, window)`; `ogd/const.py` stops carrying
parallel parameter lists with implied strategies. The union of the demands
of all **enabled** features is the fetch plan of a run:

| consumer | params | window |
|---|---|---|
| daily forecast | `tre200px`, `tre200pn`, `rka150p0`, `jp2000d0` | whole run |
| daily wind / probability | `fu3010h0`, `fu3010h1`, `dkl010h0`, `rp0003i0` | whole run |
| zero-degree sensor | `zprfr0hs` | now … +48 h |
| hourly forecast (option on) | the hourly set | now … configured horizon |
| cloud layers / percentiles (options on) | their files | as hourly |

A feature that is off demands nothing, so the cost gate of ADR-0002 stays on
the **option**, not on whether a card happens to be open. The lazy provider
and its card-driven refresh go away: when the option is on, the data is there.

### 3. The refresh is driven by the run, once per tick

`ForecastCoordinator` discovers the run **once per tick** and passes the
`Run` object down; nothing below it lists STAC again. On a new run it
refreshes each demanded parameter when its content can have changed: at the
landing hours of ADR-0002 revision 2, or when the stored series is older than
the tier's max age, or when a cheap **canary** read (the point's next few
hours, a few KB) differs from the store. The canary replaces the assumption
"these six runs never change anything" with a check.

### 4. Every file is fetched by one escalation ladder

`ogd/hourly.py` gains a single entry point
`fetch_series(run, param, point, window) -> Series` that climbs until a level
**verifies complete**, then stops:

| level | what it reads | typical cost |
|---|---|---|
| L0 | remembered byte positions from an earlier run of the same UTC day, one verifying read each | ~1 KB per block or row |
| L1 | layout-aware addressing: binary-searched point block (point-major); hour block by `Date` plus fixed row index (date-major) | 10s of KB |
| L2 | widened windows: whole hour blocks / re-searched block | ~150 KB per hour block |
| L3 | prefix up to the end of the demanded window | MBs |
| L4 | the whole file | ~30 MB |

**Completeness check after every level:** each returned row carries the
configured `point_id`/`point_type_id` and an expected `Date`; every hour of
the demanded window that the run covers is present exactly once; every value
parses. Anything else is "not proven" and the ladder climbs. A level that
would need more than **96 requests per file** is skipped in favour of the
next one, so frugality in bytes never turns into a request storm (the terms
of use name access frequency as well as volume). 96 lets a 72 h horizon stay
on row addressing; anything longer goes to a prefix.

**Quality first:** L4 is always allowed, for default features too. The
integration degrades to the last good series only when upstream itself has
no data (file missing from the run, HTTP errors after retries) — never
because a cheaper strategy did not apply. Layout and positions are remembered
per file per UTC day (offsets are stable across a day's runs) instead of
being re-classified on every fetch.

### 5. Escalation is visible

Provenance per parameter goes into diagnostics. A parameter that needed L3
or L4 on three consecutive refreshes raises a repair issue
(`forecast_fetch_escalated`) naming the file and the bytes spent, and the
weekly smoke test reports each file's layout and level. An upstream re-sort
then shows up as a traffic finding within hours, not as an `unknown` sensor.

### 6. The backend seam stays

`ForecastBackend` becomes `fetch(run, demands) -> dict[param, Series]`. The
bulk-CSV backend implements it with the ladder; the future per-point API
backend implements it with one request, and the store, the coordinator and
the entities do not change.

## Consequences

- **Fixes by construction:** the zero-degree sensor (L1 row addressing today,
  L3/L4 if that ever stops verifying), the hidden ~10 MB per run for
  `zprfr0hs` with the hourly option on, and the race behind issue #107 as a
  class — no consumer depends on another consumer's fetch any more.
- **Traffic, expected:** discovery drops from 2–4 listings of 619 KB per run
  to one conditional 80 KB day item per tick; blocks drop from 14–38 requests
  to a few once positions are remembered per day; the four daily files drop
  from ~5.3 MB per changed run to under 100 KB of row reads. A default
  installation goes from roughly 130+ MB a day to a few MB.
- **Traffic, worst case:** a file that defeats L0–L3 costs ~30 MB per due
  refresh. At the 3 h near cadence that is ~240 MB a day per such file. This
  is accepted deliberately and made loud (section 5) rather than prevented.
- **An idle instance with the hourly option on now pays for it.** ADR-0002
  revision 2 ("an instance nobody looks at pays nothing") is given up in
  exchange for data that is always there. With one known installation this is
  the owner's call; it should be revisited before the integration is promoted.
- **More requests of smaller size.** Row addressing the hourly temperature
  file for a 72 h horizon is ~72 small reads instead of a 7–11 MB prefix.
  The request cap in section 4 bounds this.
- `HourlyForecastProvider`, `_get_block_texts`, the `DAILY_BLOCK_PARAMS` /
  `HOURLY_POINT_MAJOR_PARAMS` groupings and `DailyBundle.zero_degree_level`
  are replaced by the store and the demand registry.

### Concurrency, added 2026-09-19 (issue #132)

"More requests of smaller size" turned out to be a *burst*, not just a count:
the first live cold refresh with the hourly option and the cloud/percentile
layers on issued **~800 requests in ~2 s** against `data.geo.admin.ch` (19
files, each row-addressed under one `asyncio.gather`). The volume is fine
(~2.7 MB) but the terms of use name access frequency as well as volume. A
single `asyncio.Semaphore` owned by `BulkCsvBackend` and shared across every
file of a refresh now caps the requests in flight to
`OGD_MAX_CONCURRENT_REQUESTS` (6, `ogd/const.py`), so a refresh is a steady
trickle (~10–20 s cold) rather than a spike. This bounds concurrency *across*
files and is orthogonal to the per-file `SERIES_REQUEST_CAP` of section 4. The
first refresh still blocks setup and setup does not time out.

### Verified 2026-09-18 (run `202609180600`, point 309800;2)

- **Row addressing holds for variable-width files.** In `tre200h0` every hour
  block has the same row count and the same point order, and the point sits
  at the same row index in every block. Block sizes vary by about ±60 bytes
  per hour, so a position extrapolated from the file start drifts (4 KB
  window up to +12 h, 64 KB up to +8 days, 34 KB off at +9 days). L1 must
  therefore **chain**: each verified row re-anchors the prediction for the
  next hour, which keeps the window at 1–2 KB per hour, or it anchors every
  12 h first and fills the gaps in parallel. A 72 h horizon is then roughly
  100–150 KB instead of a 7–11 MB prefix.
- **The row index is per file, not global.** `tre200h0` carries 5629 points
  per block, `zprfr0hs` and `tre200px` 5632; the index happens to match
  (2832) only because the extra points sort later. The store learns and
  verifies it per file.
- **The daily files are addressable too.** `tre200px` is date-major with nine
  day blocks of 5632 rows (~148 KB each, ±100 bytes) and the point at a
  fixed row index. Four daily files are 36 row reads, well under 100 KB,
  instead of ~5.3 MB per changed run — the largest regular cost today.
- **Run discovery can be conditional.** The items listing is 619 KB with
  `Cache-Control: max-age=600` and **no** `ETag`. The single day item
  (`/items/<yyyymmdd>-ch`) is 80 KB and carries an `ETag`; a request with
  `If-None-Match` answered **304 with 0 bytes**. A `HEAD` on a run's asset
  answers 200 when published and **403** (not 404) when not, in under 1 KB.
  STAC stays the source of truth; the day item replaces the listing, and the
  listing remains the fallback at the UTC day rollover.

### Migration, one PR each

1. `ForecastStore`, single run discovery, last-good retention, both existing
   paths writing into the store. No change in what is fetched.
2. The escalation ladder with row addressing and the completeness check;
   the zero-degree sensor reads the store. Closes the v0.3.0 regression.
3. Demand registry; hourly forecast and options move onto the store; the
   lazy provider is removed. ADR-0002 marked as partly superseded.
4. Provenance in diagnostics, the repair issue, smoke-test reporting,
   `docs/ogd.md` re-measured.

## Decided by the owner, 2026-09-18

1. **The lazy hourly path is removed.** With the hourly option on, the data
   is fetched whether or not anything subscribes (section 2). Row addressing
   brings a 72 h horizon to 100–150 KB, which removes the reason for laziness.
2. **The canary decides, not a timetable.** Every new run gets the cheap
   canary read; the landing hours of ADR-0002 revision 2 remain only as the
   max-age fallback (section 3). One measurement from August is not a
   contract.
3. **96 requests per file** is the ceiling before the ladder jumps a level
   (section 4).
