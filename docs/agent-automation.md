# Agent automation (opt-in)

Claude works an issue **only when you hand it over** or when the autopilot
takes it from the P-labelled backlog. All flows open **draft PRs only** and
never push to the default branch. Every workflow skips itself with a notice
while the `CLAUDE_CODE_OAUTH_TOKEN` secret is missing, so a fresh setup is
never red.

## Opt-in by label (`claude-labeled.yml`)

Add the **`agent:go`** label to an issue → Claude implements it on a
`claude/<n>-<slug>` branch and opens a draft PR. Issues without the label
are never touched by this flow.

- `gh issue edit <n> --add-label agent:go` (or add it in the UI).
- Manually: Actions → "Claude (opt-in via label)" → Run workflow → issue number.
- On failure the `agent:go` label is removed so it isn't left looking claimed.

## On @claude mention (`claude-mention.yml`)

Write `@claude ...` in an issue or PR comment. Gated to the owner /
collaborators (comment events carry secrets, so strangers can't trigger it).

## Model per issue

`.github/scripts/pick-model.sh` chooses the model from labels:

| Label | Model |
|---|---|
| `agent:opus` | `claude-opus-4-8` |
| `agent:haiku` | `claude-haiku-4-5` |
| `agent:sonnet` / (none) | `claude-sonnet-4-6` |
| `P1` | `claude-opus-4-8` |
| `P3` / `good first issue` | `claude-haiku-4-5` |

In this repo the P-labels order the backlog (see below), so most slice
issues also carry an explicit `agent:*` label to pick the model on merit.

## Guardrails

- The agent **cannot change `.github/workflows/`** (token lacks the
  `workflows` permission) — a deliberate safety boundary; it documents such
  changes for a human instead.
- The prompts tell the agent to stay on the official open data (ADR-0001)
  and inside the traffic budget (ADR-0002), and to stop rather than guess.
- Ambiguous issue → draft with a "Blocked / needs decision" note.
- Implementation runs (autopilot and label flow) are capped at **150
  turns**. A run that hits the cap leaves nothing behind — no branch, no
  PR, no comment — and the autopilot retries the same issue every 30
  minutes. So the cap is not a budget to fill: an issue that needs
  upstream measurement, an ADR and code is three issues (measure and
  decide by hand, then implementation slices), not one.

## Automated review + auto-merge

`claude-review.yml` runs an **independent Opus reviewer** on every agent
draft PR (`claude/*`). It reviews adversarially, runs the tests, fixes
substantive problems on the branch (**at most 3 rounds**), then either:

- **auto-merges** (`gh pr merge --auto --squash`) when green and solid, or
- **holds for you** when the change needs a live Home Assistant or a human
  decision, when the PR or its issue carries the **`needs-verification`**
  label, or when it could not make the change solid — it then labels the PR
  `needs-verification` and comments exactly what to check.

The reviewer runs **hassfest** next to `ruff` and `pytest` (it is a required
CI check that pytest does not cover), and after its verdict the job **waits
for the required checks**: if one fails after auto-merge was armed, the PR
gets `needs-verification` and a comment naming the check — otherwise it
would sit silently (the reviewer does not re-run on CI results and the
autopilot waits while an agent PR is open; PR #74 did exactly that).

To force a human check on anything, add the **`needs-verification`** label to
the issue or PR. Auto-merge respects branch protection (required checks pass
first). Reviewer = Opus, author = Sonnet/Opus per label, so it is not grading
its own work.

## Autopilot: backlog grind (`claude-autopilot.yml`)

After **every closed PR**, every completed **CI** or **review** run, every
issue label/assignment change, and on a cron at :13/:43 (which GitHub fires
only best-effort; on 2026-08-28 it ran about every 10 h) — **if no agent PR
is open**, it takes the next open P-issue
(P1 before P2 before P3, lowest number first), opens a draft PR, the
reviewer auto-merges it, and the next tick takes the next — until the
backlog is empty. One PR at a time (no conflicts). Skips issues whose title
starts with `Tracking:`, anything assigned, and anything labelled `agent:go`,
`agent:in-progress` or `needs-verification`. Pause it by disabling the
workflow in Actions; kick it by hand with `gh workflow run claude-autopilot.yml`
when nothing is running and the backlog is not empty.

**Ordering the backlog** therefore means: give an issue a P-label only when
its prerequisites are merged, or accept that the agent will hit a missing
dependency and open a "Blocked" draft. The tracking issue lists the
dependency graph.

## Setup (one-time)

1. Install the Claude GitHub App on this repository
   (https://github.com/apps/claude) — or check that the existing
   installation covers it under https://github.com/settings/installations.
2. Add the repo secret `CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`):
   `gh secret set CLAUDE_CODE_OAUTH_TOKEN -R chriguschneider/hass-meteoswiss-weather`.
3. Optional: SonarCloud project + `SONAR_TOKEN` (see
   `sonar-project.properties`).
