# ADR-0006: An optional second station from the precipitation-only network

- **Status:** Accepted
- **Date:** 2026-08-28

## Context

Rain is hyper-local, and the full SwissMetNet (`ch.meteoschweiz.ogd-smn`,
~160 stations) is sparse: from Köniz the chosen full station is BER, while
the rain-only network has gauges at Belp (7 km), Laupen (13 km) and Kiesen
(17 km). `Rudd-O/homeassistant-meteoswiss` offers a separate precipitation
station for exactly this reason; it was the one setup feature it had over
this integration (`docs/comparison.md`). Selected on 2026-08-28
(`docs/feature-options.md`, A11; issue #56).

Measured the same day (`docs/ogd.md`, A2): `ch.meteoschweiz.ogd-smn-precip`
— 141 automatic precipitation stations, meta CSVs in the A1 shape, per
station a `_t_now.csv` of ~1.2 KB with `rre150z0` (10-minute sums), about
15 minutes of lag. Same host, same licence (ADR-0001 untouched).

## Decision

- A config entry may carry an **optional second station** from
  `ch.meteoschweiz.ogd-smn-precip` (`CONF_PRECIP_STATION_ABBR`). The setup
  flow and the reconfigure flow (#52) offer the three nearest precipitation
  stations; **none is selected by default** — the feature is opt-in.
- When set, the **precipitation sensor and the weather entity's current
  precipitation** come from the precipitation station; every other value
  stays with the full station. The sensor's device info and attribution
  name the station it reads.
- The client reuses the A1 station machinery **parameterised by
  collection** (metadata files, nearest selection, `_t_now` parsing) rather
  than duplicating it; `ogd/` stays pure Python.
- Cadence: the precipitation station is polled like the main station,
  **every 10 minutes, conditional**, only when the option is set.

## Consequences

- Traffic: one ~1.2 KB file per 10 minutes when set — inside the station
  budget of ADR-0002, which is unchanged.
- The second pick lives in the surface the reconfigure flow creates (#52),
  and the inventory filter (#46) applies to it.
- Historical backfill (ADR-0007) covers the main station first; the
  precipitation station's `_h_*` files exist in the same shape and can
  follow.
- Implementation: #56 (client) and its follow-up (setup/reconfigure +
  sourcing).
