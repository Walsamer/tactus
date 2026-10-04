# Tactus → Ictus `StateSnapshot` v1 Context Adapter

## Purpose

Ictus's `DecisionProvider` consumes a `StateSnapshot`, not an
`ExecutionObservation` directly. This document defines the **outbound**,
versioned adapter at which Tactus assembles one valid, domain-neutral Ictus
`StateSnapshot` v1 from execution observations plus Tactus/domain facts.

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
        + WorkOrder / domain facts
        + attempt / recovery history
        + backend availability facts
        + constraints / authorization
        ↓
Tactus StateSnapshot v1 adapter     (this edge: assemble facts, decide nothing)
        ↓
Ictus DecisionProvider              (owns all decision/policy)
```

- Tactus owns the Work Order lifecycle, domain facts and the assembly of the
  snapshot.
- Ictus owns whatever consumes the snapshot — diagnosis, policy, retrieval,
  reroute/decompose/escalate decisions. None of that lives here.
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
| Supported `schema_version` | `1` |

Tactus emits exactly `schema_version = 1`.

## Payload shape (v1)

Required: `schema_version`, `snapshot_id`, `timestamp`, `domain`, `subject`.

Optional: `facts`, `capabilities`, `constraints`.

```json
{
  "schema_version": 1,
  "snapshot_id": "snap-0001",
  "timestamp": "2026-10-01T00:00:00Z",
  "domain": "software",
  "subject": { "type": "work_order", "id": "WO-1" },
  "facts": [
    { "key": "observation.category", "value": "WORKER_TIMEOUT" },
    { "key": "work_order.id", "value": "WO-1" },
    { "key": "work_order.state", "value": "ACTIVE" },
    { "key": "attempt.number", "value": 1 },
    { "key": "recovery.semantic_attempts", "value": 3 },
    { "key": "capability.id", "value": "demo.verify" },
    { "key": "backend.available_candidates", "value": ["backend-a", "backend-b"] },
    { "key": "backend.previous_backend", "value": "backend-a" },
    { "key": "domain.dependencies_satisfied", "value": true },
    { "key": "domain.scope_constraints", "value": ["src/**"] }
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
- `facts` is an ordered list of `{ key, value }` entries. `value` is open JSON,
  which is what lets domain detail travel without changing the Ictus core.
- `capabilities` is the set of capability ids advertised as available in this
  state; Tactus mirrors `capability.id` there when a capability is supplied.
- `constraints` carries domain-neutral constraint tokens such as
  `approval_required`.

## Fact vocabulary

All keys use the dotted, domain-neutral namespace the Ictus core expects. The
adapter's declared vocabulary is:

| Key | Type | Source | Meaning |
|---|---|---|---|
| `observation.category` | string (Ictus v1 category) | execution observation | The execution outcome, copied **verbatim**; never reclassified. |
| `observation.message` | string | execution observation | Free-text execution detail (omitted when absent). |
| `observation.observation_id` | string | execution observation | Observation identity. |
| `observation.execution_id` | string | execution observation | Execution/run identity. |
| `observation.intent_id` | string | execution observation | Validated execution-intent reference. |
| `observation.observed_at` | string (date-time) | execution observation | When the execution outcome was observed. |
| `observation.retryable` | boolean | execution observation | Backend advisory hint only; omitted when absent. |
| `observation.evidence` | array of evidence objects | execution observation | Preserved evidence pointers (`kind`, `uri`, optional `sha256`/`note`); omitted when empty. |
| `work_order.id` | string | Work Order | Stable Work Order identity. |
| `work_order.state` | string | Work Order | One of `DRAFT`, `OPEN`, `ACTIVE`, `IMPLEMENTED`, `RETIRED`. |
| `work_order.readiness` | string | Work Order | `UNKNOWN`/`READY`/`BLOCKED`; present only while `OPEN`. |
| `attempt.number` | integer | attempt history | **Dagster micro-retry** attempt index (backend-owned). |
| `recovery.semantic_attempts` | integer | recovery history | **Semantic** execution-attempt / recovery count. |
| `capability.id` | string | capability facts | Capability to (re-)execute. |
| `backend.available_candidates` | array of strings | backend facts | Backends available as candidates; no ranking. |
| `backend.previous_backend` | string | backend facts | Backend previously used, if any (omitted otherwise). |
| `domain.dependencies_satisfied` | boolean | domain facts | Dependency-satisfaction fact (omitted when unknown). |
| `domain.scope_constraints` | array of strings | domain facts | Declared scope constraints for the Work Order. |

The stable key constants are exported from
`tactus.integrations.ictus.state_snapshot`.

## Two attempt counters — never conflated

There are two categorically different counters and the adapter keeps them on
separate keys:

```text
attempt.number               -> Dagster micro-retry attempt index
                                (backend-owned, carried verbatim)

recovery.semantic_attempts   -> semantic execution-attempt / recovery count
                                (Tactus decision-relevant)
```

- A Dagster step retry is **not** a new semantic execution attempt. The adapter
  never substitutes one count for the other, never sums them and never derives
  one from the other.
- `attempt.number` is an opaque backend fact; Tactus derives no execution
  semantics from it.
- `recovery.semantic_attempts` counts the authoritative Tactus execution
  attempts / recovery cycles.

## Execution observation is included, not reclassified

- `observation.category` is copied exactly from the validated inbound
  `ExecutionObservation`, so the closed Ictus v1 vocabulary
  (`SUCCESS`, `WORKER_TIMEOUT`, `PROCESS_CRASH`, `VERIFICATION_FAILURE`,
  `INTEGRATION_CONFLICT`, `RESOURCE_EXHAUSTED`, `PROVIDER_UNAVAILABLE`,
  `UNKNOWN`) is never widened or narrowed by this adapter.
- Tactus/domain facts (dependency state, scope constraints, backend
  availability, ...) are emitted as their own `facts` keys. They are **never**
  disguised as an execution category, and there is no domain-to-category
  mapping and no `UNKNOWN` fallback used to smuggle a domain concept through
  execution.

## Constraints and scope

Two distinct concepts are kept separate:

- `constraints` (top-level, domain-neutral tokens) — Ictus-level authorization
  / policy gates such as `approval_required`.
- `domain.scope_constraints` (a fact) — the concrete scope declared for the
  Work Order, e.g. `src/**`, `tests/**`.

## Stop-condition / representability note

The Ictus v1 `fact.value` is open JSON, so every Tactus/domain fact listed above
has a valid slot and nothing is dropped or invented. There is currently **no**
required fact that lacks a valid `StateSnapshot` v1 representation.

## Tests

`tests/integrations/ictus/test_state_snapshot.py`:

- **contract**: the emitted payload is structurally validated against the shape
  of the authoritative `contracts/state-snapshot.schema.json` (validation is
  encoded locally because `jsonschema` is deliberately not a Tactus
  dependency; the external schema is not vendored);
- **no reclassification**: `observation.category` is copied verbatim across the
  whole v1 vocabulary and never replaced by a domain fact;
- **counter separation**: the Dagster micro-retry index and the semantic
  attempt count move independently;
- **purity**: no Work Order/lifecycle mutation, no persistence object leakage,
  and no recovery-decision surface.

## Implementation

`src/tactus/integrations/ictus/state_snapshot.py` — `build_state_snapshot()`,
`AttemptHistory`, `BackendFacts` and the fact-key constants.
