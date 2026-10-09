# Architecture

The canonical specification is [Architecture Baseline v1](architecture/SYSTEM_ARCHITECTURE.md).
Start there for ownership, state, contracts, execution/recovery, local operation
and migration. [Reconciliation](architecture/BASELINE_V1_RECONCILIATION.md) records
which existing code and issues need to converge. Earlier ownership tables were
replaced to keep one authoritative specification.

See also [Decision application](architecture/DECISION_APPLICATION.md) — the
explicit mapping from generic Ictus decisions
(`REEXECUTE`/`ROUTE`/`DECOMPOSE`/`ESCALATE`/`ABORT`/`EXECUTE_CAPABILITY`) to
Work Order lifecycle/readiness effects, fail-closed handling and the auditable
application record (TCT-WO-FIX-007).
