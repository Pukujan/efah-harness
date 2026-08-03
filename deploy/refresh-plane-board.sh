#!/usr/bin/env bash
# Refresh the Plane board from the compiled contract and live gate verdicts.
#
# Run on a timer so the board is current without anyone remembering to run it.
# The owner's complaint that produced this file was: "i dont have a dashboard to
# know what is going where" -- a board that is only accurate when someone thinks
# to refresh it is the same problem with extra steps.
#
# Section 4.1 fixes Plane at mode: projection_only. This is one-way. The
# compiler and TerminusDB are truth; Plane is a view; nothing is read back.
# Upsert is by external id, so re-running updates the same cards.
#
# Exit codes are cron-visible: a non-zero run leaves a line in the log with a
# timestamp. Silence in the log means it has not been running at all, which is a
# different problem from a failing projection and must not look the same.

set -euo pipefail

REPO="/home/yoav/efah/efah-harness"
VENV="/home/yoav/efah/.venv/bin/python"
ENVFILE="/home/yoav/.efah/env"
LOG="${REPO}/.data/plane-refresh.log"

mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1

echo "--- $(date -u +%Y-%m-%dT%H:%M:%SZ) refresh starting"

if [ ! -r "$ENVFILE" ]; then
  echo "    REFUSING: ${ENVFILE} is not readable; no credential, no projection"
  exit 78            # EX_CONFIG
fi

# shellcheck disable=SC1090
set -a && . "$ENVFILE" && set +a

cd "$REPO"
if PYTHONPATH=src "$VENV" tools/project_to_plane.py; then
  echo "    ok"
else
  status=$?
  echo "    FAILED with exit ${status}"
  exit "$status"
fi
