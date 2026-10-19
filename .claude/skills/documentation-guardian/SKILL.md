---
name: documentation-guardian
description: Proactively suggests an ADR when an architectural change happens in the MeteoSwiss Weather repo, and checks planned changes against existing decisions in docs/adr/. Activate on a new upstream endpoint, dataset or dependency, a change to the fetch cadence or traffic constants, a new platform or config option, a new quality gate or CI job, the same logic in a second place, or a deviation from an accepted ADR.
---

# Documentation Guardian

Keeps `docs/adr/` in sync with the code. Two jobs: detect ADR-worthy
changes as they happen and propose drafting one, and check in-progress
changes against existing decisions before they land.

ADR conventions live in [`docs/adr/README.md`](../../../docs/adr/README.md).
When the two disagree, that file wins.

## Activation triggers

- A **new upstream endpoint, collection or file family** fetched by the
  client (`custom_components/meteoswiss_weather/ogd/`) — governed by
  ADR-0001; a non-OGD host is a deviation, not an extension. A new SLF
  endpoint is a deviation from ADR-0009.
- A **new runtime dependency** (`requirements` in `manifest.json`).
- A change to **fetch cadence or traffic constants** (refresh intervals,
  the set of hourly parameter files, conditional-request handling) —
  governed by ADR-0002.
- Anything that **couples this repo to the radar integration** (shared
  code, a dependency, radar features here) — governed by ADR-0003.
- A **new quality gate**: a CI job under `.github/workflows/`, a ruff rule
  promoted, a coverage floor, a new required check — ADR-0004.
- A **new platform** (`weather`, `sensor`, ...) or a **new config/option
  key** (public API, hard to remove).
- A **pattern deviation** from an accepted ADR.
- The **same logic in a second language or place** (a table in code and
  in docs, a parser duplicated between `ogd/` and the smoke test):
  propose one written spec plus a shared golden-fixture test both sides
  run.

## Skip

- Bug fixes that keep the contract (no new option, boundary, or gate).
- Refactors inside a module with an identical public surface.
- Added test coverage, lint-warning cleanup, prose/style tweaks.
- User-facing YAML/dashboard examples in docs (usage, not architecture).

## Workflow

1. When a trigger fires, name the decision and which existing ADR it
   touches (or that it is new).
2. If it deviates from an accepted ADR, say so and stop for the user.
3. If it is a genuinely new decision, offer to draft an ADR from
   `docs/adr/template.md` with the next free number, and land it in the
   same PR as the code. If it changes an accepted ADR, propose a dated
   `## Amendment YYYY-MM-DD (#issue)` section instead of editing the
   decision text (`docs/adr/README.md`, *Changing an accepted ADR*).
4. Keep proposals rare and load-bearing: an ADR per bug fix is noise.
