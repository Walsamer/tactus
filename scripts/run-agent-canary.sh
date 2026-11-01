#!/usr/bin/env bash
# Thin wrapper for the operator canary (Issue #22 phase 2).
#
# The command, its approvals and its evidence live in scripts/run_agent_canary.py;
# this only picks the interpreter the project pins.
#
#   scripts/run-agent-canary.sh --dry-run --approve --operator NAME
#   scripts/run-agent-canary.sh --approve --operator NAME --model SLUG
set -euo pipefail

cd "$(dirname "$0")/.."

if command -v uv >/dev/null 2>&1; then
  exec uv run --quiet python scripts/run_agent_canary.py "$@"
fi
exec python3 scripts/run_agent_canary.py "$@"
