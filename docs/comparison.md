# How this compares to the other MeteoSwiss integrations

Three HACS integrations bring MeteoSwiss data into Home Assistant. They
differ fundamentally in exactly one thing — where the data comes from.
Everything else follows from that.

- **this one** — `chriguschneider/hass-meteoswiss-weather` (`meteoswiss_weather`)
- **[`Rudd-O/homeassistant-meteoswiss`](https://github.com/Rudd-O/homeassistant-meteoswiss)** (`meteoswiss`) — the
  established one, forked from `websylv`
- **[`izacus/hass-swissweather`](https://github.com/izacus/hass-swissweather)** — the widest feature set

**Sourcing.** The facts about this integration come from its code, ADRs
and the measured upstream numbers in [`ogd.md`](ogd.md). The facts about
the other two come from their `README` and `manifest.json`, read on
2026-08-28 — not from their source. Where a row below says "not
documented", the feature may still exist.

## The split that everything follows from

| | This integration | Rudd-O, izacus |
|---|---|---|
| Upstream | `data.geo.admin.ch` (STAC catalogue) | `app-prod-ws.meteoswiss-app.ch`, `meteosuisse.admin.ch` |
| Status | Open Government Data since May 2025 | Undocumented backend of the MeteoSwiss mobile app |
| Licence | CC BY 4.0, attribution required | None stated |
| Change policy | Announced in a public changelog | None; the API can change with any app release |
| Local forecast | Per postal code since September 2025 | Per point, always |

The app backend is what the phone app talks to. MeteoSwiss has never
documented, endorsed or versioned it. The predecessor of Rudd-O's fork
was broken for over a year, and the community thread about it runs past
a thousand posts. That is the risk being traded away here — see
[ADR-0001](adr/0001-official-open-data-only-upstream.md).

## Feature matrix

| | `meteoswiss_weather` | Rudd-O `meteoswiss` | izacus `hass-swissweather` |
|---|---|---|---|
| Data source | official OGD only | app API + OGD files | app API |
| Breakage mode | announced change | silent API change | silent API change |
| Daily forecast | 9 days, official symbols | yes | 8 days |
| Hourly forecast | opt-in, ≥ 3 h apart, ~1 GB/day | yes, no traffic cost | yes, no traffic cost |
| Current conditions | SwissMetNet, 10 min, 11 sensors, optional separate precipitation station | yes, interval configurable, separate precipitation station | yes, station code entered by hand |
| Warnings | no — use core `meteoalarm` | not documented | yes, several warning entities |
| Pollen | no | no | yes |
| Radar | separate sibling integration (ADR-0003) | no | no |
| Python requirements | none | `hamsclientfork`, `geopy` | own libraries |
| Third-party services | none | Nominatim (OpenStreetMap) for the postal code | none known |
| Setup | 3 steps, pre-filled from the HA location | UI, coordinates + postal code | UI, station codes by hand |
| Default traffic | ~5 MB per new forecast run; 304s in between | negligible | negligible |
| Maturity | young, `0.2.1`, small install base | established, `quality_scale: silver` | established |

## What this integration gains

- **Structural durability.** Upstream changes are announced rather than
  rolled out silently, and the weekly smoke test against the real files
  is the tripwire (ADR-0004).
- **A licence you can point at.** CC BY 4.0, every entity carries
  `Source: MeteoSwiss` — which matters the moment a dashboard is public.
- **No dependencies, no third party.** `requirements: []`; nothing is
  installed alongside, and the user's postal code is not sent to
  Nominatim.
- **Polite polling.** Conditional GETs with `ETag` / `Last-Modified`, CSV
  parsing in the executor, station polled every 10 minutes and costing a
  single 304 when nothing changed.
- **Station choice with a suggestion.** The three nearest SwissMetNet
  stations are offered, nearest pre-selected, so elevation or exposure
  can be corrected for.
- **Decisions are written down.** The ADRs say why, and the forecast
  already runs behind a backend interface ready for the point API.

## What this integration gives up

- **No warnings.** Thunderstorm, hail and snow warnings are not in the
  open data and are not on the roadmap before 2027. Core
  [`meteoalarm`](https://www.home-assistant.io/integrations/meteoalarm/)
  carries the official CAP feed at region level and runs alongside.
- **The hourly forecast is expensive.** The local forecast is published
  as whole-of-Switzerland CSVs, 29–33 MB per parameter per run. Enabled,
  the option costs roughly 1 GB/day and is throttled to at most every
  3 hours ([ADR-0002](adr/0002-traffic-budget-bulk-local-forecast.md)).
- **No pollen, no nowcast.** Not planned.
- **The station cannot be changed after setup.** Delete and re-add the
  entry for now.
- **Not every station measures everything.** Sensors a station does not
  carry stay `unknown`.
- **Young.** Version 0.2.1, custom HACS repository, few installs, so
  correspondingly little field hardening.

## What the alternatives do better

- **They are more complete.** An hourly forecast at no traffic cost;
  izacus adds warnings and pollen on top — the full app picture.
- **Point queries.** The app API returns one location in a few kilobytes.
  No traffic dilemma, no throttle.
- **They are proven.** Rudd-O carries `quality_scale: silver` and years
  of bug reports, and lets the measurement interval be configured. Its
  symbol table is good enough that [`symbols.md`](symbols.md) credits it
  as the source of ours. (The separate precipitation station it offered is
  now matched here — [ADR-0006](adr/0006-optional-precipitation-station.md).)

And what they carry with that:

- **The source can vanish.** No contract, no announcement, no version.
- **A fork chain rather than an upstream.** `websylv` → `Rudd-O` → further
  forks: whoever ships the fix changes, and with them the repository the
  user has to have added.
- **Extra dependencies** are installed alongside, and postal-code lookup
  calls out to Nominatim.
- **No data licence** to rely on.
- **More manual work** in izacus: station and pollen station codes have
  to be looked up by hand.

## Which one to run

| If you want | Then |
|---|---|
| An entity that still works in a year | this integration — that is the entire point |
| An hourly graph on a dashboard | Rudd-O or izacus — or enable the option here and accept ~1 GB/day |
| Severe-weather automations | core `meteoalarm`, alongside any of the three |
| Pollen | izacus, the only one |
| Precipitation radar | [MeteoSwiss Radar](https://github.com/chriguschneider/hass-meteoswiss-radar), the sibling |
| To republish the data | this integration — the only one with a clean CC BY basis |
| The least ongoing maintenance | this integration for the entity, `meteoalarm` for warnings |

The app-API integrations bundle everything into one package, and thereby
into one failure mode. Split up, the same coverage is: this integration
for the entity and station sensors, `meteoalarm` for warnings, MeteoSwiss
Radar for radar. What remains missing is pollen.

## What changes at the end of 2026

MeteoSwiss has announced an OGC Features API with per-point local-forecast
queries as a beta for the end of 2026. That removes the largest drawback
above: the hourly forecast would cost kilobytes instead of megabytes, the
3-hour throttle becomes unnecessary, and the reason to reach for an
app-API integration shrinks to warnings and pollen.

The swap is prepared — the forecast already goes through a backend
interface that a point-API backend can implement without touching the
coordinator or the entities (ADR-0002).

---

*The options for closing these gaps are collected as a pick list in
[feature-options.md](feature-options.md).*
