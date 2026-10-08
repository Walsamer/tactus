> **Implemented v1 adapter reference, not the complete target contract.**
> [Baseline v1 contracts](CROSS_SYSTEM_CONTRACTS.md) govern new work;
> [reconciliation](BASELINE_V1_RECONCILIATION.md) records profile, initial-context
> and validation gaps. The pinned schemas here describe inspected main.

# Tactus ← Ictus Execution-Observation Contract

## Purpose

Execution outcomes must cross from the execution plane into the decision plane
as a domain-neutral **fact**, without leaking Tactus persistence internals and
without Tactus re-implementing Ictus. This document defines the versioned,
**inbound** compatibility boundary at which Tactus validates and normalizes
Ictus `ExecutionObservation` v1.

It is deliberately a one-way boundary: Ictus produces execution observations,
Tactus consumes them. Tactus never originates, fabricates or round-trips an
`ExecutionObservation` from Tactus-internal facts.

## Ownership boundary (normative)

```text
Dagster / backend
    ↓
Ictus execution layer
    ↓
ExecutionResult
    ↓
Ictus-owned ExecutionResult.to_observation()
    ↓
ExecutionObservation v1
    ↓
Tactus Ictus compatibility boundary  (validate + normalize, fail-closed)
    ↓
Tactus normalized execution-observation fact
```

- **Ictus owns execution semantics** and the `ExecutionResult` →
  `ExecutionObservation` transformation, including the Dagster result-mapping
  adapter.
- **Tactus consumes the observation contract** and validates/normalizes
  execution facts at its integration boundary.
- Tactus does **not** define `ExecutionResult`, does **not** implement
  `ExecutionResult.to_observation()`, and does **not** map Dagster runs. Those
  remain Ictus-owned; Tactus never vendors or re-implements them.

Tactus records and applies lifecycle state; Ictus owns diagnosis and policy.
This boundary transports facts only. It never decides whether to retry, never
mutates a Work Order, and never dispatches recovery.

## Two classes of fact — do not force domain facts through execution

Ictus's execution taxonomy is intentionally small and describes **execution
outcomes** only. Tactus also has **domain/control facts** that are not execution
outcomes at all. These are two different classes and must be transported by two
different surfaces:

```text
Execution facts     → ExecutionObservation  (this boundary)
Domain/control facts → StateSnapshot facts/context (FIX-005)
```

- **Execution facts** describe what happened while executing an intent:
  `SUCCESS`, `WORKER_TIMEOUT`, `PROCESS_CRASH`, `VERIFICATION_FAILURE`,
  `INTEGRATION_CONFLICT`, `RESOURCE_EXHAUSTED`, `PROVIDER_UNAVAILABLE`,
  `UNKNOWN`. They arrive (inbound) as an Ictus `ExecutionObservation` v1.
- **Domain/control facts** describe Tactus' own domain and control state, for
  example dependency state and resolution, task
  complexity/decomposability, request supersession, scope violations, and
  backend capacity/health. They are **not** execution observations.

Domain/control facts must **not** be narrowed into an `ExecutionObservation` and
must **not** be forced through `ExecutionObservation → UNKNOWN` merely because
Ictus's execution taxonomy cannot represent them. They belong on the
`StateSnapshot` facts/context surface consumed by the decision plane, which is
the subject of the StateSnapshot adapter (FIX-005): see
[`ICTUS_STATE_SNAPSHOT_CONTRACT.md`](ICTUS_STATE_SNAPSHOT_CONTRACT.md).

For example, the following are **domain/control facts, not execution
observations**, and are explicitly out of scope for this boundary:

```text
DEPENDENCY_RESOLVED
TASK_TOO_COMPLEX_SPLITTABLE
TASK_TOO_COMPLEX_NOT_SPLITTABLE
REQUEST_INVALID_OR_SUPERSEDED
SCOPE_VIOLATION
```

There is therefore **no Tactus-wide failure taxonomy** in this boundary mapped
onto Ictus observation categories, and no `UNKNOWN` fallback used to smuggle
domain concepts through execution.

## Contract pin

| Item | Value |
|---|---|
| Ictus repository | `Walsamer/ictus` |
| Pinned commit | `833175d` |
| `ExecutionObservation` schema | `contracts/observation.schema.json` |
| Supported `schema_version` | `1` |

Tactus supports exactly `schema_version = 1`. Any other version fails closed.

## Wire fields (v1)

Required: `schema_version`, `observation_id`, `execution_id`, `intent_id`,
`category`, `observed_at`.

Optional: `message`, `evidence`, `retryable`.

Each evidence entry is `{ kind, uri, sha256?, note? }`; `kind` and `uri` are
required, `sha256` and `note` optional.

The closed v1 `category` vocabulary is:

