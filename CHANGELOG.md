# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Release tags carry a `v` prefix (e.g. `v0.1.0`); the release workflow
(`.github/workflows/release.yml`) turns an annotated tag into a GitHub release
using the matching section below as release notes.

## [Unreleased]

### Added

- **Settings map in CONFIGURATION.md** (#147). A new
  [What each setting controls](docs/CONFIGURATION.md#what-each-setting-controls)
  section lists every setting (setup, reconfigure, options, entity registry)
  alongside what it produces (entity and forecast field keys), its extra
  traffic cost, and what works without it. The disabled-entities section is
  expanded into a full grouped list (station / forecast / pollen / diagnostic).
  A new test guards the table and fails when a sensor key or extra hourly
  forecast field is added to the code without a matching entry.

- **Two diagnostic sensors show daily traffic to the MeteoSwiss backend** (#146).
  `sensor.<name>_data_fetched_today` (enabled by default, MB, `DATA_SIZE` device
  class, `total_increasing`) counts bytes transferred by the forecast coordinator
  since local midnight.  `sensor.<name>_requests_today` (disabled by default,
  plain count) tracks the number of HTTP requests.  Both reset at midnight,
  survive a restart within the day, and feed from the bytes/requests the existing
  fetch ladder already records — no extra traffic.

- **The hourly options page now ends with a summary of the consequences** (#145).
  After choosing the hourly forecast, horizon and the cloud/percentile extras, a
  confirmation step spells out — before anything is saved — which forecast fields
  the choice **adds or removes** compared with the current options, and an
  **estimated traffic per refresh and per day** for the combination. The estimate
  is derived from the demand registry (options → the demanded files → bytes) and a
  small table of typical bytes per file kind; where a file is already active, its
  last **measured** fetch from the run-scoped store is used instead of the table
  and the summary says "measured" rather than "estimated". For horizons that no
  longer fit row addressing it states the honest consequence (a large prefix of
  the ~30 MB source files) rather than a fixed number.

### Changed

- **The options dialog is now a menu with an overview page** (#144). Instead of a
  hidden wizard whose second page only appeared after ticking "hourly" and
  pressing submit, the options flow opens a menu with three entries: **Hourly
  forecast** (toggle, horizon, cloud layers and percentiles on one page),
  **Pollen monitoring** (toggle and station on one page) and **Overview**. The
  overview is read-only: it shows what is on now, which forecast fields that
  produces, and how many of the entry's entities are currently created disabled
  (with where to enable them), plus a pointer to `docs/CONFIGURATION.md`. Each
  page saves only its own options, so changing the hourly settings no longer
  resets the pollen settings or vice versa. The stored option keys are unchanged,
  so existing entries keep their settings without a migration.

### Fixed

- **A point-major-group file re-sorted to date-major upstream no longer falls to
  the whole ~30 MB file** (#153,
  [ADR-0008](docs/adr/0008-run-scoped-forecast-store.md)). `zprfr0hs` (zero-degree
  level) is date-major upstream since 2026-09-16 but still lives in the static
  point-major group; at "Full run" a changed canary (about every hour) dragged it
  to the whole ~32 MB file, and at a 3–8 day horizon to a 16–30 MB prefix — up to
  ~780 MB a day for one sensor value. Two changes fix this by construction: the
  **fetch ladder** now decides about chunking from a file's *detected* layout, not
  the group it was listed in — a date-major demand that overruns the request cap
  (a long window, or a whole run with too many blocks) is row-addressed in
  consecutive cap-sized windows (the #143 far-tail mechanism), with the prefix and
  whole file kept only as the per-window fallback; and the coordinator **schedules
  each demanded file by its last detected layout**, so a file that turned
  date-major follows the date-major near/far cadence (near window on a changed
  canary, far remainder at most every 6 h) instead of being re-fetched whole on
  every changed run. A genuinely point-major file is still one ~5 KB block per run;
  the hourly forecast still carries `zero_degree_level` for every hour to the
  horizon; default-horizon behaviour and cost are unchanged.

- **Long hourly horizons no longer read a multi-MB prefix on every run** (#143,
  [ADR-0008](docs/adr/0008-run-scoped-forecast-store.md)). With the hourly option
  on and a horizon of 3+ days or "Full run", a changed forecast (which MeteoSwiss
  publishes almost every hour) made the date-major group fall back to a prefix of
  the ~30 MB files — up to ~16–30 MB per file per hour, a regression against
  v0.3.1's 6-hourly far range. The date-major refresh is now split by distance:
  a changed run refreshes only the **near window** (what fits row addressing under
  the request cap, ~100–150 KB), while the **far remainder** rides the 6-hourly
  far cadence and is fetched by row addressing in consecutive cap-sized windows —
  never the prefix. The forecast still reaches the configured horizon at all
  times, because a near-only refresh keeps the previous run's far hours until the
  far refresh replaces them. Default-horizon behaviour and cost are unchanged. The
  horizon option text no longer warns that longer horizons read "a much larger
  part of the ~30 MB files on every refresh".

- **The option dialog and the docs describe what each setting really does and
  costs.** The texts still promised a refresh "at most every 3 hours", "7–11 MB
  per refresh" and cloud layers that "quadruple the traffic". Measured on a
  live instance, everything switched on costs about 1.2 MB per refresh, about
  hourly; the cloud layers are the cheapest extra (~0.05 MB) and the
  temperature percentiles the most expensive (~0.5 MB). Every option now says
  what it gives you, what it costs and what works without it, in all four
  languages, and `docs/CONFIGURATION.md` lists the entities that are created
  disabled. `docs/ogd.md` records that MeteoSwiss adjusts the near term about
  every hour, not on the 3-hourly rhythm measured in August.

- **`radiation` and `zero_degree_level` now appear in the hourly forecast** (#135).
  Both fields were already fetched and parsed (`gre000h0` / `zprfr0hs`, part of
  `HOURLY_REQUIRED_PARAMS`) but were silently dropped by `_as_hourly_forecast`.
  `weather.get_forecasts(type: hourly)` now carries `radiation` (W/m²) and
  `zero_degree_level` (m) for every hour that has a value; hours without a value
  omit the key rather than sending `null`.

### Added

- **Persisted fetch-ladder hints across restarts** (#133,
  [ADR-0008](docs/adr/0008-run-scoped-forecast-store.md)). The fetch ladder's
  per-file hints (the row-order layout and the point's learned byte positions,
  remembered per UTC day) are now saved per config entry with Home Assistant's
  storage and restored at setup, so the first refresh after a restart or reload
  is warm instead of re-discovering positions known a minute earlier (a cold
  refresh measured ~800 requests / ~2.7 MB). The hints are only ever hints: a
  stale, corrupt or foreign store is ignored — the ladder re-verifies every
  position and falls back to a fresh look, never a wrong row. Diagnostics report
  whether hints were restored; the store is removed with the config entry.

- **Fetch provenance in diagnostics** (#126,
  [ADR-0008](docs/adr/0008-run-scoped-forecast-store.md)). The integration's
  diagnostics dump now reports, per stored forecast parameter, the escalation
  ladder level (0–4), the number of HTTP requests, bytes fetched, and the last
  observed row-order layout of the upstream file. A short "How to read the
  fetch diagnostics" section in `docs/ogd.md` explains each field.

- **Escalated-fetch repair issue** (`forecast_fetch_escalated`, #126). When
  the same parameter needs a full or large-prefix download (fetch ladder level
  3 or 4) on three consecutive refreshes — usually because MeteoSwiss
  re-sorted the file's rows — the integration raises a Home Assistant repair
  issue naming the file and the bytes spent. The issue clears automatically
  once the fetch ladder finds a cheap strategy again.

- **Smoke-test layout report** (#126). The weekly smoke test now prints the
  observed row-order layout for every hourly file so a layout drift is visible
  in the CI log without waiting for three escalated fetches.

### Fixed

- **`zprfr0hs` fetched once per run with the hourly option on** (#134). With
  the hourly option on, the zero-degree level file (`zprfr0hs`) was fetched
  twice per run: once by the daily path (fixed 48 h window) and again by the
  hourly refresh (configured horizon, typically 49–72 h). The shared series
  cache from #123 could not serve the second request because 48 h < the hourly
  horizon. The daily path now uses `max(48 h, hourly horizon)` as the
  zero-degree window so one fetch serves both consumers; with the hourly option
  off the 48 h window is unchanged.

### Changed

- **A refresh is a steady trickle, not an ~800-request burst** (#132,
  [ADR-0008](docs/adr/0008-run-scoped-forecast-store.md)). A cold forecast
  refresh fans out every demanded file at once, each row-addressed with several
  small byte-range reads; with the hourly option and the cloud/percentile layers
  on this was ~800 requests in ~2 s against `data.geo.admin.ch`. The volume was
  always inside the budget, but the burst was not (the MeteoSwiss terms of use
  name access frequency as well as volume). All requests of a refresh now share
  one concurrency limiter (`OGD_MAX_CONCURRENT_REQUESTS` = 6), so at most six are
  in flight at once. User-visible effect: a cold refresh takes ~10–20 s instead
  of ~2 s; the first refresh still blocks setup and setup does not time out. The
  per-file request cap is unchanged.

- **A canary read decides whether a new run is refreshed** (#125,
  [ADR-0008](docs/adr/0008-run-scoped-forecast-store.md)). Whether a new forecast
  run is worth downloading is no longer decided by a timetable measured once
  (the near/far "landing hours"). On every new run the integration now reads a
  cheap canary — the point's next few hours of one representative file per group
  (the temperature file for the date-major group, the wind file for the
  point-major group), a few KB using the remembered byte positions — and compares
  it with what it already holds: unchanged values keep the stored series and
  re-stamp it to the run (so diagnostics show it current, not stale), while
  changed values, or a canary that cannot be read, refresh the group. The same
  idea gates the daily files, so an unchanged run no longer re-downloads them.
  The near/far/point-major max-age fallbacks stay as a backstop that still forces
  a refresh, so a canary blind spot can never let a series go stale unbounded.
  User-visible effect: quiet runs (a new run whose content did not move) cost a
  few KB instead of a full group refresh, and the forecast reads as current for
  the run rather than stale.

- **Hourly forecast fetched eagerly; the lazy provider is removed** (#124,
  [ADR-0008](docs/adr/0008-run-scoped-forecast-store.md)). With the hourly
  option on, the demanded files are now fetched by the forecast coordinator's
  own refresh — whether or not a card or automation subscribes — and filed in a
  per-parameter forecast store that every consumer reads. The hourly forecast,
  the current-hour condition and the zero-degree sensor no longer depend on
  another consumer's fetch: the data is there after the first refresh. Which
  files a run fetches comes from a demand registry keyed on the enabled features
  (hourly, cloud layers, temperature percentiles), so a feature that is off
  still fetches nothing; the near/far/point-major refresh cadence is unchanged.
  User-visible effect: an instance with the hourly option on now pays for it
  even when nobody is looking (the traffic cost gates on the option, not on an
  open card).

- **Hourly forecast fetched through the ladder, with a two-way shared cache**
  (#123, [ADR-0008](docs/adr/0008-run-scoped-forecast-store.md)). The hourly
  forecast no longer downloads a date-major file (`tre200h0`) as a multi-MB
  horizon prefix: every parameter is fetched through the escalation ladder for
  the window `[start of the current hour, horizon)`, so a date-major file is
  row-addressed for the horizon (~100–300 KB) and a point-major file returns its
  ~5 KB block. Since MeteoSwiss re-sorted `zprfr0hs` to date-major, this removes
  a hidden ~10 MB per run whenever the hourly option was on. The daily and hourly
  paths now share **one per-run cache keyed by parameter** that records the window
  each text covers: a file either path fetched for the current run is reused by
  the other without a second download, in either order, and a windowed cache
  entry (the daily 48 h zero-degree window) never silently shortens a longer
  hourly horizon.

- **Daily forecast files fetched by row addressing** (#122,
  [ADR-0008](docs/adr/0008-run-scoped-forecast-store.md)). The four daily files
  (`tre200px`, `tre200pn`, `rka150p0`, `jp2000d0`, ~1.3 MB each) are now routed
  through the fetch ladder instead of downloaded whole. Each is date-major with
  nine day blocks (`Date` stamped `YYYYMMDD0000`), so the ladder addresses the
  blocks at a one-day step and reads a few KB per file, climbing to the full
  file only when it cannot prove all nine days. A changed daily refresh drops
  from ~5.3 MB — the largest regular cost of a default installation — to under
  ~100 KB of row reads. The layout classifier now also recognises a
  few-block date-major file (its probes see repeated, not strictly increasing,
  dates), after ruling out the point-major orders so no point-major file is
  ever misread.
- **One hint object per forecast file, remembered per UTC day** (#121,
  [ADR-0008](docs/adr/0008-run-scoped-forecast-store.md)). The fetch ladder no
  longer re-classifies a file's layout and re-reads its header, first and last
  row on every refresh. It carries a single hint per file (layout, the UTC day
  it was learned on, the point-major block offset or the date-major row
  geometry, and the header/first/last stamps) that a later run of the same UTC
  day reuses, verifying it through the rows it finds. A hint from another day or
  for another layout is detected and costs a fresh look, never a prefix or the
  whole file. A warm zero-degree window now needs about one request per hour
  (previously ~90+ for a 72 h window) and a warm point block at most three, so a
  request cap miss no longer throws the whole cheap fetch away.
- **Day-item run discovery** (#120, [ADR-0008](docs/adr/0008-run-scoped-forecast-store.md)).
  Run discovery now fetches today's UTC day item (~80 KB, ETag-capable) instead
  of the full collection listing (~620 KB, no ETag). An unchanged item costs a
  single conditional 304 with 0 bytes. Falls back to yesterday's day item (for
  the brief window after 00:00 UTC when the new day's item is not yet published)
  and then to the full listing as a last resort. Traffic for discovery drops from
  ~15 MB/day to a few KB/day per instance.

## [v0.3.1] — 2026-09-19

### Changed

- **One forecast store per entry** (#116, [ADR-0008](docs/adr/0008-run-scoped-forecast-store.md)).
  Per-hour values such as the zero-degree level now live in one place that
  every fetch path fills and every entity reads. The run is discovered once
  per refresh and handed down, so a refresh costs one ~600 KB STAC listing
  instead of two to four. The diagnostics dump lists, per parameter, which
  run is stored, which path delivered it and whether it is held over.

### Fixed

- **The zero-degree level sensor no longer depends on which path fetched the
  file, or how MeteoSwiss sorts it** (#116). MeteoSwiss re-sorted `zprfr0hs`,
  so the small block fetch of v0.3.0 stopped applying and the sensor read
  `unknown`. Files are now fetched through an escalation ladder: the cheapest
  read the file's layout admits (a point block, or the point's row addressed
  directly inside each hour block), then wider reads, a prefix and finally the
  whole file, until every demanded hour is proven present. The integration
  never again answers `unknown` to save traffic; it does so only when
  MeteoSwiss has no data, and even then the previous run's series stays in
  place. For the re-sorted file this costs about 100–300 KB per refresh
  instead of the 33 MB file. The same applies to the daily wind and
  precipitation-probability blocks should their files ever be re-sorted.

## [v0.3.0] — 2026-09-17

### Added

- **Daily precipitation probability** (#112). Each day of the daily forecast now
  carries a `precipitation_probability`, shown by the weather card. MeteoSwiss
  publishes no daily probability parameter, so it is derived from the hourly
  3-hour probability (`rp0003i0`): the maximum over the local calendar day — the
  conservative "chance of rain today" figure. The value is folded into the
  default daily refresh via one ~5 KB point-major block fetch, next to the wind
  blocks (no measurable traffic cost, [ADR-0002](docs/adr/0002-traffic-budget-bulk-local-forecast.md)
  revision 5); with the hourly option on, the block is fetched once per run and
  reused.

- **Measurement time sensor** (#105). A new `sensor.<name>_measurement_time`
  entity (`device_class: timestamp`, `entity_category: diagnostic`) exposes the
  `reference_timestamp` of the latest station observation. Disabled by default;
  enable it to build automations that detect a stale or dead station (e.g. raise
  an alert when the measurement time has not advanced for more than an hour).

### Fixed

- **The zero-degree level sensor no longer needs the hourly forecast option**
  (#107). `sensor.<name>_zero_degree_level` used to read the lazy hourly
  cache, so it showed a value only when the hourly option was on, something
  had already subscribed to the hourly forecast, and that had happened before
  the coordinator's hourly tick — for most users it was permanently
  `unknown`. The zero-degree file is point-major, so its ~5 KB point block now
  rides along with every daily refresh next to the wind blocks (ADR-0002
  revision 6): the sensor has a value after the first refresh with the option
  off, no card open and no `get_forecasts` call, and it advances at the top
  of every hour. With the option on, the hourly fetch reuses the block, so
  nothing is downloaded twice. Each block file now degrades on its own: a
  wind file that is not point-major no longer blanks the zero-degree level,
  and vice versa.

- **A transient forecast failure no longer makes the weather entity
  unavailable** (#108). The entity's availability is now derived from
  whether each coordinator *has data* rather than whether its most recent
  refresh succeeded. Station observations and automations that depend on the
  weather entity survive a brief forecast outage (a STAC hiccup, a slow run
  rollover, a 5xx from `data.geo.admin.ch`) without going `unavailable`.
  A coordinator that has never succeeded (no data at all) still causes the
  entity to report `unavailable`.

- **The condition no longer stays on the night variant after sunrise**
  (#103). MeteoSwiss keeps the night variant of the hourly symbol
  (`jww003i0`) for a couple of hours past sunrise, so the entity reported
  `clear-night` in broad daylight. The current condition is now reconciled
  with `sun.sun` in both directions: a night code with the sun up renders as
  its day counterpart, a day code with the sun down as its night counterpart
  (the latter already applied to the daily symbol). The per-hour conditions
  in the hourly forecast are unchanged — those are future hours, which
  `sun.sun` cannot answer for.

## [v0.2.2] — 2026-08-31

### Fixed

- **`cloud_coverage` and the cloud-layer attributes now carry correct
  percentages** (#97). MeteoSwiss silently changed the three cloud-cover
  files (`nprohihs`, `npromths`, `nprolohs`) from percent (0–100) to fraction
  (0–1) between 2026-08-27 and 2026-08-31. The parser now applies a tolerant
  per-file heuristic: if every non-`None` value is ≤ 1.0 the file is treated
  as fraction-encoded and multiplied by 100, so a future silent revert to
  percent encoding is also handled correctly.

## [v0.2.1] — 2026-08-29

### Fixed

- **The hourly forecast no longer starts in the past, and no longer
  shows blank leading hours** (#92). The hourly parser bounded only the
  end of the window, so every refresh delivered the hours the model run
  covers *before* now — up to a full day of them — and the first hours
  came out ragged, because the parameter files do not all begin at the
  same hour: an entry would carry a temperature but no icon, no
  precipitation and no wind, and a weather card rendered it as a blank
  slot. Hours before the current hour are now discarded at parse time
  (the running hour is kept), and an hour is only delivered when it
  carries temperature, symbol, precipitation and wind speed. The
  precipitation-probability, zero-degree-level, radiation, cloud-layer
  and temperature-percentile fields stay optional, so a disabled option
  never drops an otherwise good hour. Present since v0.1.1 and made more
  visible by the extra parameter files v0.2.0 added.

## [v0.2.0] — 2026-08-28

### Added

- **Pollen sensors** (#53, #67, ADR-0005). An opt-in pollen option in the
  options flow picks the nearest station of the `ch.meteoschweiz.ogd-pollen`
  network (the three nearest offered) and creates one sensor per taxon that
  station actually measures — grasses and birch enabled by default, alder,
  hazel, beech, ash and oak shipped but disabled. Concentrations in
  grains/m³, refreshed at most once an hour with conditional requests. The
  taxon codes come from the file header and their names from the parameter
  metadata, so nothing about the taxon list is hard-coded. Pollen setup is
  non-fatal: a failure there never blocks the entry.
- **Reconfigure instead of delete-and-re-add** (#52). The forecast point and
  the weather station can now be changed through Home Assistant's standard
  reconfigure step, which re-offers both with the current choices
  pre-selected and updates the entry in place — history and automations
  survive a change that previously meant deleting the entry. When the
  station really changes, a history step asks what to do with the values
  recorded so far: **keep** them (the default; the switch is written to the
  logbook so the seam stays findable), **discard** them (purges the station
  sensors' recorded states and clears their long-term statistics), or
  **backfill** them from the official station history. A point-only change
  never touches history.
- **Mountain forecast points** (#59). Setup and reconfigure now open with a
  mode step: a postal-code point as before, or one of the 631 mountain
  points of interest, offered in a dropdown with altitude labels and
  pre-selected to the one nearest the Home Assistant location.
- **Wind on the daily forecast, on by default** (#60, ADR-0002 revision 3).
  Daily entries now carry wind speed, gust speed and bearing. The three
  point-major wind files are fetched as ~5 KB per-point blocks alongside
  every daily refresh and aggregated per local calendar day. Wind stays
  best-effort: a file that is not point-major, has not been published yet
  for the run, or fails to fetch degrades wind to `None` instead of failing
  the daily refresh — the ~30 MB full file is never downloaded for a
  default feature.
- **Hourly precipitation probability, zero-degree level and radiation**
  (#55, ADR-0002 revision 4). Three more point-major files join the hourly
  set at roughly 5 KB each: precipitation probability is exposed on the
  hourly forecast under Home Assistant's standard key, the zero-degree
  level as its own sensor (disabled by default), and global radiation on
  the hourly data.
- **Cloud layers and temperature percentiles, per-entity gated** (#69).
  Hourly cloud cover in three layers (high, medium, low) and the p10/p90
  temperature percentiles, both off by default. These are date-major files
  — the expensive path, one horizon prefix each — so they are fetched only
  while their option is on, the per-entity gating of ADR-0002. With neither
  enabled the fetch set is byte-for-byte the one before. `cloud_coverage`,
  Home Assistant's single number, is the maximum of the three layers.
- **Optional separate precipitation station** (#70, ADR-0006). The station
  step of setup and reconfigure now offers an optional second station from
  the automatic precipitation-only network (`ch.meteoschweiz.ogd-smn-precip`,
  ~141 gauges), with the three nearest offered and **none selected by
  default**. When set, the `precipitation` sensor and the weather entity's
  `current_precipitation` attribute read from it — its attribution and a
  `station` attribute name the station — while every other value stays with
  the main station. It is polled every 10 minutes, conditionally, only while
  the option is set (~1.2 KB per poll, inside the ADR-0002 station budget);
  unset means zero requests to the precipitation collection.
- **Service `import_history`: backfill long-term statistics from the
  official station history** (#66, ADR-0007). A one-off, user-triggered
  service imports a station's hourly history (`_h_recent` plus the decade
  files) into Home Assistant's long-term statistics under the integration's
  own sensor statistic ids: mean/min/max for temperature, mean for humidity,
  dew point, pressure, wind, gust and radiation, and an hourly sum for
  precipitation. Optional `start`/`end` (default: the current year);
  overlapping periods are replaced, not duplicated. Long-term statistics
  only — the raw states are untouched. Nothing polls the history files, so
  this stays outside the recurring ADR-0002 budget. The shared
  `async_backfill` layer also powers the reconfigure flow's backfill choice
  (#52).

### Changed

- **Hourly forecast now fetched with HTTP Range, plus a horizon option**
  (#50). The bulk hourly files come in two layouts: point-major files are
  fetched as the configured point's contiguous ~5 KB block (located by a
  binary search over byte offsets), and the one date-major minimum-set file
  (`tre200h0`) as a `Range` prefix covering the chosen horizon. Layout is
  detected at runtime and falls back to a full download if unrecognised. A
  new `hourly_horizon_days` option (options flow, shown only with the hourly
  opt-in) chooses how far ahead to fetch, in full local calendar days —
  default 2 (the rest of today plus two full days), plus a "full run" choice.
  The hourly opt-in now costs roughly 7–11 MB per refresh at the default
  horizon instead of ~125 MB. Revises ADR-0002.
- **The hourly forecast is only downloaded when something asks for it**
  (#54, ADR-0002 revision 2). The bulk hourly fetch moved out of the
  coordinator's polling path into `async_forecast_hourly`, so it happens
  only while a card, an automation or a `weather.get_forecasts` call is
  actually subscribed. An instance nobody looks at pays nothing, even with
  the hourly option on.
- **Hourly refresh follows the measured model-run rhythm** (#68, ADR-0002
  revision 2). The flat 3 h staleness floor gave way to three independently
  scheduled groups: a **near tier** (temperature to the end of tomorrow) at
  the ICON-CH1 landing hours or after 3 h, a **far tier** (temperature out
  to the configured horizon) at the ICON-CH2 hours or after 6 h, and the
  **point-major group** (precipitation, symbol, wind, gust, direction) with
  every new run. The three merge by hour into one forecast. At the default
  horizon a permanently subscribed instance settles around 70 MB/day.

## [v0.1.1] — 2026-08-27

### Fixed

- **Weather symbol table was invented, so most forecast conditions were
  wrong** (#44). The `jp2000d0`/`jww003i0` icon-code → HA-condition map in
  `symbols.py` did not describe the MeteoSwiss icon set: code `2` reported
  `sunny` instead of `partlycloudy`, `26` reported `snowy` (a 22 °C
  September day) instead of `sunny`, `27`/`28` were rain/thunder instead of
  `fog`, `38` was `hail` instead of `lightning-rainy`, and more — roughly
  every second forecast day in Switzerland got a wrong icon. The table is
  now copied faithfully from the reference in
  `Rudd-O/homeassistant-meteoswiss` (MIT), which dumps the official
  MeteoSwiss weather-icon spreadsheet, and credited in the module docstring
  and `docs/symbols.md`.
- **Night codes are now mapped from their own entries** instead of being
  synthesised as `day − 100` with only `sunny → clear-night`. The icon set
  assigns 101–142 independently (e.g. `26` is `sunny` but `126` is
  `cloudy`), and night codes do occur in the hourly file.
- The symbol test no longer validates the table against itself; it pins a
  set of codes to the reference condition and asserts every code 1–42 and
  101–142 resolves, so a gap can no longer make the entity report no
  condition at all.

## [v0.1.0] — 2026-08-27

First release: the integration produces a live `weather` entity per Swiss
postal code, plus the sensors of the chosen SwissMetNet station.

### Added

- **`weather` entity** with current conditions (temperature, humidity,
  pressure, wind speed and direction, precipitation) sourced from the
  nearest SwissMetNet station (10-minute values) and a 9-day daily forecast
  sourced from the MeteoSwiss local-forecast file for the configured point.
- **Hourly forecast** as an opt-in option (off by default; ADR-0002). When
  enabled, the weather entity advertises `FORECAST_HOURLY` and serves
  temperature, precipitation, symbol, wind speed, gust and bearing per hour
  from the bulk local-forecast files. The download (~1.5 GB/day) is throttled
  to `HOURLY_FORECAST_MIN_INTERVAL` (3 h) regardless of how often a new run
  appears, and the current hour's symbol drives the entity `condition` when
  available. Toggling the option reloads the entry.
- **Station sensors**: temperature, humidity, dew point, pressure (QFF, and
  QFE as a diagnostic), wind speed, bearing, gust, 10-minute precipitation,
  sunshine duration and global radiation, refreshed every 10 minutes.
- **`ogd/` client package** — pure Python (no HA imports): STAC catalogue
  discovery, station CSV download and parsing, local-forecast CSV download
  and parsing, weather-symbol mapping to HA condition strings.
- **Three-step config flow**: postal-code entry → forecast point selection
  → nearest-station confirmation. Options flow lets users toggle hourly
  forecast.
- Station and forecast `DataUpdateCoordinator`s with conditional HTTP
  requests and executor-offloaded CSV parsing.
- **Weekly upstream smoke test** (`tests/tools/smoke_test.py`, ADR-0004): the
  only check that touches live data. It reads the parameter codes from the
  integration itself and requires the configured postal-code point in every
  daily file — the property whose absence caused the defect below.

### Fixed

- **Daily forecast now has temperatures and precipitation for postal-code
  points.** The daily client fetched `tre200dx`/`tre200dn`/`rka150d0`, which
  MeteoSwiss publishes for weather stations only — so the default configuration
  (a postal-code point) silently got `temp_max`/`temp_min`/`precipitation` of
  `None` and only a symbol. It now fetches the local-calendar-day
  `tre200px`/`tre200pn`/`rka150p0` variants, which cover every point type. The
  daily refresh grows from ~2 MB to ~5 MB, still far below the hourly opt-in
  (ADR-0002). Caught by the smoke test before this release. (#34)
- CI: the Claude agent workflows check out with `persist-credentials: false`.
  `actions/checkout@v7` keeps `GITHUB_TOKEN` in an `includeIf` credentials
  file that `claude-code-action` does not clear, so the reviewer's fix commit
  on PR #26 was pushed as `github-actions[bot]` and its CI runs waited for a
  manual approval instead of letting auto-merge proceed.
- CI: `claude-review.yml` guarded on `draft == true`, which is false by
  definition on a `ready_for_review` event, so an agent marking its own draft
  ready skipped the independent review entirely.

## [v0.0.1] — 2026-08-26

Repository scaffold. Not released: the first release is cut once the
integration produces a weather entity (see the tracking issue in the README).

### Added

- Integration skeleton `custom_components/meteoswiss_weather` with a
  postal-code config flow, brand icons and English strings; no platforms yet
- ADR-0001 (official Open Data is the only upstream), ADR-0002 (traffic
  budget for the bulk local-forecast files), ADR-0003 (sibling of the radar
  integration, not a merge), ADR-0004 (quality gates and release process
  inherited from the radar repo)
- `docs/ogd.md` with the measured facts about the MeteoSwiss open data
  files, so contributors and agents do not have to rediscover them
- CI (hassfest, HACS validation, ruff, pytest), CodeQL, SonarCloud, the
  tag-triggered release gate with a zip asset, and the opt-in Claude agent
  workflows (label, mention, autopilot, reviewer)

[Unreleased]: https://github.com/chriguschneider/hass-meteoswiss-weather/compare/v0.3.1...HEAD
[v0.3.1]: https://github.com/chriguschneider/hass-meteoswiss-weather/compare/v0.3.0...v0.3.1
[v0.3.0]: https://github.com/chriguschneider/hass-meteoswiss-weather/compare/v0.2.2...v0.3.0
[v0.2.2]: https://github.com/chriguschneider/hass-meteoswiss-weather/compare/v0.2.1...v0.2.2
[v0.2.1]: https://github.com/chriguschneider/hass-meteoswiss-weather/compare/v0.2.0...v0.2.1
[v0.2.0]: https://github.com/chriguschneider/hass-meteoswiss-weather/compare/v0.1.1...v0.2.0
[v0.1.1]: https://github.com/chriguschneider/hass-meteoswiss-weather/compare/v0.1.0...v0.1.1
[v0.1.0]: https://github.com/chriguschneider/hass-meteoswiss-weather/compare/v0.0.1...v0.1.0
[v0.0.1]: https://github.com/chriguschneider/hass-meteoswiss-weather/releases/tag/v0.0.1
