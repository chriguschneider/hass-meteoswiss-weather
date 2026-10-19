# ADR-0010: Sunshine duration rides the daily point blocks

- **Status:** Accepted
- **Date:** 2026-10-19

## Context

The owner wants the forecast sunshine duration in the forecast entries:
per hour and as a daily total, without having to switch on the hourly
forecast (ADR-0002 keeps that opt-in because of its traffic).

The local forecast carries `sre000h0` (minutes of sunshine per hour) and
**no daily sunshine parameter** (all 32 parameters of the run checked
2026-10-19). Measured the same day on run `202610190800`: ~30 MB,
point-major, 219 rows per point, so one point is a ~5 KB block. That is
the shape of the files the daily refresh already fetches as point blocks
(`DAILY_BLOCK_PARAMS`: wind, gust, direction, precipitation probability,
zero-degree level, ADR-0008), whatever the hourly option says.

Home Assistant's `Forecast` has no sunshine field. The integration
already adds custom keys to forecast entries (`zero_degree_level`,
`radiation`, `cloud_coverage_*`, `temperature_p10/p90`); Home Assistant
passes unknown keys through unchanged.

## Decision

- `sre000h0` joins **`DAILY_BLOCK_PARAMS`**: fetched for the configured
  point with every daily refresh, through the same fetch ladder and the
  same per-run cache, regardless of the hourly option. No option of its
  own.
- Its hourly series is filed in the forecast store (ADR-0008). Hourly
  forecast entries carry **`sunshine_duration`** (minutes in that hour)
  when the hourly option is on; it never gates whether an hour renders.
- Daily forecast entries carry **`sunshine_duration`** (minutes): the sum
  over the local calendar day, assigned to days the same way as the
  daily wind and probability aggregates, and **only for complete days**
  (23–25 hours across DST). A partial or missing day stays absent; no
  value is invented.
- The key has no `native_` prefix: Home Assistant does not unit-convert
  unknown keys.

## Consequences

- Traffic: one more ~5 KB block per changed run, at most ~120 KB a day,
  inside the budget of ADR-0002, which is otherwise unchanged. The
  options cost preview counts it.
- Whether `Date` marks the start or the end of the hour is not settled
  (docs/ogd.md, "Sunshine duration"); daily totals do not depend on it
  because night hours are 0.
- Cards that do not know the key ignore it; templates and custom cards
  can read it from `weather.get_forecasts`.
- Implementation: #178.
