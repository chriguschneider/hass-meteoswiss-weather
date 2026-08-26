---
name: commit-guardian
description: Before any `git commit` in this repo, verify that staged changes do not contradict an accepted ADR in docs/adr/ or break the conventions in AGENTS.md. Activates when the user says "commit", "commit this", "let's commit", or before running `git commit ...` via Bash. Reports findings as a numbered list referencing ADR number plus file:line, then waits for user confirmation. Non-blocking — the user always decides.
---

# Commit Guardian

Pre-commit ADR and convention check for the MeteoSwiss Weather repo. Runs
before any `git commit` to catch violations of accepted decisions in
`docs/adr/` and conventions in [`AGENTS.md`](../../../AGENTS.md).

Complementary to [`documentation-guardian`](../documentation-guardian/SKILL.md):
that one triggers when an architectural change *happens*; this one
triggers when a commit is *imminent*.

## Activation triggers

- About to run `git commit ...` via Bash.
- The user says "commit", "commit this", "let's commit", "ready to commit".

## Skip

- The user opts out ("just commit", "skip the check").
- Trivial diffs with no architectural surface: typo/prose fixes,
  whitespace, comment rewording, a single constant/default tweak.
- Pure version bump in manifest.json + const.py for a release commit
  (that is the *required* sync, not a violation).

## Workflow

1. `git diff --staged` and `git status` to see what is actually staged.
2. `git rev-parse --abbrev-ref HEAD`. If it is `master`, **stop** and
   offer to create a feature branch (the flow is PR-only).
3. Read every ADR under `docs/adr/` (skip `README.md`, `template.md`) plus
   `AGENTS.md`, unless already in context. Extract checkable rules.
4. Match each finding to a `path:line` in the diff.
5. Output:
   - **All clear:** one line, "All checks pass, proceeding with commit."
   - **Findings:** numbered list, each naming `ADR NNNN` or the convention
     source, the rule, and the offending `path:line`.
6. If anything is flagged, wait for the user. Never block.

## Checkable rules (current)

From the ADRs:

- **ADR-0001** — the only upstream is the official open data under
  `data.geo.admin.ch`. Flag any URL on `meteoswiss-app.ch`,
  `meteoschweiz.admin.ch/product`, `meteosuisse.admin.ch` or any other
  host, and any HTML scraping. Flag HA imports inside
  `custom_components/meteoswiss_weather/ogd/` (the client stays pure).
- **ADR-0002** — traffic budget. Flag an hourly local-forecast fetch
  interval below 3 h, hourly parameters fetched when the option is off,
  more hourly parameter files than the documented minimum set, a download
  without conditional headers, or CSV parsing on the event loop.
- **ADR-0003** — no code shared with or imported from the radar
  integration; no radar features (map, frames, INCA) in this repo.

From AGENTS.md:

- **Version sync** — manifest.json and const.py `VERSION` must match.
  Flag a diff that changes one without the other.
- **English only** in code, comments, commit messages.
- **Commit trailer** — an AI-authored commit carries an honest
  `Co-Authored-By:` trailer.
- **No direct commit to `master`.**

Do not rely on this list being complete: new ADRs land over time, always
re-read `docs/adr/`.
