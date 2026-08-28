# Feature options — pick list

Options for closing the gaps named in [comparison.md](comparison.md) and
for features beyond parity, all checked against the ADRs and the measured
upstream facts in [ogd.md](ogd.md) on 2026-08-28. The MeteoSwiss roadmap
was re-read the same day: the OGC Feature API for the local forecast is
confirmed as a **beta for the end of 2026**; weather warnings are still
not on the roadmap.

**How to use this file:** tick the boxes you want. Each ticked option
becomes a self-contained GitHub issue in the style of the existing
backlog (#3), with the ADR work included where the table says so.
Un-ticked options stay here as a record of what was considered.

Two gaps are not closeable by a feature and are listed at the bottom as
deliberate non-goals: warnings (not in the open data, breaking ADR-0001
is the only way) and maturity (only time and installs fix that).

Effort: **S** ≲ half a day · **M** ≈ 1–3 days · **L** = more, or new
infrastructure.

## A. Closing the named disadvantages

### Warnings

- [ ] **A1 — Config-flow hint to `meteoalarm`** when it is not set up,
  the exact pattern of the radar hint from #42. Effort S, no ADR.
- [ ] **A2 — "Complete setup" recipe in the README**: this integration +
  core `meteoalarm` + MeteoSwiss Radar, presented as the bundle that
  matches the app-API integrations feature for feature (minus pollen).
  Effort S, no ADR.
- [ ] ~~**A3 — Own warning entities via the EUMETNET CAP feed.**~~
  Official and documented, but a second upstream host (reopen ADR-0001)
  and redundant to core `meteoalarm`. **Recommended: no.**

### Hourly forecast traffic (~1 GB/day)

- [x] **A4 — OGC Features backend** once the announced beta ships (end
  of 2026). The seam (`ForecastBackend`, `_backend_factory`) and the
  step-by-step guide in ogd.md already exist; this eliminates the
  disadvantage rather than mitigating it. Effort M; supersedes ADR-0002.
  *Selected 2026-08-28 — prepare now, build later:* the API does not
  exist yet, so today's deliverable is a tracking issue (blocked
  upstream) that pins the plan; implementation starts the day the beta
  is reachable.
