#!/usr/bin/env bash
# Tactus verifier — architecture/bootstrap stage.
#
# Tactus owns documentation, composition contracts and integration adapters;
# there is no orchestration implementation to test yet. This gate therefore
# checks repository hygiene and required documentation only, and is
# deliberately minimal so it can be extended as real subsystems land.
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
if grep -rInE --exclude-dir=.git \
     -e '-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----' \
     -e 'gh[pousr]_[A-Za-z0-9]{20,}' . ; then
  echo "TACTUS_VERIFIER_FAIL: possible secret material detected" >&2
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  exit 1
fi

echo "TACTUS_VERIFIER_PASS"
