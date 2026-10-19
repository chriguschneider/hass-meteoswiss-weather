# Architecture Decision Records

Short, dated records of decisions that shape this repo: why only the
official open data is used, why the local forecast is fetched the way it
is, why this is not part of the radar integration. They exist so a later
change (human or AI) does not silently undo a deliberate call.

## When to write one

Write an ADR when a change:

- adds a **new upstream endpoint, dataset or host**, or changes how the
  client reaches the open data,
- changes the **fetch cadence, the set of files fetched, or any traffic
  budget** constant,
- adds a **platform** or a **public config/option key**,
- adds or moves a **quality gate** (a CI job, a lint rule, a coverage
  floor),
- introduces a **new architectural pattern** or breaks a module boundary,
- **deviates from an existing accepted ADR**.

Skip it for bug fixes that keep the contract, refactors inside a module,
added test coverage, and prose/style tweaks.

## How

1. Copy [`template.md`](template.md) to `NNNN-short-slug.md` (next free
   number).
2. Fill in Context, Decision, Consequences. Keep it to a screen.
3. Land it in the same PR as the code it describes, and add it to the
   index below.

## Changing an accepted ADR

Do not rewrite it. Append a section and keep the original text:

```
## Amendment YYYY-MM-DD (#issue)

What changed and why. Decided by: <owner | ADR-NNNN>.
```

A decision that reverses the ADR gets a new ADR, and the old one's status
becomes `Superseded by ADR-NNNN`. (Older ADRs here use `## Revision N` or
`## Decided by the owner` headings for the same thing; they stay as they
are.)

## Index

- [0001 — The official MeteoSwiss open data is the only upstream](0001-official-open-data-only-upstream.md)
- [0002 — Traffic budget for the bulk local-forecast files](0002-traffic-budget-bulk-local-forecast.md)
- [0003 — A sibling of the radar integration, not a merge](0003-sibling-of-the-radar-integration.md)
- [0004 — Quality gates and release process inherited from the radar repo](0004-quality-gates-inherited-from-radar.md)
- [0005 — The pollen dataset is in scope, as an opt-in on the existing entry](0005-pollen-dataset-in-scope.md)
- [0006 — An optional second station from the precipitation-only network](0006-optional-precipitation-station.md)
- [0007 — Station history is imported into long-term statistics on request only](0007-station-history-backfill.md)
- [0008 — One run-scoped forecast store, filled by an escalating fetch](0008-run-scoped-forecast-store.md)
- [0009 — SLF IMIS snow stations as a second official upstream](0009-slf-imis-second-upstream.md)
- [0010 — Sunshine duration rides the daily point blocks](0010-sunshine-duration-daily-block.md)
- [0011 — A snow station is its own config entry](0011-snow-station-own-entry.md)