- [x] **A5 — Hourly horizon option via HTTP Range.** The bulk files are
  sorted by `Date` first, so the earliest hours of *all* points sit at
  the start of the file (~150 KB per hour block); the server honours
  `Range` (HTTP 206, verified). ogd.md only rules out Range *per
  point*, not per horizon. Needs live verification first. Effort M;
  revises ADR-0002 (traffic down, fetch semantics change).
  *Selected 2026-08-28, with this shape:* the horizon is a user option
  in the options flow, counted in **full local calendar days** — the
  same day boundary the daily `p`-variants and the app use. Default:
  **the rest of today plus the next two full days**, i.e. 49–72 h
  depending on the time of day. Choices: 0–8 days ahead, plus "full
  run" (all 220 h, today's behaviour). At the default, the four
  parameter files cost ~29–43 MB per refresh instead of ~125 MB —
  roughly **300 MB/day instead of 1 GB**.
  *Measured 2026-08-28 (issue #50):* only 6 of the 16 hourly files are
  Date-major; the other 10 — symbol, precipitation, wind, gusts,
  direction, zero-degree level, radiation, sunshine — are
  **point-major**, so one point's ~220 rows are a contiguous ~5 KB block
  that a single Range request fetches. Only `tre200h0` needs the
  horizon; the budget drops to **~7–11 MB per refresh**.
- [ ] ~~**A6 — Community proxy** that splits the bulk files per point.~~
  Would zero the per-user traffic but makes the proxy the upstream and
  someone the operator. **Recommended: no.**

### Pollen

- [x] **A7 — Pollen sensor platform** from `ch.meteoschweiz.ogd-pollen`:
  same host, small CSVs, nearest pollen station chosen like the weather
  station. Closes the last feature gap to `izacus`. Effort M; new
  dataset → new ADR (and revisits the "out of scope" line in ogd.md).
  *Selected 2026-08-28.* The collection is the same shape as `ogd-smn`
  (three meta CSVs, 16 automatic stations, hourly/daily resolutions);
  first step is a measured `ogd.md` section, then the ADR, then the
  platform. Selecting A7 strikes A8.
- [ ] ~~**A8 — Point to `frimtec/hass-swiss-pollen` instead** (README
  and/or config-flow hint), keeping pollen out of scope.~~ Off the
  table since A7 was selected on 2026-08-28.

### Station handling

- [x] **A9 — Reconfigure flow** (`async_step_reconfigure`): change the
  station and the forecast point without deleting the entry. Resolves
  the documented "delete and re-add" answer in CONFIGURATION.md. Effort
  S–M; config surface change, run it past documentation-guardian.
  *Selected 2026-08-28, combined with B12:* a station change asks what
  to do with the history recorded so far — **keep** (default; the
  switch is logged and the dialog says the old values came from the
  previous station), **discard** (purge states and statistics for a
  clean start), or **backfill** (rewrite the long-term statistics from
  the new station's official historical files, powered by B12).
  Keep/discard ship first; the backfill choice lights up when B12
  lands. Backfill covers long-term statistics only, not the raw
  short-term states — that limit is stated in the dialog.
- [x] **A10 — Only create sensors the station actually measures**, from
  `ogd-smn_meta_datainventory.csv`, instead of showing `unknown`.
  Effort S, no ADR. *Selected 2026-08-28.*
- [x] **A11 — Separate precipitation station** from
  `ch.meteoschweiz.ogd-smn-precip` (the dense rain-only network),
  optional second pick in the flow. Restores the one setup feature
  Rudd-O has over this integration. Effort M; new dataset → new ADR.
  *Selected 2026-08-28.* Builds on the surface A9 creates (the second
  pick lives in the same setup/reconfigure steps) and on A10's
  inventory filter.

### Reach and maturity

- [ ] **A12 — HACS default store submission** — tracked as #19, listed
  here for completeness. Human task.
- [ ] **A13 — `home-assistant/brands` PR** so the icon shows without the
  local brand folder. Effort S. Human task (PR review latency).
- [ ] **A14 — Declare `quality_scale` in the manifest** and work the
  bronze checklist (Rudd-O ships `silver`). Effort M.
- [ ] **A15 — Side-by-side validation against Rudd-O** on the production
  instance before the Blinzern migration (already a human task in #3);
  publishing the comparison builds trust. Effort S.

## B. Features beyond parity

### Free — the data is already downloaded

The station `_t_now.csv` carries 33 columns; the integration uses ~11.
Each of these is a new sensor from bytes already fetched (zero traffic),
effort S each, no ADR. *All of B1–B6 selected 2026-08-28.*

- [x] **B1 — Snow depth** (`htoauts0`) — a differentiator for mountain
  stations; no alternative integration has it.
- [x] **B2 — Wind chill** (`xchills0`) and **QNH** (`pp0qnhs0`).
- [x] **B3 — Soil temperatures** 5/10/20 cm (`tso005s0`, `tso010s0`,
  `tso020s0`) — garden and frost automations.
- [x] **B4 — 5 cm air temperature** (`tre005s0`) — ground-frost warning
  material.
- [x] **B5 — Diffuse and long-wave radiation** (`ods000z0`, `oli000z0`)
  — for the PV crowd, next to the existing global radiation.
- [x] **B6 — "Today" sensors** derived from the already-fetched daily
  forecast: `temp_max_today`, `precipitation_today` as plain sensor
  entities for dashboards and automations.

### Forecast additions — each costs ~+30 MB per hourly fetch today

Every extra hourly parameter is another whole-of-Switzerland file, so
each of these revisits ADR-0002 — **unless it lands after A4**, when the
point API makes them nearly free.
*All of B7–B11 selected 2026-08-28, with this shape:* they sit behind
the existing hourly opt-in, and each parameter file is fetched only
when its entity or attribute is enabled — the pattern ADR-0002 already
uses for gusts and direction — so the cost of the plain opt-in does not
change. One ADR-0002 revision covers the whole set with measured
numbers (up to 8 more files; ~7–11 MB each at the A5 default horizon,
~30 MB each at full run). Once A4 ships they become nearly free.

- [x] **B7 — Precipitation probability** (`rp0003i0`) → the HA forecast
  field `precipitation_probability`; the most visible missing forecast
  field.
- [x] **B8 — Zero-degree level** (`zprfr0hs`) — a snow-line sensor;
  much asked for in Switzerland.
- [x] **B9 — Cloud coverage** (`nprohihs`/`npromths`/`nprolohs`) → the
  HA `cloud_coverage` forecast field.
- [x] **B10 — Radiation forecast** (`gre000h0`) — feedstock for PV
  production forecasts.
- [x] **B11 — Uncertainty band** (`treq10h0`/`treq90h0` percentiles) as
  forecast attributes.

### New datasets and platforms

- [x] **B12 — Statistics backfill**: import the station `_h_recent` /
  `_historical` files into HA long-term statistics via a service call —
  decades of climate data; none of the alternatives has anything like
  it. Effort L; new files from an existing dataset → ADR.
  *Selected 2026-08-28.* Doubles as the machinery behind the
  "backfill" choice in A9's station change.

### UX and polish

- [x] **B13 — Translations de/fr/it** next to `en.json` — near-mandatory
  for a Swiss integration, pure diligence. Effort S–M.
  *Selected 2026-08-28.*
- [x] **B14 — Tiered hourly-forecast refresh** (reshaped and selected
  2026-08-28; originally "configurable station poll interval").
  Keep the near term fresh, the far days cheap: the hours until local
  midnight are re-fetched on **every new run** — that is hourly;
  upstream publishes no faster, and the 10-minute cadence exists only
  for the station measurements, which already refresh every 10 min —
  while the following days stay on the 3 h throttle (or stretch to
  6 h). Builds directly on A5's Range machinery: the "today" slice is
  just the first ~0.2–3.6 MB of each parameter file. Daily volume
  stays in the region of A5 alone, but the near-term forecast is at
  most one run old instead of up to three. Effort S–M on top of A5;
  folded into the same ADR-0002 revision.
- [x] **B15 — Mountain forecast points selectable** (added and
  selected 2026-08-28). The point list carries 631 mountain points of
  interest (type 3: summits, passes, resorts) with name and height, and
  every forecast file has their rows — but the config flow is
  postal-code only. A mode choice in the first step plus a searchable
  dropdown of mountain points makes a ski-area entry possible: forecast
  for the summit, current conditions and snow depth (B1) from the
  nearest mountain station. Effort S–M; config surface change, no ADR
  expected.
- [x] **B16 — Daily wind derived from the hourly forecast** (added and
  selected 2026-08-28). The dataset has no daily wind parameter; with
  the hourly opt-in on, the daily entries get the day's maximum wind
  speed, maximum gust and the direction at that hour, aggregated per
  local calendar day. Days beyond the hourly horizon stay empty rather
  than guessed. Effort S; no new data, no option, no ADR.

## Deliberate non-goals

- **Warning entities from the app API or scraped pages** — ADR-0001.
- **Nowcast / INCA** — radar-integration territory, ADR-0003.
- **A3 and A6 above** — named so they are rejected on record, not
  forgotten.

## Issues created 2026-08-28

Autopilot order is P1 → P2 → P3, lowest number first, one PR at a
time; #57 carries no P-label and a `Tracking:` title, so the bots skip
it until the upstream beta exists.

| option | issue | priority | model |
|---|---|---|---|
| A10 | #46 | P1 | sonnet |
| B1–B5 | #47 | P1 | sonnet |
| B6 | #48 | P1 | sonnet |
| B13 | #49 | P1 | sonnet |
| A5 | #50 | P1 | opus |
| B12 | #51 | P2 | opus |
| A9 | #52 | P2 | opus |
| A7 | #53 | P2 | opus |
| B14 | #54 | P2 | opus |
| B7–B11 | #55 | P3 | opus |
| A11 | #56 | P3 | opus |
| B15 | #59 | P2 | sonnet |
| B16 | #60 | P2 | sonnet |
| A4 | #57 | — (tracking) | — |

## Suggested sequencing (opinion, not binding)

1. **Selected (2026-08-28):** A5 — build now, with the day-based
   horizon option described above. A4 — prepare now (tracking issue),
   implement when the beta ships. A7 — pollen platform, with its ADR.
   A10 — inventory filter, first (smallest, standalone). A9 + B12 —
   reconfigure flow whose station change offers keep / backfill /
   discard; keep/discard ship first, backfill lights up with B12.
   A11 — precipitation station, on top of A9's surface. B1–B6 and
   B13 — small and independent, any time. B7–B11 — behind the hourly
   opt-in with per-entity gating, one ADR-0002 revision for the set,
   after A5; nearly free once A4 ships. B14 — tiered refresh, on top
   of A5, same ADR-0002 revision. B15 — mountain points in the config
   flow, after the P1 basics. B16 — daily wind from the hourly data,
   any time after the hourly path exists.
2. **Still open:** A1, A2, A14.
3. **Alongside (human):** A12, A13, A15.
