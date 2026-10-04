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
