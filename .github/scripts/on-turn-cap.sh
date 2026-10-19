#!/usr/bin/env bash
# Handle an implementation run that hit the --max-turns cap.
#
# Without this, a capped run leaves nothing behind and the autopilot retries the
# same issue every tick, each time from zero (issue #178 burned two Opus runs).
# Here the partial work is saved to a `wip/claude-<n>-<run>` branch, the issue
# gets a comment that says "too big, split it", and `needs-verification` keeps
# the autopilot off it until a human has split or relabelled it. A later run on
# the same issue is told to continue from that branch.
#
# Only acts when the execution log says `error_max_turns`; any other failure
# keeps the old behaviour (nothing saved, normal retry).
#
# Usage: on-turn-cap.sh <issue-number> <execution-file>
# Env: GH_TOKEN (contents/issues write), GITHUB_REPOSITORY, RUN_URL.
set -euo pipefail

issue="${1:?usage: on-turn-cap.sh <issue> <execution-file>}"
exec_file="${2:-}"
# The action may not set its output when it fails; its default path is known.
if [ -z "$exec_file" ] && [ -n "${RUNNER_TEMP:-}" ]; then
  exec_file="${RUNNER_TEMP}/claude-execution-output.json"
fi

if [ -z "$exec_file" ] || [ ! -f "$exec_file" ]; then
  echo "no execution file; not a recognisable turn-cap failure" >&2
  exit 0
fi
if ! grep -q '"error_max_turns"' "$exec_file"; then
  echo "run failed for another reason; leaving it to the normal retry" >&2
  exit 0
fi

saved=""
# The agent may have opened its PR before running out of turns; then the
# work is already on GitHub and only the comment below is needed.
open_pr="$(gh pr list --state open --json number,headRefName \
  --jq "[.[] | select(.headRefName | startswith(\"claude/${issue}-\"))][0].number // empty")"

if [ -z "$open_pr" ]; then
  git config user.name "github-actions[bot]"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
  default="$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name)"
  git add -A
  git commit -q -m "wip: partial work for #${issue} (run hit the turn cap)" || true
  if [ -n "$(git rev-list "origin/${default}..HEAD" 2>/dev/null)" ]; then
    saved="wip/claude-${issue}-${GITHUB_RUN_ID:-manual}"
    git push -q "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" \
      "HEAD:refs/heads/${saved}"
  fi
fi

{
  echo "The implementation run hit the turn cap (\`error_max_turns\`) and stopped: ${RUN_URL:-}"
  echo
  if [ -n "$open_pr" ]; then
    echo "Its draft PR #${open_pr} holds the work so far."
  elif [ -n "$saved" ]; then
    echo "Partial work is saved on branch \`${saved}\`; a later run on this issue continues from it."
  else
    echo "The run left no changes to save."
  fi
  echo
  echo "This issue is probably too big for one run. Split it into slices of one layer each (docs/agent-automation.md, \"Sizing an issue\"), then remove \`needs-verification\` from the slice the autopilot should take."
} > /tmp/turn-cap-comment.md

gh issue comment "$issue" --body-file /tmp/turn-cap-comment.md
gh issue edit "$issue" --add-label needs-verification
