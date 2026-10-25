#!/usr/bin/env bash
# Run the bounded execution-failure scenarios (TCT-WO-005).
#
# Requires TACTUS_ICTUS_ROOT to point at the pinned Ictus checkout with its
# existing venv (dagster + ictus_dagster) and prebuilt target/debug/ictus.
# Override the interpreter with TACTUS_ICTUS_PYTHON if the venv lives elsewhere.
#
# The scenarios use a Tactus-owned capability code location; the production
# Ictus bridge (submission/receipts/result mapping) is unchanged.
set -euo pipefail

cd "$(dirname "$0")/.."

: "${TACTUS_ICTUS_ROOT:?set TACTUS_ICTUS_ROOT to the pinned Ictus checkout}"
python_bin="${TACTUS_ICTUS_PYTHON:-${TACTUS_ICTUS_ROOT}/.venv/bin/python}"

exec "${python_bin}" scripts/validate_failure_scenarios.py "$@"
