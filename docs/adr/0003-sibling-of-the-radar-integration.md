# ADR-0003: A sibling of the radar integration, not a merge

- **Status:** Accepted
- **Date:** 2026-08-26

## Context

The same maintainer ships
[`hass-meteoswiss-radar`](https://github.com/chriguschneider/hass-meteoswiss-radar),
which was initially an entity-less integration that proxied the MeteoSwiss
website's radar products to a Lovelace card. (Note: as of 2026-09-30, the radar
integration grew local rain-nowcast sensor and binary-sensor entities via
[PR #190](https://github.com/chriguschneider/hass-meteoswiss-radar/pull/190).)
Folding a weather entity into that repository was considered and rejected:

- HACS treats one repository as one integration; the radar's default-store
  submission was in flight when this repo was created and a scope change
  would have disturbed it. (This constraint no longer applies — the radar
  integration is in the HACS default store as of 2026-08-31.)
- The two have different upstreams (website product JSON vs. the open
  data platform, ADR-0001) with different failure modes and cadences.
- Users who want only the map should not get a weather entity, and vice
  versa.
- The domain `meteoswiss_radar` would no longer describe its contents;
  renaming a domain breaks every existing installation.

## Decision

- This repository is a **separate integration** with its own domain
  `meteoswiss_weather` and no code dependency on the radar integration in
  either direction. No radar features (map, frames, INCA nowcast layers)
  live here.
- The two are presented as one **family**: the same brand icon set
  (`brand/`, generated from the official MeteoSwiss app icon by the same
  script and decision as the radar repo, see `docs/brands-icon.md`), the
  naming pattern "MeteoSwiss Radar" / "MeteoSwiss Weather", and reciprocal
  links in both READMEs.
- Integration points that add value are built **where the data already
  is**: a "rain expected at home" entity belongs in the radar repo, which
  already fetches the nowcast frames; a forecast strip on the radar card
  reads a `weather` entity from this integration through the normal HA
  state machine, never through a private API between the two.
- The domain `meteoswiss` is deliberately **not** used: it is taken by two
  existing custom integrations, and a new one must be installable next to
  them while users migrate.

## Consequences

- Two repositories to maintain, with shared conventions kept in sync by
  hand (AGENTS.md skeleton, workflows, ADR-0004).
- Cross-promotion is limited to documentation and, at most, a config-flow
  hint that the other integration exists.
- The Decision's entity-placement rule has been applied as written: the
  rain-nowcast entities that belong in the radar repo are now shipped there
  (hass-meteoswiss-radar PR #190, 2026-09-04), and integrate with this
  integration's weather entity through the normal HA state machine. The
  revisit trigger ("if the radar integration itself grows entities") is
  therefore discharged. The coupling rule—a radar-side entity reading this
  integration's `weather` entity through the HA state machine, never through
  a private API—remains unchanged and is the enduring integration point.
