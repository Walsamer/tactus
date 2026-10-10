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
| Pinned Ictus commit | `e823000c9255b959c2c9516ed4ee85eb13150df2` |
| Overrides | `TACTUS_ICTUS_ROOT`, `TACTUS_ICTUS_TARGET_DIR` |

The normal test entry point (`uv run pytest`) executes the same boundary; a
missing or off-pin checkout fails visibly instead of skipping.

See [Architecture Baseline v1](../../docs/architecture/SYSTEM_ARCHITECTURE.md).
