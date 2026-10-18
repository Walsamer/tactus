#!/usr/bin/env bash
# Run the bounded real Tactus/Ictus/Dagster execution-slice validation.
#
# Requires TACTUS_ICTUS_ROOT to point at the pinned Ictus checkout with its
# existing venv and prebuilt target/debug/ictus binary. Override the interpreter
# with TACTUS_ICTUS_PYTHON if the venv lives elsewhere.
set -euo pipefail

cd "$(dirname "$0")/.."

: "${TACTUS_ICTUS_ROOT:?set TACTUS_ICTUS_ROOT to the pinned Ictus checkout}"
python_bin="${TACTUS_ICTUS_PYTHON:-${TACTUS_ICTUS_ROOT}/.venv/bin/python}"

exec "${python_bin}" scripts/validate_execution_slice.py "$@"
