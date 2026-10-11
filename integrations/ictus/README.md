# ictus

Ictus owns typed decisions, policy, capability validation, routing and approval requirements. Tactus persists human grants/interventions and applies validated effects. Ictus remains an independent repository.

The outbound Tactus → Ictus boundary is a **versioned** `StateSnapshot` profile
(`snapshot.profile_version = 2`, phases `INITIAL`/`RECOVERY`, canonical semantic
count/limit, raw backend and authorization facts, deterministic digest). See
[the contract](../../docs/architecture/ICTUS_STATE_SNAPSHOT_CONTRACT.md). The
inbound `ExecutionObservation` boundary is strict and fail-closed.

## Real-boundary conformance

`conformance.py` runs the deterministic fixtures through the **actual** pinned
Ictus binary (no Python policy mirror). It verifies the exact source revision
and builds `ictus` into a disposable target directory:

```sh
TACTUS_ICTUS_ROOT=/path/to/agentic-control \
  uv run python integrations/ictus/conformance.py \
  tests/integrations/ictus/data/initial_snapshot.v2.json \
  tests/integrations/ictus/data/recovery_snapshot.v2.json
```

| Item | Value |
|---|---|
| Pinned Ictus repository | `Walsamer/ictus` |
| Pinned Ictus commit | `f4581f94906d07decd3aca6ad3e83ae42e6cf9ae` |
| Overrides | `TACTUS_ICTUS_ROOT`, `TACTUS_ICTUS_TARGET_DIR` |

The normal test entry point (`uv run pytest`) executes the same boundary; a
missing or off-pin checkout fails visibly instead of skipping.

## GitHub Actions

`.github/workflows/ci.yml` provisions the boundary rather than skipping it:

1. checks out Tactus;
2. checks out `Walsamer/ictus` at the exact pinned commit into `.ictus/`;
3. verifies `git -C .ictus rev-parse HEAD` against the pin before any build;
4. installs a stable Rust toolchain and runs `cargo fetch --locked` so the
   harness's `cargo build --offline --locked` can resolve from the local cache;
5. exports `TACTUS_ICTUS_ROOT=.ictus` and
   `TACTUS_ICTUS_TARGET_DIR=.ictus/target` for `uv run pytest` and
   `scripts/verify-goal.sh`.

The conformance build writes only to the isolated `TACTUS_ICTUS_TARGET_DIR`
(`.ictus/target`, which Ictus gitignores); the pinned checkout itself stays
clean. A missing checkout, a pin mismatch, a build failure, a non-JSON envelope
or a decision-contract regression fails the job; nothing is silently skipped.

See [Architecture Baseline v1](../../docs/architecture/SYSTEM_ARCHITECTURE.md).
