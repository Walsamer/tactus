# Decision Application — Generic Ictus Decisions → Tactus Work Order Effects

## Purpose

Ictus owns the generic, domain-neutral decision vocabulary, diagnosis and all
decision **policy**. Tactus owns the Work Order lifecycle/readiness, so Tactus —
and only Tactus — owns the **explicit mapping** from a validated generic Ictus
decision to a Work Order effect plus an auditable application record.

This replaces the older issue #8 `RecoveryDecision` wording. The generic
vocabulary is frozen by Ictus ADR `0001-generic-decision-vocabulary` (Ictus
FIX-006) and contains **no Tactus lifecycle nouns**:

```text
REEXECUTE | ROUTE | DECOMPOSE | ESCALATE | ABORT | EXECUTE_CAPABILITY
```

## Ownership

```text
Ictus   diagnosis, policy, capability validation, semantic routing,
        the generic vocabulary and the validated decision
Tactus  Work Order lifecycle/readiness, the explicit decision→effect mapping,
        human/domain coordination, the auditable application record
Dagster temporal/durable execution, run/step state, execution-level retries
```

Rules:

- Ictus never mutates a Work Order. It produces a validated decision; Tactus
  applies it.
- Tactus contains **no** decision policy: no diagnosis, no retry budget, no
  backend ranking/selection, no capability validation policy.
- A decision maps only to existing lifecycle states plus `OPEN` readiness. No new
  lifecycle state is invented.

## Explicit mapping

| Ictus decision | Guard | Tactus effect | Lifecycle / readiness result |
| --- | --- | --- | --- |
| `REEXECUTE` | `ACTIVE` | end attempt, authorize a new semantic attempt | `ACTIVE → OPEN + READY` |
| `ROUTE` | `ACTIVE` | end attempt, preserve generic route constraints | `ACTIVE → OPEN + READY` |
| `EXECUTE_CAPABILITY` | `ACTIVE` | end attempt, record validated follow-up capability | `ACTIVE → OPEN + READY` |
| `ESCALATE` | `ACTIVE` | create typed `HumanInterventionRequest` | `ACTIVE → OPEN + BLOCKED` |
| `ABORT` | `ACTIVE` | stop; no further action | `ACTIVE → RETIRED` |
| `DECOMPOSE` | `ACTIVE` | create bounded children durably, retire parent last | `ACTIVE → RETIRED` |

Every recovery decision requires an `ACTIVE` Work Order (a failure is observed
and triaged while the Work Order stays `ACTIVE`). The Work Order is validated
before any mutation, so a rejected application leaves it unchanged.

### New semantic attempt (`REEXECUTE` / `ROUTE` / `EXECUTE_CAPABILITY`)

The effect is the lifecycle reopen to `OPEN + READY`. Accepting the *new*
execution attempt is the separate, Ictus-validated execution-admission step
(`OPEN + READY → ACTIVE`); Tactus does not fabricate an execution intent.

`ROUTE` preserves the generic role constraints (`exclude_backend`,
`preferred_backend`, `required_provider`, `required_runtime`) on the audit
record. Tactus never selects, ranks or filters a backend from them.

### Human escalation (`ESCALATE`)

The effect is `OPEN + BLOCKED` plus a typed `HumanInterventionRequest` with a
reason from the Tactus-owned intervention vocabulary (default
`UNRESOLVED_RECOVERY`). The typed request is the reason carrier; the
WorkOrder-level generic typed `BlockReason` (issue #3) is an external
prerequisite that Tactus does not re-implement here.

### Decomposition (`DECOMPOSE`)

A bounded child plan (`ChildPlan`, with an explicit `max_children` bound) is
created through a durable child-creation capability. The parent is retired
**only after** durable creation succeeds and matches the plan; if creation fails
or does not match, the parent stays `ACTIVE`. Ictus itself creates no child work
items.

## Fail-closed

- An unknown decision token is rejected at the inbound boundary
  (`UnknownDecisionError`) and never reaches the mapping.
- Unsupported proposal `schema_version` values fail closed; the v2-only tokens
  `ROUTE`/`DECOMPOSE` are rejected under v1, and the legacy v1 `RETRY` token is
  normalized to `REEXECUTE`.
- Applying a recovery decision to a non-`ACTIVE` Work Order fails closed and
  leaves it unchanged.
- `DECOMPOSE` applied without a bounded plan or a durable creation capability
  fails closed and leaves the parent `ACTIVE`.

## Auditable application record

Every successful application emits an immutable `DecisionApplicationRecord`
holding the decision identity/kind, the explicit effect, the from/to lifecycle
state and readiness, the applied transition record(s), the preserved route
constraints, any intervention request and any created child ids. The
`DecisionApplier` keeps these records in an ordered audit ledger.

## Boundaries

- `src/tactus/integrations/ictus/decisions.py` — inbound, fail-closed validation
  of the decision-relevant part of the `DecisionProposal` payload.
- `src/tactus/domain/decisions.py` — Tactus's read-only mirror of the frozen
  vocabulary and the validated `SemanticDecision` value.
- `src/tactus/domain/decision_application.py` — the explicit mapping, effects
  and audit record.

Ictus-owned proposal metadata (`provider`, `subject`, `evidence`) is not
re-validated by Tactus: that is Ictus's contract.
