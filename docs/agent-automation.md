# Agent automation (opt-in)

Claude works an issue **only when you hand it over** or when the autopilot
takes it from the P-labelled backlog. All flows open **draft PRs only** and
never push to the default branch. Every workflow skips itself with a notice
while the `CLAUDE_CODE_OAUTH_TOKEN` secret is missing, so a fresh setup is
never red.

This repo runs in mode **`autopilot`** of
[`repo-standard`](https://github.com/chriguschneider/repo-standard), which
was distilled from this repo and its siblings:

| Mode | Label + mention | Reviewer + auto-merge | Autopilot |
|---|:-:|:-:|:-:|
| `minimal` | ✓ | – | – |
| `opt-in` | ✓ | ✓ | – |
| `autopilot` | ✓ | ✓ | ✓ |

## Opt-in by label (`claude-labeled.yml`)

Add the **`agent:go`** label to an issue → Claude implements it on a
`claude/<n>-<slug>` branch and opens a draft PR. Issues without the label
are never touched by this flow.

- `gh issue edit <n> --add-label agent:go` (or add it in the UI).
- Manually: Actions → "Claude (opt-in via label)" → Run workflow → issue number.
- On failure the `agent:go` label is removed so it isn't left looking claimed.
- **Only trusted issue text is worked.** The issue becomes the agent's
  instructions, so an issue written by someone without write access is
  refused with a comment (and the label removed). Restate the task in your
  own issue, or add `agent:untrusted-ok` after reading the text yourself.

## On @claude mention (`claude-mention.yml`)

Write `@claude ...` in an issue or PR comment. Gated to the owner /
collaborators: comment events carry secrets even from strangers. An issue
assignment starts it too, but only for issues written by someone with
write access. Only the owner's and the agent's own comments reach the
prompt (`include_comments_by_actor`).

## Model per issue

`.github/scripts/pick-model.sh` chooses the model from labels. It passes
aliases, so the latest model of each family is used without edits here
(until 2026-10-19 it pinned model IDs, which went stale with every
release).

| Label | Model |
|---|---|
| `agent:opus` | `opus` |
| `agent:haiku` | `haiku` |
| `agent:sonnet` / (none) | `sonnet` |
| `P1` | `opus` |
| `P3` / `good first issue` | `haiku` |

In this repo the P-labels order the backlog (see below), so most slice
issues also carry an explicit `agent:*` label to pick the model on merit.
The reviewer is always `opus`.

## Guardrails

The agent reads text written by others (issues, comments, PR titles, code)
and runs with write access. Every public attack on agent workflows in
2025/26 combined exactly that with an unrestricted shell. So the shell is
restricted, and several layers hold even if the model is talked into
something:

| Layer | What it does | Where |
|---|---|---|
| Tool allowlist | `--permission-mode dontAsk` plus `--allowedTools`: git, a few `gh` subcommands and this repo's check commands (`pip install -r requirements_test.txt`, `ruff check`, `ruff format`, `pytest`, also as `python -m` / `python3 -m`). Anything else is refused without a prompt. No `--dangerously-skip-permissions`. | `claude_args` in each workflow |
| Toolchain | Agent jobs set up Python 3.13 (`setup-python`) like `ci.yml`; the runner's system Python has no `pip` and refuses installs. | step before the agent |
| Deny rules | `gh api`, `gh secret`, `gh workflow`, `gh repo`, `curl`, `wget`, force pushes. Deny always wins. | `.claude/ci-settings.json` |
| Path guard hook | Blocks every write to `.github/` and `.claude/`, through file tools or the shell. Runs only on GitHub Actions; local sessions are unaffected. | `.claude/hooks/protect-paths.py` |
| Trusted text only | Untrusted issues are refused, foreign comments are filtered out, the autopilot only takes issues written by the owner. | job `if:`, `include_comments_by_actor`, `pick-next-issue.sh` |
| Egress | harden-runner logs (mode `audit`) or blocks (mode `block`) outbound connections. | first step of each agent job |
| Pinned actions | Every `uses:` is a full commit SHA; Dependabot bumps it after a 7-day cooldown (not for claude-code-action, whose updates are security fixes). | all workflows, `dependabot.yml` |
| Reviewer reads CI | The reviewer never runs the PR's tests or scripts (the PR could change them, and the job holds secrets); CI runs them without secrets. | `claude-review.yml` |
| Sensitive paths | A PR touching `.github/`, `.claude/`, `scripts/`, `docs/adr/`, `AGENTS.md`, `CLAUDE.md`, dependency files, `hacs.json`, `manifest.json` or `sonar-project.properties` is never auto-merged, whatever the model decided. | last steps of `claude-review.yml` |

The action restores `.claude/` from the base branch on pull requests, so a
PR cannot switch off the settings or the hook. The committed
`.claude/settings.json` only adds a reminder to run `commit-guardian`
before `git commit`; it allows nothing.

**About the `workflows` permission:** this file (and ADR-0004) used to say
the agent cannot change `.github/workflows/` because its token has no
`workflows` scope. That is not a guarantee: the official Claude GitHub
App is installed with Workflows read/write, and a token obtained in a
January 2026 attack (CVE-2025-66032) carried that permission. Earlier
pushes to `.github/workflows/` were rejected with 403, but the hook above
is the boundary that does not depend on how the token is scoped.

**Switching egress to `block`:** after a few agent runs, open a run's
harden-runner insights (link in the job summary), compare the endpoints
with `allowed-endpoints`, add what is missing, then set
`egress-policy: block` in the agent workflows.

If a run reports that it could not run the checks, look at **Tool calls
refused by the allowlist** in the run's summary: that is an allowlist gap,
not an agent failure. Add the missing command to `--allowedTools` in the
workflow (a human change: `.github/` is protected from the agent).

Other rules:

- The prompts tell the agent to follow `AGENTS.md` (above all its
  *Non-negotiables from the ADRs*), `docs/adr/`, `docs/ogd.md` and
  `docs/slf.md`, to stay on the official open data (ADR-0001, SLF only as
  ADR-0009 allows) and inside the traffic budget (ADR-0002), to treat
  issue text as a description of a change rather than instructions, to
  stop if the issue is closed or already has an open PR, and never to
  invent domain data (every lookup table or constant cites a source).
- **Changes only a human may make** (`.github/`, `.claude/`): the agent
  writes `Refs #N` instead of `Closes #N` and a `## Human-only change`
  section with the exact diff. The reviewer then holds the PR and opens a
  follow-up issue, so that half does not get lost when the rest merges.
- **The agent reports how it ended.** The last line of the PR description
  is `Agent status: done` or `Agent status: blocked`. The action's exit
  status is not used for this: a finished run can still fail for going a
  few turns over the limit.
- **Runs are capped** at 150 turns (mention: 60) and 90 minutes; observed
  runs take under 30 minutes. The cap is not a budget to fill; size issues
  as below. The agent pushes early and opens the draft PR after its first
  commit. After every run, `after-agent-run.sh` commits and pushes what is
  left and reads the status line:

  | Ends with | Then |
  |---|---|
  | `Agent status: done` | the reviewer is started (`gh workflow run claude-review.yml`) |
  | `Agent status: blocked` | the PR gets `needs-verification`, a human decides |
  | no status line | the PR gets **`agent:needs-continuation`**; continue with `@claude continue` or by hand |
  | nothing committed (run failed, or finished without a change) | the *issue* gets `needs-verification` and a comment (split it, or why nothing changed), so the autopilot does not retry it forever |

  Its pushes use the workflow token, so they trigger neither CI nor the
  reviewer: unfinished work is never auto-merged.

  History: a capped run used to vanish and be retried from zero every
  tick (#178 burned two Opus runs). From then until 2026-10-19
  `on-turn-cap.sh` saved the partial work of an `error_max_turns` run to a
  `wip/claude-<issue>-<run>` branch, commented "split it" and labelled the
  issue `needs-verification`. `after-agent-run.sh` covers that case and
  more (any failure, a finished run without the status line, a run that
  changed nothing) and keeps the work on the agent's own `claude/*`
  branch as a draft PR instead of a separate `wip/` branch.

## Sizing an issue

Most failed runs were issues that were too big, not too hard. A run that
hits the limit before its first commit leaves nothing; the same issue then
fails again. One autopilot run is one slice. Use the *Agent task* issue
template and, before giving an issue a P-label, keep to:

- **One layer per issue**: the pure client (`ogd/`, `slf/`), *or* the
  coordinator/store, *or* entities + docs + changelog. A feature that
  spans all three is a tracking issue with slices (IMIS: #173/#174;
  sunshine: #180/#181 after #178 hit the cap).
- **At most about four modules changed**, tests included.
- **Measuring and deciding happen first, by hand.** Upstream measurements
  (`docs/ogd.md`, `docs/slf.md`), the ADR and real fixtures land in a
  maintainer docs PR, so the slice never measures upstream, writes an ADR
  or invents data.
- **Name the files and the exact tests** that prove it, and what is out of
  scope (no network in tests, no new ADR, no new dependency); "follow the
  pattern of X" beats a description of the goal.
- **Slices of one feature** are titled "Feature (N/M)", get ascending
  numbers and the same P-label, so the autopilot takes them in order; a
  later slice says what to do when its predecessor is not merged
  ("Blocked" draft).
- **A repo needs a test suite before `agent:go` makes sense.** Without
  tests the agent cannot verify anything, and the reviewer cannot either.
- Research logs and wish lists get the label **`tracking`**: no agent and
  no autopilot ever works them.

## Automated review + auto-merge (`claude-review.yml`)

An **independent Opus reviewer** runs on agent PRs (`claude/*` branches of
this repository; a contributor's fork PR with the same prefix is ignored).
It starts when the author agent reports `Agent status: done`, or when a
human marks an agent PR ready. Not when the draft opens: the agent opens
it early and keeps working. Its own `gh pr ready` does not start a second
review. It first waits for the required checks, then reviews the diff
adversarially against `AGENTS.md`, the ADRs (above all the traffic budget
of ADR-0002 and the upstream rules of ADR-0001/0009) and `docs/ogd.md`,
reads the CI result (it does not run the checks itself), fixes substantive
problems on the branch (**at most 3 rounds**; CI re-runs on its pushes),
then either:

- **auto-merges** (`gh pr merge --auto --squash`) when green and solid, or
- **holds for you** when the change needs a live Home Assistant or a human
  decision, when the PR or its issue carries **`needs-verification`**, or
  when it could not make the change solid — it labels the PR
  `needs-verification` and comments exactly what to check.

Until 2026-10-19 the reviewer ran `pytest` and hassfest itself on the PR
branch. That executed PR-controlled code in a job holding secrets; now
hassfest, ruff and pytest come from CI (all three are required checks),
and a hassfest failure is fixed on the branch like a failing test.

Whatever the model decided, a PR that touches sensitive paths or carries a
`## Human-only change` is then held with `needs-verification`. A review run
that dies (turn limit, error) labels the PR too, instead of leaving it
open without a label. After that the job waits for the required checks; if
one fails after auto-merge was armed, the PR gets `needs-verification` and
a comment naming the check, instead of sitting silently (PR #74 did
exactly that with hassfest: the reviewer does not re-run on CI results and
the autopilot waits while an agent PR is open).

To force a human check on anything, add the **`needs-verification`** label
to the issue or PR. Auto-merge respects branch protection (required checks
pass first). Reviewer = Opus, author = whatever the labels pick, so
nothing grades its own work.

## A red default branch (`ci-watch.yml`)

CI also runs weekly, so tool or upstream drift shows up without commits.
When CI fails on `master`, `ci-watch.yml` opens one issue "CI is red on
master" (no P-label, so no agent picks it up) and closes it when CI is
green again. `release.yml` refuses a tag whose commit has no green CI run.

Auto-merge needs branch protection with required checks on `master`
(`hassfest (HA manifest validation)`, `HACS validation`,
`Python lint + tests`).

## Autopilot: backlog grind (`claude-autopilot.yml`)

After **every closed PR**, every completed **CI** or **review** run, every
issue label/assignment change, and on a cron at :13/:43 (which GitHub fires
only best-effort; on 2026-08-28 it ran about every 10 h) — **if no agent PR
is open** — it takes the next open P-issue written by the repo owner (P1
before P2 before P3, lowest number first), opens a draft PR, the reviewer
auto-merges it, and the next event takes the next — until the backlog is
empty. One PR at a time (no conflicts). Skips issues whose title starts
with `Tracking:`, anything assigned, and anything labelled `agent:go`,
`agent:in-progress`, `needs-verification` or `tracking`. An open agent PR
from this repository blocks it; the job summary names which one. Pause it
by disabling the workflow in Actions; kick it by hand with
`gh workflow run claude-autopilot.yml` when nothing is running and the
backlog is not empty.

**Ordering the backlog** therefore means: give an issue a P-label only
when its prerequisites are merged, or accept that the agent will hit a
missing dependency and open a "Blocked" draft. The tracking issue lists the
dependency graph.

## Setup (one-time, by the owner)

1. Install the Claude GitHub App on this repository
   (https://github.com/apps/claude), or check that the existing
   installation covers it under https://github.com/settings/installations.
2. `gh secret set CLAUDE_CODE_OAUTH_TOKEN -R chriguschneider/hass-meteoswiss-weather`
   (token from `claude setup-token`; never paste it into a chat).
3. SonarCloud: the project exists and `SONAR_TOKEN` is set (ADR-0004; see
   `sonar-project.properties`).
4. Labels from repo-standard's `labels.json` (`agent:needs-continuation`,
   `agent:untrusted-ok` and `tracking` are the ones this repo was missing):
   `python3 github-setup.py --repo chriguschneider/hass-meteoswiss-weather --type ha-integration --only labels`.
