# ADR-0007: Station history is imported into long-term statistics on request only

- **Status:** Accepted
- **Date:** 2026-08-28

## Context

Every SwissMetNet station publishes its hourly history next to the live
file (`docs/ogd.md`, A1 "History files"): `_h_recent.csv` for the current
year (BER: 829 KB, 5,736 rows, refreshed daily at ~02:15 UTC) and one
`_h_historical_<decade>.csv` per decade back to the 1980s (8–13 MB each,
refreshed yearly). The hourly columns include the temperature **mean, min
and max** (`tre200h0`/`hn`/`hx`) — the exact shape of a Home Assistant
long-term statistics row. No alternative integration offers this.

The maintainer selected the backfill on 2026-08-28 (`docs/feature-options.md`,
B12; issue #51), and combined it with the reconfigure flow (A9, #52): on a
station change the user chooses to keep, discard or **backfill** the
recorded history.

## Decision

- History is fetched **only on an explicit user action**: the service
  `meteoswiss_weather.import_history` (config entry, optional start/end)
  and the "backfill" choice of the reconfigure flow. **Nothing polls the
  history files**; they are not part of any coordinator.
- The files read are `_h_recent.csv` and the `_h_historical_<decade>.csv`
  files that the requested range needs, one at a time, parsed in the
  executor, streaming — never all decades in memory at once.
- The import targets Home Assistant's **long-term statistics** through the
  recorder's import API, under the statistic ids of the integration's own
  sensor entities: hourly mean/min/max for temperature, mean for the other
  continuous quantities, hourly **sum** for precipitation. Overlapping
  periods are **replaced, not duplicated**. The raw short-term states are
  not touched — the service description and `docs/CONFIGURATION.md` say so
  plainly.
- The import code lives in `ogd/history.py` (pure Python parsing) plus a
  thin integration layer; the reconfigure flow's backfill calls the same
  layer as the service.

## Consequences

- Traffic is one-off and user-visible: the current year ≈ 1 MB, a decade
  ≈ 8–13 MB, everything since 1980 ≈ 45 MB per station. This is outside the
  recurring ADR-0002 budget by design; the service documentation states the
  sizes.
- A station change with "backfill" produces a statistics series from one
  location only; "keep" leaves the seam and logs it; "discard" clears — the
  user decides, the flow says what each choice does.
- The precipitation station (ADR-0006) has the same file shape and can be
  backfilled by the same code later.
- Implementation: #51 (parser) and its follow-up (service + recorder
  import); the reconfigure choice is #52.
