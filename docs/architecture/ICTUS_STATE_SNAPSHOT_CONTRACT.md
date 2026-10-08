> **Implemented versioned adapter reference.** [Baseline v1 contracts](CROSS_SYSTEM_CONTRACTS.md)
> govern new work; [reconciliation](BASELINE_V1_RECONCILIATION.md) records the
> profile, initial-context and validation gaps this adapter closes. The pinned
> Ictus schemas describe inspected main (Ictus commit `833175d`).

# Tactus → Ictus `StateSnapshot` versioned context adapter

## Purpose

Ictus's `DecisionProvider` consumes a `StateSnapshot`, not an
`ExecutionObservation` directly. This document defines the **outbound**,
versioned adapter at which Tactus assembles one valid, domain-neutral Ictus
`StateSnapshot` v1 for **first execution** (`INITIAL`) and **recovery**
(`RECOVERY`) from domain/control facts.

The adapter transports facts to the decision plane. It decides nothing and
mutates nothing.

It is the outbound sibling of the inbound execution-observation boundary
defined in
[`ICTUS_OBSERVATION_CONTRACT.md`](ICTUS_OBSERVATION_CONTRACT.md): execution
facts cross into Tactus there, and execution plus domain/control facts cross
out to Ictus here.

## Ownership boundary (normative)

```text
ExecutionObservation v1             (inbound: Ictus -> Tactus, validated)
        + WorkOrder / source / revision facts
        + fenced execution-attempt correlation
        + canonical semantic count/limit + diagnostic step retry
        + raw backend descriptors / health / administrative enablement / quota facts
        + raw approval grants/evidence + constraints
        ↓
Tactus StateSnapshot adapter        (this edge: assemble facts, decide nothing)
        ↓
Ictus DecisionProvider              (owns all decision/policy)
```

- Tactus owns the Work Order lifecycle, domain facts and the assembly of the
  snapshot.
- Ictus owns whatever consumes the snapshot — diagnosis, policy, retrieval,
  reroute/decompose/escalate decisions, backend compatibility/freshness and
  route selection. None of that lives here.
- The adapter is **pure**: it reads the public, read-only views of the domain
  entity and returns a fresh JSON-serializable `dict`. It performs no Work
  Order mutation, no lifecycle transition, no readiness change, no persistence,
  no scheduler/recovery dispatch and no backend calls.
- Tactus persistence models never appear in the snapshot. Only primitives,
  lists and mappings are emitted.

## Contract pin

| Item | Value |
|---|---|
| Ictus repository | `Walsamer/ictus` |
| Pinned commit | `833175d` |
| `StateSnapshot` schema | `contracts/state-snapshot.schema.json` |
| Supported generic `schema_version` | `1` |
| Tactus fact-profile version | `2` (independent, `snapshot.profile_version`) |

The generic Ictus schema version stays `1`. The *Tactus fact profile* is
versioned independently (Baseline v1): a consumer rejects an unsupported
profile rather than guessing. `validate_snapshot_profile()` is the
consumer-side guard.

## Phases

| Fact | Phase | Observation |
|---|---|---|
| `snapshot.phase = INITIAL` | first execution | **none** — a previous/fake observation is never fabricated. |
| `snapshot.phase = RECOVERY` | recovery after a failure | exactly one validated, **correlated** observation. |

For `RECOVERY`, Tactus correlates the observation against the authoritative
[`ExecutionAttempt`](../../src/tactus/domain/admission.py):

- Work Order identity ↔ `attempt.work_order_id` (**subject**);
- `observation.intent_id` ↔ `attempt.intent_id` (**intent**);
- `observation.execution_id` ↔ `attempt.dagster_run_id` (**attempt/run**).

A mismatch, a missing attempt, or a `SUCCESS` observation fails closed.

## Payload shape (v1)

Required: `schema_version`, `snapshot_id`, `timestamp`, `domain`, `subject`.

Optional: `facts`, `capabilities`, `constraints`.

```json
{
  "schema_version": 1,
  "snapshot_id": "snap-recovery-0001",
  "timestamp": "2026-10-01T00:00:00Z",
  "domain": "software",
  "subject": { "type": "work_order", "id": "WO-1" },
  "facts": [
    { "key": "snapshot.profile_version", "value": 2 },
    { "key": "snapshot.phase", "value": "RECOVERY" },
    { "key": "observation.category", "value": "WORKER_TIMEOUT" },
    { "key": "recovery.attempt_id", "value": "attempt-0001" },
    { "key": "recovery.intent_id", "value": "intent-0001" },
    { "key": "work_order.id", "value": "WO-1" },
    { "key": "work_order.state", "value": "ACTIVE" },
    { "key": "recovery.semantic_attempts", "value": 1 },
    { "key": "recovery.max_semantic_attempts", "value": 3 },
    { "key": "execution.step_retry_index", "value": 2 },
    { "key": "backend.descriptors", "value": [ { "backend_id": "backend-a" } ] },
    { "key": "authorization.grants", "value": [ { "grant_id": "grant-0001" } ] },
    { "key": "snapshot.digest", "value": "ecf6c114…" }
  ],
  "capabilities": ["demo.verify"],
  "constraints": ["approval_required"]
}
```

- `subject.type` is always `work_order`; `subject.id` is the Work Order id.
- `domain` defaults to `software` (the Tactus software-engineering control
  plane) and is overridable.
- `timestamp` is an RFC 3339 / ISO 8601 UTC instant; naive datetimes are
  interpreted as UTC.
- `snapshot_id` is caller-supplied or generated fresh.
- `snapshot.digest` is a SHA-256 over the canonical (sorted-key, compact,
  UTF-8) JSON body *without* the digest fact. With an explicit `snapshot_id`
  and `timestamp` the whole payload — including the digest — is deterministic.
