# AGENTS.md

Conventions for AI-assisted contributions to this repo (Claude Code,
Cursor, Codex, Aider, or any other assistant). Read this first if you are
an AI assistant working on a branch, or a human driving one.

---

## Shared Skeleton

These conventions apply across all of chriguschneider's repos and come from
[`repo-standard`](https://github.com/chriguschneider/repo-standard). Repo-specific sections follow below.

### Commit attribution

Commits made with AI assistance carry a `Co-Authored-By:` trailer that
names the tool and model honestly, e.g.:

```
Co-Authored-By: Claude <noreply@anthropic.com>
Co-Authored-By: Codex <noreply@openai.com>
```

The exact string is not load-bearing; what matters is that git history
reflects what did the typing.

### Language

English only in code, comments, commit messages, and these repo docs.
Non-English is fine in chat, but never gets persisted to a file or a
commit.

### Comment discipline

Inline comments earn their place by explaining the *why* (a hidden
constraint, a subtle invariant, a workaround), not by restating the
code. The PR description is the place for context that does not survive
the diff.

### Model selection

Route the model to the difficulty, not the other way round:

- **Haiku** — trivial mechanical: typos, docs, a config default, a bumped
  constant, value validation.
- **Sonnet** — the default: normal bug fixes, tests, CI, well-scoped
  features.
- **Opus** — architectural surface: lifecycle/teardown, concurrency,
  hardening, ambiguous root-cause hunting, anything ADR-worthy.

### Parallel work

- **Branch naming**: `<tool-or-initials>/<issue>-<slug>`, e.g.
  `claude/4-station-client`.
- **Issue claiming**: `gh issue edit <N> --add-assignee @me` before
  starting, so parallel contributors (and the autopilot) see it is taken.
- **Worktrees**: parallelize along module boundaries, one worktree each:
  `git worktree add ../msw-<issue> <branch>`. One file, one lane.

### Draft PRs

CI runs on every push. If you iterate with several pushes, open the PR as
a **draft** until you expect CI to pass, then mark ready. Direct push to
`master` is not the flow; open a PR.

### Changelog entries

Every PR that changes user-visible behavior adds an entry to
`CHANGELOG.md` under `## [Unreleased]` in the right subsection
(Added/Changed/Deprecated/Removed/Fixed/Security, per
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/)).

**Exemption:** a fix for a feature that never appeared in a released
version needs no entry — it has no user-visible before-state.

Write entries for users: would they click *Update* because of this line?
HACS and release pages show the section as is. Internal work (refactors,
CI, tests) goes in a short *Under the hood* list at the end of the section,
or nowhere.

The release commit (`chore(release): vX.Y.Z`) renames `## [Unreleased]`
to `## [vX.Y.Z] — YYYY-MM-DD`; the release workflow extracts that section
verbatim as the GitHub release notes.

### Generated files and data

- A committed build output (bundle, generated table, PDF) has a CI check
  that it matches its sources, and is regenerated in the same commit as
  the source change. On a rebase conflict, regenerate; never hand-merge it.
- Test fixtures are trimmed real files (keep the real header, encoding and
  a handful of rows), never hand-written. `tests/README.md` says where each
  one was cut from.
- Domain data (lookup tables, codes, thresholds) cites its source:
  `docs/upstream.md`, a URL or a fixture. Nothing is invented.

### Architectural decisions

If a change adds an upstream endpoint or dependency, adds a platform or a
public option, adds a quality gate, or deviates from an existing ADR, it
needs an ADR. Triggers and the template are in
[`docs/adr/README.md`](docs/adr/README.md). Land the ADR with the code.
An accepted ADR is not rewritten: append a dated amendment, or supersede
it with a new one (`docs/adr/README.md`, *Changing an accepted ADR*).

Claude Code users get automatic prompts via the repo-local skills in
`.claude/skills/`:

- **`commit-guardian`** — before any `git commit`, checks the staged diff
  against accepted ADRs and these conventions. Non-blocking.
- **`documentation-guardian`** — proposes an ADR when an architectural
  change happens and flags changes that contradict an existing decision.

### Automation

Opt-in, draft-PR-only (see [`docs/agent-automation.md`](docs/agent-automation.md)):
add the **`agent:go`** label to hand an issue to Claude, or write
**@claude** in a comment (owner/collaborators only). The model comes from
the issue's labels via `.github/scripts/pick-model.sh`. Unattended runs
use a tool allowlist, cannot write to `.github/` or `.claude/` (path guard
hook), and only act on issue text from people with write access.

