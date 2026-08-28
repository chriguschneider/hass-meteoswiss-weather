# ADR-0005: The pollen dataset is in scope, as an opt-in on the existing entry

- **Status:** Accepted
- **Date:** 2026-08-28

## Context

Pollen was the one feature gap to the app-API integrations that the open
data can actually close (`docs/comparison.md`): `izacus/hass-swissweather`
ships pollen, this integration deliberately left it out, and `docs/ogd.md`
said "out of scope for now". The maintainer selected it on 2026-08-28
(`docs/feature-options.md`, A7; issue #53).

Measured the same day (`docs/ogd.md`, Pollen): `ch.meteoschweiz.ogd-pollen`
on `data.geo.admin.ch` — 15 automatic stations with coordinates, 7 taxa
(alder, birch, hazel, beech, ash, oak, grasses) × 4 granularities (hourly
mean, two daily means, annual integral), unit grains/m³. The per-station
`_h_now.csv` is ~400 bytes, refreshed hourly with about one hour of lag.
ADR-0001 is untouched: same host, same licence, same STAC catalogue.

## Decision

- The pollen dataset is **in scope**. The client gains `ogd/pollen.py`
  (pure Python, aiohttp/stdlib only, like the rest of `ogd/`): station
  metadata, nearest-station selection reusing `ogd/geo.py`, and the
  current hourly concentrations per taxon from `_h_now.csv`.
- Pollen is an **opt-in option on the existing config entry**
  (`CONF_POLLEN` in the options flow), not a second config flow or a
  sibling integration. The nearest pollen station to the forecast point
  is pre-selected and can be overridden.
- **Cadence matches the data:** at most one request per hour per
  station, conditional (`If-None-Match`), parsed in the executor. There
  is no faster upstream than hourly, so nothing polls faster.
- Sensor surface: one sensor per taxon carrying the **hourly mean**
  (`…h0`), unit `grains/m³`, attribution `Source: MeteoSwiss`. Grasses
  and birch are enabled by default, the other taxa disabled — the
  station-sensor pattern. Only taxa the station measures are created
  (`_meta_datainventory.csv`).
- The daily means (`d0`/`d1`) and the annual integral are **not** exposed
  in the first slice; adding them later is a sensor change, not a new
  decision.

## Consequences

- Traffic: one ~400-byte file per hour when the option is on — no
  ADR-0002 impact.
- `docs/ogd.md`'s "out of scope" line and `docs/comparison.md`'s pollen
  row change; the A8 option (pointing users to a third-party pollen
  integration) is off the table.
- A pollen **forecast** is not in the open data; this ADR covers
  measurements only. Revisit when MeteoSwiss publishes one.
- Implementation: #53 (client) and its follow-up (option + platform).
