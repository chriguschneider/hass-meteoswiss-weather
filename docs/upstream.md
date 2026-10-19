# Upstream facts

Measured facts about every external system this repo talks to: endpoints,
file formats, units, update cadence, limits, quirks. Agents read these
before they touch anything that depends on an upstream, and every lookup
table or constant in the code cites a line in the files below, a URL or a
fixture cut from a real file.

This repo kept its facts where they already were; this page is the index.

- **[`ogd.md`](ogd.md)** — the MeteoSwiss open data on
  `data.geo.admin.ch` (ADR-0001): STAC catalogue, file families, CSV
  layout and encoding, point ids, run cadence, the measured sizes behind
  the traffic budget (ADR-0002). The authoritative documentation is
  https://opendatadocs.meteoswiss.ch; when it and `ogd.md` disagree, the
  docs win and `ogd.md` gets a PR.
- **[`slf.md`](slf.md)** — the SLF IMIS API on `measurement-api.slf.ch`
  (ADR-0009): the three endpoints used, fields, cadence, licence and
  attribution.
- **[`symbols.md`](symbols.md)** — the MeteoSwiss forecast symbols and how
  they map to Home Assistant conditions.
- **`tests/fixtures/`** — trimmed real files; `tests/README.md` says how
  they are cut and refreshed.
