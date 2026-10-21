#!/usr/bin/env bash
# Run the test suite against the SANITIZED FIXTURE CORPUS instead of live data.
# This is what CI runs, and what anyone can run -- no account, no agent
# installs, no personal transcripts required.
#
#   ./tests/run_fixtures.sh
#
# The live-data suite (scripts/test_sessions.py) is stronger -- it checks
# real sessions from every harness -- but it only works on a machine where
# those harnesses are installed. The fixture suite is the portable one.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
F="$HERE/fixtures"

# the suite lives inside the skill payload; find it relative to this script
if [ -f "$ROOT/skills/session-time-machine/scripts/test_sessions.py" ]; then
  SUITE="$ROOT/skills/session-time-machine/scripts/test_sessions.py"
elif [ -f "$ROOT/scripts/test_sessions.py" ]; then
  SUITE="$ROOT/scripts/test_sessions.py"
else
  echo "cannot find test_sessions.py near $ROOT" >&2
  exit 1
fi

export STM_HERMES_DB="$F/hermes_state.db"
export STM_OPENCODE_DB="$F/opencode.db"
export STM_CLINE_DB="$F/cline_sessions.db"
export STM_CODEX_DIR="$F/codex_sessions"
export STM_AGY_BRAIN="$F/agy_brain"
export STM_AGY_DIR="$F/agy_conversations"
export STM_OMP_SESSIONS="$F/omp_sessions"
export STM_PI_SESSIONS="$F/pi_sessions"

echo "=== session-time-machine fixture suite (no live data) ==="
echo "fixtures: $F"
echo
python3 "$SUITE"
