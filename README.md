# Tactus

Tactus is the WorkOrder control plane and composition repository for a local-first
system with Ictus decisions and Dagster durable execution.

## Architecture Baseline v1

**Tactus governs work. Ictus decides. Dagster executes durably. Agents perform the task.**

- Tactus owns WorkOrder state, readiness, dependencies, claims, approvals records,
  admission, correlation and application of validated decisions/results.
- Ictus owns policy, capability validation, backend/model routing, approval
  requirements and semantic recovery. Its generic core remains independent.
- Dagster owns runs/steps, execution queues/concurrency, retries and workers.
- Runtime adapters supervise processes; Stax manages authorized workspace and
  change/PR publication. Metaxy is optional artifact/data lineage.

Start with the [canonical architecture](docs/architecture/SYSTEM_ARCHITECTURE.md),
[reconciliation](docs/architecture/BASELINE_V1_RECONCILIATION.md) and
[GitHub roadmap](docs/ROADMAP.md).

## Implementation status

Main contains a pure five-state WorkOrder domain model and dependency graph,
in-memory execution admission/correlation, backend descriptors/health/quota facts,
and inbound observation/outbound StateSnapshot adapters. Durable domain storage,
complete initial decision context, trusted validated-decision application and
the persistent end-to-end execution path remain implementation work.

Decision application code exists on a Fleet integration ref and requires review
and completion before merge. The first vertical slice uses a deterministic local
subprocess. Real agents and Stax follow in M3; directory placeholders do not
establish that those integrations are installed.

## Collaboration

```text
Architecture → GitHub Issue → human branch OR Fleet WorkOrder + branch
             → PR → review → main
```

Issues are shared intent. Fleet WorkOrders derive from a checked Issue revision.
Only explicit `fleet:ready` issues may be ingested after intake gates are upgraded;
see [migration/intake](docs/architecture/MIGRATION_FROM_FLEET.md).

## Development

Python 3.12+ and uv. `uv run pytest` runs the existing suite;
`scripts/verify-goal.sh` is the repository gate.

See [Contributing](CONTRIBUTING.md) and [Dependencies](docs/DEPENDENCIES.md).
