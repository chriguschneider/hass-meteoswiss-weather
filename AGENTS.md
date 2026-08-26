# AGENTS.md

Conventions for AI-assisted contributions to this repo (Claude Code,
Cursor, Codex, Aider, or any other assistant). Read this first if you are
an AI assistant working on a branch, or a human driving one.

---

## Shared Skeleton

These conventions apply across multiple repos and can be adopted by
sibling projects. Sibling repos should maintain their own `AGENTS.md`
files with this skeleton plus repo-specific sections below.

### Commit attribution

Commits made with AI assistance carry a `Co-Authored-By:` trailer that
names the tool and model honestly, e.g.:

```
Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
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

---

## Repo-Specific: MeteoSwiss Weather

### What this is

A Home Assistant integration that reads the **official MeteoSwiss open
data** (opendatadocs.meteoswiss.ch) and exposes a `weather` entity plus
station sensors. It is the sibling of
[`hass-meteoswiss-radar`](https://github.com/chriguschneider/hass-meteoswiss-radar)
(ADR-0003) and deliberately **not** a fork of any app-API integration.

### Repo structure (target)

```
custom_components/meteoswiss_weather/
├── __init__.py          # entry setup/unload, platforms
├── config_flow.py       # UI setup + options
├── const.py             # DOMAIN, VERSION, OGD constants, intervals
├── coordinator.py       # DataUpdateCoordinator(s): station + forecast
├── weather.py           # WeatherEntity (current + daily [+ hourly opt-in])
├── sensor.py            # SwissMetNet station sensors
├── ogd/                 # pure client: STAC discovery, CSV parsing, models
│   ├── __init__.py      #   NO Home Assistant imports (ADR-0001) — this
│   ├── stac.py          #   package is destined for PyPI once stable
│   ├── stations.py
│   ├── forecast.py
│   └── models.py
├── brand/               # icon.png, icon@2x.png (see docs/brands-icon.md)
├── strings.json / translations/en.json
└── manifest.json
tests/                   # pytest + pytest-homeassistant-custom-component
docs/ogd.md              # measured facts about the upstream files — READ IT
docs/adr/                # decisions; ADR-0001/0002 constrain every fetch
```

The tree above is the plan; the scaffold ships only `__init__.py`,
`config_flow.py`, `const.py`, strings and brand. Issues fill it in.

### Non-negotiables from the ADRs

- **Only `data.geo.admin.ch`** (ADR-0001). No app API, no website
  scraping, no third-party weather API. If an issue seems to need one,
  stop and say so.
- **Traffic budget** (ADR-0002): daily forecast by default, hourly is an
  option refreshed at most every 3 h with the minimum parameter set,
  conditional requests always, CSV parsing in the executor, keep only the
  configured point's rows.
- **`ogd/` stays pure Python** (aiohttp/stdlib only).
- **Attribution** `"Source: MeteoSwiss"` on every entity.
- **No radar code here** (ADR-0003).

### Architectural decisions

If a change adds an upstream endpoint, changes fetch cadence or the set
of files, adds a platform or option, adds a quality gate, or deviates
from an existing ADR, it needs an ADR. Triggers and the template are in
[`docs/adr/README.md`](docs/adr/README.md). Land the ADR with the code.

Claude Code users in this clone get automatic prompts via the repo-local
skills below. Other tools should read `docs/adr/README.md` directly.

### Repo-local skills

Two Claude Code skills are checked into `.claude/skills/`:

- **`commit-guardian`** — before any `git commit`, checks the staged diff
  against accepted ADRs and these conventions. Reports a numbered list;
  the user decides. Non-blocking.
- **`documentation-guardian`** — proposes an ADR when an architectural
  change happens and flags changes that contradict an existing decision.

### Parallel work

- **Branch naming**: `<tool-or-initials>/<issue>-<slug>`, e.g.
  `claude/4-station-client`.
- **Issue claiming**: `gh issue edit <N> --add-assignee @me` before
  starting, so parallel contributors (and the autopilot) see it is taken.
- **Worktrees**: parallelize along module boundaries — `ogd/` client,
  coordinator/entities, tests — one worktree each:
  `git worktree add ../msw-<issue> <branch>`.

### Draft PRs

CI (hassfest + HACS + ruff + pytest) runs on every push. If you iterate
with several pushes, open the PR as a **draft** until you expect CI to
pass, then mark ready. Direct push to `master` is not the flow; open a PR.

### Automation

Opt-in, draft-PR-only (see [`docs/agent-automation.md`](docs/agent-automation.md)):
add the **`agent:go`** label to hand an issue to Claude, or write **@claude**
in a comment (owner/collaborators only). The autopilot grinds P-labelled
issues one at a time. The model comes from the issue's labels via
`.github/scripts/pick-model.sh`. The agent cannot modify
`.github/workflows/`.

### Release procedure

Releases are automated via tag push (`.github/workflows/release.yml`,
ADR-0004):

1. **Update version and changelog**: bump `version` in
   `custom_components/meteoswiss_weather/manifest.json` and `VERSION` in
   `const.py`. Add a `## [vX.Y.Z] — YYYY-MM-DD` section at the top of
   `CHANGELOG.md` (Keep a Changelog) plus its link definition. Commit as
   `chore(release): vX.Y.Z`.
2. **Verify**: `pytest -q tests/test_metadata.py tests/test_changelog.py`.
3. **Tag** with an **annotated** tag whose message is the release title:
   `git tag -a vX.Y.Z -m "what changed"`. Lightweight tags get a bare title.
4. **Push and watch CI**: `git push origin vX.Y.Z`. The workflow verifies
   the tag against the manifest, extracts the notes, packages
   `meteoswiss_weather.zip` and creates the release. HACS reads it.

### Testing

- `pip install -r requirements_test.txt` once.
- **Lint**: `ruff check custom_components tests scripts`.
- **Tests**: `pytest -q`. Metadata/changelog/brand tests are stdlib-only;
  integration tests use `pytest-homeassistant-custom-component`
  (`hass` fixture, `enable_custom_integrations`), with upstream responses
  replayed from `tests/fixtures/` — never hit the network in tests.
- Fixtures are trimmed real files: keep a handful of points, keep the
  real header and encoding (Latin-1, `;`).
