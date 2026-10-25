#!/usr/bin/env bash
# M2 acceptance: one reproducible command for the five-scenario vertical slice.
#
# Runs, in order:
#   1. the normal offline verifier   (uv run pytest + pinned-Ictus conformance gate)
#   2. the success/restart slice     (Issue -> WO -> INITIAL ALLOW -> Dagster -> IMPLEMENTED)
#   3. the execution-failure scenarios (transient, terminal, escalation, duplicate)
#
# Requires TACTUS_ICTUS_ROOT to point at the pinned Ictus checkout with its
# existing venv (dagster + ictus_dagster) and prebuilt target/debug/ictus.
# Every runtime step uses a temporary SQLite ledger, DAGSTER_HOME and workspace
# root; nothing under the repository is written.
#
# Usage:
#   TACTUS_ICTUS_ROOT=/path/to/agentic-control scripts/run-acceptance.sh
set -euo pipefail

cd "$(dirname "$0")/.."

: "${TACTUS_ICTUS_ROOT:?set TACTUS_ICTUS_ROOT to the pinned Ictus checkout}"
export TACTUS_ICTUS_ROOT
# Provisioning is deterministic and offline: the uv cache carries the locked
# dependencies, so no network is required (see the operator notes in
# docs/architecture/END_TO_END_EXECUTION_VALIDATION.md).
export UV_OFFLINE="${UV_OFFLINE:-1}"

evidence_dir="${1:-/tmp}"

echo "== 1/3 verifier (pytest + pinned Ictus conformance) =="
./scripts/verify-goal.sh

echo "== 2/3 success/restart slice =="
./scripts/run-execution-slice.sh \
  --evidence "${evidence_dir}/tactus-execution-slice-evidence.json"

echo "== 3/3 execution-failure scenarios =="
./scripts/run-failure-scenarios.sh \
  --evidence "${evidence_dir}/tactus-failure-scenarios-evidence.json"

echo "ACCEPTANCE_PASS"
