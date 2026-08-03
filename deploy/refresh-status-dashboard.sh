#!/usr/bin/env bash
# Re-run the gates and re-render the owner's status page.
#
# Companion to refresh-plane-board.sh, which projects TASK state. This projects
# VERDICT state, which is the thing that actually moves and the thing Plane
# could not show: Plane's snapshot reads 56 PROPOSED / 1 FAILED_ORACLE whether
# the board is green or red.
#
# The gate summary is written under .data/ (gitignored) rather than to
# evidence/gate-run-summary.json on purpose. A timer that rewrites a TRACKED
# evidence file every two hours leaves the working tree permanently dirty and
# makes every `git status` a lie about whether anyone changed anything. The
# evidence copy stays under human control; this one is a cache for the page.
#
# Staleness is rendered rather than hidden: the page compares the candidate
# commit to HEAD and says so when they differ. The defect that produced this
# script was a persisted summary sitting 14 commits behind HEAD while reporting
# PASS=11 FAIL=0, when the truth was PASS=11 FAIL=1.
#
# Exit codes are cron-visible. Silence in the log means it has not been running
# at all, which is a different problem from a failing run and must not look the
# same.

set -euo pipefail

REPO="/home/yoav/efah/efah-harness"
VENV="/home/yoav/efah/.venv/bin/python"
ENVFILE="/home/yoav/.efah/env"
LOG="${REPO}/.data/status-dashboard.log"
SUMMARY="${REPO}/.data/gate-run-latest.json"
OUT="${REPO}/.data/dashboard"

mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1

echo "--- $(date -u +%Y-%m-%dT%H:%M:%SZ) status refresh starting"

if [ ! -r "$ENVFILE" ]; then
  echo "    REFUSING: ${ENVFILE} is not readable; the gate run needs it"
  exit 78            # EX_CONFIG
fi

# shellcheck disable=SC1090
set -a && . "$ENVFILE" && set +a

cd "$REPO"

# Capture the runner's real exit code. `if ! cmd; then status=$?` does NOT do
# this -- inside the branch $? is the status of the negation, which is always 0,
# so a genuine failure would exit 0 and read as success. That is the same shape
# as the fabricated kill_rate: a missing tool exiting like a passing run.
set +e
PYTHONPATH=src "$VENV" -m evaluation.gate_runner --json "$SUMMARY" >/dev/null
gate_status=$?
set -e

if [ "$gate_status" -ne 0 ]; then
  # The runner exits non-zero when the board is red. That is a real verdict, not
  # an infrastructure fault, and the page must still be rendered from it --
  # refusing to draw a red board is how a dashboard starts lying.
  echo "    gate_runner exit ${gate_status} (a red board is a verdict, not a fault)"
  if [ ! -s "$SUMMARY" ]; then
    echo "    FAILED: no summary written; nothing to render"
    exit "$gate_status"
  fi
fi

if PYTHONPATH=src "$VENV" tools/build_status_dashboard.py --summary "$SUMMARY" --out "$OUT"; then
  echo "    ok"
else
  status=$?
  echo "    FAILED to render with exit ${status}"
  exit "$status"
fi
