# Ictus

First-party external decision/policy layer. Independently versioned and
publishable.

- Typed decisions, policy, capability validation, approvals.
- Produces validated `ExecutionIntent`s consumed by the execution plane.
- Source lives in `Walsamer/ictus`; it is **not** copied into Tactus.

Tactus integrates with Ictus through an explicit contract:

```text
Tactus
   ↓
Ictus
   ↓
Dagster
```

The Tactus-side observation compatibility boundary (pinned to Ictus commit
`833175d`, `ExecutionObservation` `schema_version = 1`) is documented in
[`docs/architecture/ICTUS_OBSERVATION_CONTRACT.md`](../../docs/architecture/ICTUS_OBSERVATION_CONTRACT.md).
Ictus owns the Dagster adapter and the `ExecutionResult` →
`ExecutionObservation` transformation; Tactus only validates and normalizes the
wire contract at its edge.
