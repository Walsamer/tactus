# ictus

Ictus owns typed decisions, policy, capability validation, routing and approval requirements. Tactus persists human grants/interventions and applies validated effects. Ictus remains an independent repository.

The outbound Tactus → Ictus boundary is a **versioned** `StateSnapshot` profile
(`snapshot.profile_version = 2`, phases `INITIAL`/`RECOVERY`, canonical semantic
count/limit, raw backend and authorization facts, deterministic digest). See
[the contract](../../docs/architecture/ICTUS_STATE_SNAPSHOT_CONTRACT.md). The
inbound `ExecutionObservation` boundary is strict and fail-closed.

See [Architecture Baseline v1](../../docs/architecture/SYSTEM_ARCHITECTURE.md).