- `facts` is an ordered list of `{ key, value }` entries with unique keys.
- `capabilities` mirrors `capability.id` when a capability is supplied;
  `constraints` carries domain-neutral tokens such as `approval_required`.

## Canonical fact vocabulary

### Profile / identity

| Key | Type | Meaning |
|---|---|---|
| `snapshot.profile_version` | integer | Tactus fact-profile version (currently `2`). |
| `snapshot.phase` | string | `INITIAL` or `RECOVERY`. |
| `snapshot.digest` | string | SHA-256 of the canonical body. |
| `work_order.id` / `work_order.state` / `work_order.readiness` | string | Work Order identity and lifecycle/readiness views. |
| `work_order.revision` | integer | Tactus-owned optimistic Work Order revision (optional). |
| `source.revision` | string | Immutable source revision bound to the context (optional). |

### Execution observation (RECOVERY only)

`observation.category`, `observation.message`,
`observation.observation_id`, `observation.execution_id`,
`observation.intent_id`, `observation.observed_at`,
`observation.retryable`, `observation.evidence` — copied verbatim from the
validated inbound `ExecutionObservation`; the closed Ictus v1 category
vocabulary is never widened, narrowed or reclassified.

`recovery.attempt_id` / `recovery.intent_id` bind the correlated attempt.

### Canonical budget facts — not the legacy retry facts

| Key | Type | Meaning |
|---|---|---|
| `recovery.semantic_attempts` | integer | Already accepted semantic attempts (zero before the first; includes the failed current attempt during recovery). |
| `recovery.max_semantic_attempts` | integer | Authorized total accepted-attempt limit. |
| `execution.step_retry_index` | integer | Execution-owned (Dagster) diagnostic step-retry index only. |

Another attempt is permitted only while `count < limit`. Missing or invalid
budget is a hard validation failure (`StateSnapshotContractError`), never an
implicit zero or unlimited budget. The step-retry index can never consume or
refill the semantic allowance by substitution.

### Raw backend facts (never a filtered candidate list)

| Key | Value |
|---|---|
| `backend.descriptors` | Raw descriptors: stable `backend_id`, `capabilities`, optional `provider`/`model`/`agent_runtime`/`effort`. |
| `backend.status` | Timestamped observed-health facts: `status` (`AVAILABLE`/`UNAVAILABLE`), `observed_at`, optional `expires_at`/`reason`. |
| `backend.administrative_enablement` | Operator/admin enablement, independent of observed health: `backend_id`, `enabled`, optional `provenance`/`observed_at`/`expires_at`/`reason`. Emitted only when present. |
| `backend.quota` | Externally reported provider-capacity facts: optional `limit`/`unit`/`source`, `observed_at`, optional `expires_at`/`reason`. |
| `backend.previous_backend` | Backend previously used, if any. |

Tactus supplies these facts without policy-filtering a preferred candidate
list. Ictus decides compatibility, freshness suitability and deterministic
tie-breaking.

### Raw authorization evidence

| Key | Value |
|---|---|
| `authorization.grants` | Raw approval grants: `grant_id`, `scope`, `actor`, `issued_at`, optional `expires_at`/`source_revision` and `evidence` refs. |

Tactus records grants; it never asserts that a grant is sufficient or that an
`ALLOW` was issued. Ictus validates their sufficiency.

### Domain / control facts

| Key | Type | Meaning |
|---|---|---|
| `capability.id` | string | Capability to (re-)execute (mirrored in `capabilities`). |
| `domain.dependencies_satisfied` | boolean | Dependency-satisfaction fact (omitted when unknown). |
| `domain.scope_constraints` | array of strings | Declared scope constraints for the Work Order. |

## Two counters — never conflated

```text
recovery.semantic_attempts / recovery.max_semantic_attempts
    canonical, decision-relevant semantic count/limit

execution.step_retry_index
    execution-owned diagnostic (Dagster); never a semantic attempt

attempt.number / retry.attempt / retry.budget
    SUPERSEDED legacy v1 profile inputs; never emitted by this adapter
```

- A Dagster step retry is **not** a new semantic execution attempt. The adapter
  never substitutes one count for the other, never sums them and never derives
  one from the other.
- Legacy inputs are translated only **explicitly** by
  `translate_legacy_budget_facts()` (and `semantic_budget_from_legacy()` for the
  Tactus `AttemptHistory` shim). The former mismatch — missing legacy facts
  defaulting to `0`, so an exhausted `0/0` budget blocked first execution, or
  the step-retry index masquerading as the semantic count — is covered by the
  shared fixtures.

## Stop-condition / representability note

The Ictus v1 `fact.value` is open JSON, so every Tactus/domain fact listed above
has a valid slot and nothing is dropped or invented.

## Tests

`tests/integrations/ictus/`:

- `test_state_snapshot.py` — schema shape, phases, correlation rejection,
  canonical budget facts, raw backend/authorization facts, deterministic
  digest, profile validation, fail-closed input validation and purity;
- `test_profile_fixtures.py` and `data/*.json` — cross-repository pinned
  fixtures run through a schema/profile boundary and through the documented
  policy rule, exposing the former `retry.attempt`/`retry.budget` mismatch.

## Implementation

`src/tactus/integrations/ictus/state_snapshot.py` — `build_state_snapshot()`,
`SemanticBudget`, `StepRetryDiagnostic`, `SnapshotPhase`,
`validate_snapshot_profile()`, `compute_snapshot_digest()` and the fact-key
constants. Domain fact adapters live in
`src/tactus/integrations/ictus/facts.py`.
