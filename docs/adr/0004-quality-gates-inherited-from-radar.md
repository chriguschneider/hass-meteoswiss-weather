# ADR-0004: Quality gates and release process inherited from the radar repo

- **Status:** Accepted
- **Date:** 2026-08-26

## Context

The sibling radar repository arrived at its CI, release and analysis
setup through eight ADRs and a series of real incidents (releases tagged
against the wrong tree, a workflow that an ADR decided but nobody wrote,
zero download counts because HACS never fetched an asset). Re-deriving
those lessons here would repeat the incidents.

## Decision

Adopt the radar repo's gates and process as they stand on 2026-08-26, by
reference rather than by copying the reasoning:

- **CI on every push and PR** (`ci.yml`): hassfest, HACS validation with
  no `ignore:`, ruff (`E F W B UP I`), pytest. All four are required
  status checks on `master`; the branch is PR-only.
- **Tag-triggered release gate** (`release.yml`, radar ADR-0004): an
  annotated `vX.Y.Z` tag becomes a GitHub release only if the tag matches
  `manifest.json`, `CHANGELOG.md` has a non-empty section for it, and no
  release exists yet. The release ships `meteoswiss_weather.zip` with the
  integration directory's contents at the zip root, and `hacs.json`
  declares `zip_release` (radar ADR-0008).
- **CodeQL** `security-extended` for Python on push, PR and weekly (radar
  ADR-0005).
- **SonarCloud** CI-based analysis with coverage, gate-blocking, skipped
  while `SONAR_TOKEN` is absent (radar ADR-0007).
- **Weekly upstream smoke test** that opens or updates a drift issue and
  is not a PR check (radar ADR-0006). The script is a tracked issue; the
  workflow skips until it exists.
- **Agent workflows** exactly as in the radar repo: opt-in via `agent:go`,
  `@claude` for owner/collaborators, autopilot over the P-backlog one PR
  at a time, an independent Opus reviewer that auto-merges or holds with
  `needs-verification`. The agent token cannot touch
  `.github/workflows/`. All four skip themselves while
  `CLAUDE_CODE_OAUTH_TOKEN` is absent.

Differences from the radar repo are intentional and small: no JavaScript
jobs (there is no card), `pytest-homeassistant-custom-component` is
installed in CI so real integration tests can be written from the first
slice, import sorting is enabled from day one, and `.gitattributes`
normalises line endings.

## Consequences

- A change to a gate here should be mirrored in the radar repo, or the
  divergence recorded in a new ADR.
- The first release is not cut until the integration produces a weather
  entity; until then `CHANGELOG.md` carries the scaffold version without
  a tag.
- Secrets (`CLAUDE_CODE_OAUTH_TOKEN`, `SONAR_TOKEN`) and the SonarCloud
  project are per-repository and have to be created by the owner.
