#!/usr/bin/env bash
# Run the bounded Tactus/Ictus/Dagster agent-attempt slice.
#
# Proves the join: a checked WorkOrder obtains a real pinned-Ictus decision for
# `software.implement`, Dagster durably runs it, and the Tactus-owned code
# location executes one supervised agent attempt under the operator-owned
# runtime pin, with the recorded patch checked against the original workspace and
# the terminal result applied back to the WorkOrder.
#
# The pinned runtime is the deterministic FAKE agent: no model provider, no
# credentials, no publication authority, no OS isolation. Issue #22 remains
# undone until those gates are closed.
#
# Requires TACTUS_ICTUS_ROOT to point at the pinned Ictus checkout with its
# existing venv (dagster + ictus_dagster) and a prebuilt target/debug/ictus. The
# checkout must be clean at the pin; --allow-dirty-ictus exists only for
# throwaway B4 experiments and marks the evidence accordingly.
set -euo pipefail

cd "$(dirname "$0")/.."

: "${TACTUS_ICTUS_ROOT:?set TACTUS_ICTUS_ROOT to the pinned Ictus checkout}"
python_bin="${TACTUS_ICTUS_PYTHON:-${TACTUS_ICTUS_ROOT}/.venv/bin/python}"

exec "${python_bin}" scripts/validate_agent_slice.py "$@"