---

## Repo-Specific: MeteoSwiss Weather

### What this is

A Home Assistant integration that reads the **official MeteoSwiss open
data** (opendatadocs.meteoswiss.ch) and exposes a `weather` entity plus
station sensors. It is the sibling of
[`hass-meteoswiss-radar`](https://github.com/chriguschneider/hass-meteoswiss-radar)
(ADR-0003) and deliberately **not** a fork of any app-API integration.

### Repo structure

```
custom_components/meteoswiss_weather/
├── __init__.py          # entry setup/unload, platforms, services
├── config_flow.py       # UI setup, reconfigure + options
├── const.py             # DOMAIN, VERSION, OGD constants, intervals
├── coordinator.py       # DataUpdateCoordinators: station, forecast, pollen, SLF
├── store.py             # run-scoped forecast store (ADR-0008)
├── demand.py            # forecast demand registry (ADR-0008)
├── history.py           # station history import into statistics (ADR-0007)
├── weather.py           # WeatherEntity (current + daily [+ hourly opt-in])
├── sensor.py            # station, pollen, snow and traffic sensors
├── symbols.py           # MeteoSwiss symbol -> HA condition (docs/symbols.md)
├── diagnostics.py       # redacted diagnostics download
├── services.yaml
├── ogd/                 # pure client for the open data: STAC discovery,
│                        #   CSV parsing, models. NO Home Assistant imports
│                        #   (ADR-0001); destined for PyPI once stable
├── slf/                 # pure client for the SLF IMIS API (ADR-0009)
├── brand/               # icon.png, icon@2x.png (see docs/brands-icon.md)
├── strings.json / translations/
└── manifest.json
tests/                   # pytest + pytest-homeassistant-custom-component
├── fixtures/            # trimmed real upstream files (tests/README.md)
└── tools/               # live smoke test, fixture refresh (network)
docs/ogd.md              # measured facts about the MeteoSwiss files — READ IT
docs/slf.md              # measured facts about the SLF IMIS API
docs/adr/                # decisions; ADR-0001/0002/0009 constrain every fetch
```

### Non-negotiables from the ADRs

Every change respects these. The agent workflows tell the agent to stop
and say so in the PR rather than break one.

- **Only `data.geo.admin.ch`** (ADR-0001). No app API, no website
  scraping, no third-party weather API. If an issue seems to need one,
  stop and say so.
- **Traffic budget** (ADR-0002): daily forecast by default, hourly is an
  option refreshed at most every 3 h with the minimum parameter set,
  conditional requests always, CSV parsing in the executor, keep only the
  configured point's rows.
- **SLF IMIS is the one exception** (ADR-0009): only
  `/public/api/imis/stations`, `/public/api/imis/station/{code}/measurements`
  and `/public/api/imis/daily-snow` on `measurement-api.slf.ch`, only from
  `slf/`, never the bulk `/imis/measurements`, and no SLF request when no
  IMIS station is configured. SLF-fed entities carry the SLF attribution,
  not the MeteoSwiss one.
- **`ogd/` and `slf/` stay pure Python** (aiohttp/stdlib only).
- **Attribution** `"Source: MeteoSwiss"` on every MeteoSwiss-fed entity.
- **No radar code here** (ADR-0003).
- **Quality gates stay** (ADR-0004): hassfest, HACS validation (no
  `ignore:`), ruff, pytest, CodeQL and the SonarCloud gate are not
  loosened, skipped or excluded to get a PR green. Releases go only
  through the tag gate in `release.yml`.

### Architectural decisions in this repo

On top of the triggers in the skeleton, a change to the **fetch cadence or
the set of files fetched** needs an ADR here (ADR-0002); the full list is
in [`docs/adr/README.md`](docs/adr/README.md). Older ADRs record later
changes as `## Revision N` or `## Update` sections; new changes use the
`## Amendment YYYY-MM-DD (#issue)` form.

### Parallel work in this repo

Parallelize along module boundaries — `ogd/` client, `slf/` client,
coordinator/store, entities, tests — one worktree each.

### Release procedure

Releases are automated via tag push (`.github/workflows/release.yml`,
ADR-0004):

1. **Update version and changelog**: bump `version` in
   `custom_components/meteoswiss_weather/manifest.json` and `VERSION` in
   `const.py`. Rename `## [Unreleased]` to `## [vX.Y.Z] — YYYY-MM-DD`,
   add a fresh empty `## [Unreleased]` and the link definition. Commit as
   `chore(release): vX.Y.Z`.
2. **Verify**: `pytest -q tests/test_metadata.py tests/test_changelog.py`.
3. **Wait for CI**: merge the release commit and wait until CI on
   `master` is green for it. `release.yml` refuses a tag whose commit has
   no successful `ci.yml` run.
4. **Tag** with an **annotated** tag whose message is the release title:
   `git tag -a vX.Y.Z -m "what changed"`. Lightweight tags get a bare title.
5. **Push and watch the run**: `git push origin vX.Y.Z`. The workflow
   verifies the tag against the manifest, extracts the notes, packages
   `meteoswiss_weather.zip` (exactly the tracked files of the integration)
   and creates the release. HACS reads it.

### Testing

- `pip install -r requirements_test.txt` once.
- **Lint**: `ruff check custom_components tests scripts`.
- **Tests**: `pytest -q`. Metadata/changelog/brand tests are stdlib-only;
  integration tests use `pytest-homeassistant-custom-component`
  (`hass` fixture, `enable_custom_integrations`), with upstream responses
  replayed from `tests/fixtures/` — never hit the network in tests.
- Fixtures are trimmed real files: keep a handful of points, keep the
  real header and encoding (Latin-1, `;`). `tests/README.md` says how
  they are cut and refreshed.
- **New behavior gets a test**: a config-flow field gets a flow test, a
  stored field that identifies the user gets a diagnostics assertion.
- hassfest is a required CI check that pytest does not cover; locally:
  `docker run --rm -v "$PWD":/github/workspace ghcr.io/home-assistant/hassfest`.

### Home Assistant pitfalls

Lessons from real integrations; several shipped with every test green.

- **`vol.Required` with an empty-string default blocks the form.** The UI
  treats `""` as "not filled in" and refuses to submit, while a test that
  calls `async_configure` with a value passes. Use `vol.Optional` for
  fields that may stay empty, or a real default; give a required field no
  default at all.
- **Open cards only see what the coordinator announces.** A dashboard card
  re-renders when the entity it watches writes a new state, and coordinator
  entities only write state when the coordinator calls its listeners. Data
  that changes outside `_async_update_data` (a forecast, a service call, a
  push message) must go through `coordinator.async_set_updated_data(...)`
  or `coordinator.async_update_listeners()`; changing an attribute and
  waiting does nothing for an open card.
- **Availability follows the data, not the last poll.** An entity is
  available when the coordinator has data it can show
  (`coordinator.data is not None`), not only when the last update
  succeeded. One failed poll must not blank every card; data that is too
  old to trust is the entity's call, made explicitly.
- **Never block the event loop.** No synchronous I/O, and no parsing of big
  CSV/JSON payloads on the loop: `await hass.async_add_executor_job(parse,
  payload)`. HA logs "Detected blocking call" and the whole UI stalls.
- **Diagnostics redact secrets and location.** Users attach the diagnostics
  file to public issues. Everything in `diagnostics.py` goes through
  `async_redact_data`; API keys, tokens, passwords, coordinates, postal
  codes, hosts and serial numbers are in `TO_REDACT`, and
  `tests/test_diagnostics.py` proves it. A new stored field that identifies
  the user is added to `TO_REDACT` in the same PR.

### Upstream smoke test

`.github/workflows/smoke-test.yml` (ADR-0004) runs
`tests/tools/smoke_test.py` against the live open data and the SLF API
every Monday. On failure it opens or comments on one issue titled
"Upstream open data drift detected" (labels `tracking`, `bug`,
`area-client`, never a P-label, so no agent works it); on recovery it
comments and leaves the issue open for a human to close.

- A PR that starts parsing a new upstream file or endpoint adds a check
  for it to the smoke test in the same PR. Checks validate shape (headers,
  field names, plausible ranges), not content; no data at all is a pass.
- The script stays stdlib-only and secret-free.
- A failing smoke test is not a reason to change the parser blindly:
  first compare what upstream returns now with `docs/ogd.md` /
  `docs/slf.md`, then refresh the fixtures from the new real files
  (`tests/tools/refresh_fixtures.py`).