```text
SUCCESS
WORKER_TIMEOUT
PROCESS_CRASH
VERIFICATION_FAILURE
INTEGRATION_CONFLICT
RESOURCE_EXHAUSTED
PROVIDER_UNAVAILABLE
UNKNOWN
```

`UNKNOWN` is a valid Ictus category meaning Ictus could not classify the
execution outcome. It is distinct from an **unknown wire category**, which is
invalid input and fails closed; a future/unknown category is never silently
coerced to `UNKNOWN`.

## Nullable-field decision (v1)

**Decision: optional means "omitted", not "explicit `null`". A present `null`
is rejected as malformed.**

The authoritative contract (`contracts/observation.schema.json`) types the
optional fields as:

```text
message    : string
evidence   : array of evidence objects
retryable  : boolean
sha256     : string
note       : string
```

and declares no `null` alternative for any of them. The v1 wire encoding of
"not present" is therefore **omission**. Tactus is a strict, fail-closed
validator of the versioned wire contract, so it accepts an optional field only
when it is absent or has the declared type; an explicit `null` for `message`,
`evidence`, `retryable`, `sha256` or `note` is a malformed payload.

The Ictus Rust type models these as `Option<T>` with `#[serde(default)]`, which
happens to also deserialize `null` to `None`. That is an implementation
permissiveness, not part of the versioned JSON contract; Tactus validates the
authoritative schema, not a particular host-language deserializer, and does not
widen or extend the contract. Rejecting explicit `null` invents no new field and
changes no wire shape.

## Fail-closed compatibility

| Condition | Result |
|---|---|
| `schema_version` unsupported | `UnsupportedObservationVersionError` |
| incoming `category` outside v1 vocabulary | `UnknownObservationCategoryError` |
| missing / wrong-type / non-integer required field | `MalformedObservationError` |
| explicit `null` for a present optional field | `MalformedObservationError` |
| malformed `evidence` array or evidence entry | `MalformedObservationError` |

All derive from `ObservationContractError`.

## Evidence and provenance

Evidence references are preserved end-to-end (`kind`, `uri`, optional `sha256`,
optional `note`), including ordering across multiple entries. Evidence is an
opaque pointer only; no secret or domain payload is stored. Preservation is
inbound: because there is no outbound origination, the boundary does not
serialize an observation back to the Ictus wire (`to_ictus_payload`) and no
Ictus → Tactus → Ictus round-trip is required or provided.

## Purity

The boundary is pure: input payload → validation/normalization → output value.
It performs no Work Order mutation, no lifecycle transition, no persistence, no
scheduler invocation, no recovery dispatch and no backend calls. Tests assert
that normalization leaves a Work Order's lifecycle state, readiness, transition
records and failure observations unchanged.

## Implementation

`src/tactus/integrations/ictus/observation.py` — the only public entry point for
Ictus observation traffic.

## StateSnapshot adapter

The outbound complement of this boundary is the **versioned** pure
Tactus-to-Ictus `StateSnapshot` context adapter (Tactus fact profile `2`). It
assembles the domain-neutral state that an Ictus `DecisionProvider` consumes
for first execution (`INITIAL`, no fabricated observation) and recovery
(`RECOVERY`, one correlated observation), together with Work Order/source
facts, the accepted-attempt correlation, canonical semantic count/limit and raw
backend and authorization facts.

- It includes the execution observation **without reclassification**: the
  `observation.category` is copied verbatim and the closed v1 vocabulary is
  never widened or narrowed.
- Tactus/domain facts are emitted as their own snapshot facts; they are never
  disguised as execution categories.
- Recovery context is **correlated**: a wrong subject, intent or attempt/run —
  or a `SUCCESS` observation — fails closed.
- The canonical semantic count/limit (`recovery.semantic_attempts`,
  `recovery.max_semantic_attempts`) is kept strictly separate from the
  execution-owned diagnostic step-retry index (`execution.step_retry_index`).
  The superseded `attempt.number`/`retry.attempt`/`retry.budget` facts are never
  emitted and are only translated explicitly.
- The adapter is pure: no Work Order mutation, no lifecycle transition, no
  persistence-model leakage and no recovery-decision logic.

See [`ICTUS_STATE_SNAPSHOT_CONTRACT.md`](ICTUS_STATE_SNAPSHOT_CONTRACT.md) for
the payload shape and the full fact vocabulary.

## Relationship to issue #8

This boundary deliberately does **not** model recovery decisions. Ictus v1's
decision vocabulary (`RETRY`, `ABORT`, `ESCALATE`, `EXECUTE_CAPABILITY`) does
not yet include `REQUEUE_READY`, `REROUTE`, `BLOCK`, `SPLIT_REPLAN` or `RETIRE`.
That richer recovery-decision vocabulary belongs to issue #8 and requires a
deliberately scoped Ictus change, not an opportunistic extension of this
observation contract.
