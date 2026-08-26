# ADR-0001: The official MeteoSwiss open data is the only upstream

- **Status:** Accepted
- **Date:** 2026-08-26

## Context

Every MeteoSwiss integration for Home Assistant with a user base
(`Rudd-O/homeassistant-meteoswiss`, `izacus/hass-swissweather`) reads its
forecast from the undocumented backend of the MeteoSwiss mobile app
(`app-prod-ws.meteoswiss-app.ch`) and its current conditions from legacy
CSV drops or scraped website pages. That backend is private: MeteoSwiss
has never documented, endorsed or versioned it, and the community thread
about the resulting breakages runs to more than a thousand posts.

Since 22 May 2025 MeteoSwiss publishes its data under the Open Government
Data programme (opendatadocs.meteoswiss.ch): licence CC BY 4.0, no API
key, files on `data.geo.admin.ch` discoverable through a STAC catalogue,
changes announced through a public changelog. Since September 2025 that
includes the local forecast the app itself shows, per postal code
(collection `ch.meteoschweiz.ogd-local-forecasting`), next to the 10-minute
SwissMetNet station measurements (`ch.meteoschweiz.ogd-smn`).

The one thing the open data does not carry, and does not promise before
2027, is weather warnings.

## Decision

The integration reads **only** the official open data:

- Every HTTP request goes to `OGD_FILE_BASE` / `OGD_STAC_BASE`
  (`https://data.geo.admin.ch`). No request to `meteoswiss-app.ch`,
  `meteoschweiz.admin.ch`, `meteosuisse.admin.ch` or any other host. No
  HTML scraping.
- The client lives in `custom_components/meteoswiss_weather/ogd/` and
  imports nothing from Home Assistant, so it can move to PyPI unchanged
  once the interface has settled.
- Weather warnings are out of scope. The README points users to Home
  Assistant's core `meteoalarm` integration, which carries the MeteoSwiss
  CAP feed at region level.
- Every entity sets `attribution = "Source: MeteoSwiss"`, as the CC BY 4.0
  terms require.

## Consequences

- Breakage risk drops from "silent change of a private API" to "announced
  change in a public changelog". The weekly smoke test (ADR-0004) is the
  tripwire for the latter.
- Feature parity with the app-API integrations is deliberately incomplete:
  no warnings, and hourly data costs real traffic (ADR-0002) until the
  point API MeteoSwiss has announced for the end of 2026 exists.
- A feature that would need the app API is rejected, not hidden behind an
  option. Reopen this ADR rather than adding a second upstream.
