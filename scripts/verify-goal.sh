#!/usr/bin/env bash
# Tactus verifier — architecture/bootstrap stage.
#
# Tactus owns documentation, composition contracts and integration adapters.
# Since the first domain core has landed, this gate runs repository hygiene
# checks, required documentation checks and (when a Python project is present)
# the domain test suite. It stays deliberately small so it can be extended as
# real subsystems land.
set -euo pipefail

cd "$(dirname "$0")/.."

fail=0

# 1. No whitespace errors or conflict markers in the working tree diff.
if ! git diff --check; then
  echo "TACTUS_VERIFIER_FAIL: git diff --check" >&2
  fail=1
fi

# 2. Required documentation exists and is non-empty.
for f in README.md docs/ARCHITECTURE.md docs/DEPENDENCIES.md; do
  if [ ! -s "$f" ]; then
    echo "TACTUS_VERIFIER_FAIL: missing or empty $f" >&2
    fail=1
  fi
done

# 3. No obvious secret material in tracked files.
if grep -rInE \
     --exclude-dir=.git --exclude-dir=.venv --exclude-dir=__pycache__ \
     --exclude-dir=.pytest_cache --exclude-dir=.pixi \
     -e '-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----' \
     -e 'gh[pousr]_[A-Za-z0-9]{20,}' . ; then
  echo "TACTUS_VERIFIER_FAIL: possible secret material detected" >&2
  fail=1
fi

# 4. Python domain tests, when a Python project is present.
if [ -f pyproject.toml ]; then
  if command -v uv >/dev/null 2>&1; then
    if ! uv run --quiet pytest; then
      echo "TACTUS_VERIFIER_FAIL: pytest" >&2
      fail=1
    fi
  elif command -v python3 >/dev/null 2>&1 && python3 -c 'import pytest' 2>/dev/null; then
    if ! python3 -m pytest; then
      echo "TACTUS_VERIFIER_FAIL: pytest" >&2
      fail=1
    fi
  else
    echo "TACTUS_VERIFIER_FAIL: no Python test runner available" >&2
    fail=1
  fi
fi

if [ "$fail" -ne 0 ]; then
  exit 1
fi

echo "TACTUS_VERIFIER_PASS"
