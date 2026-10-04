# Tactus → Ictus Observation Contract

## Purpose

Execution outcomes must cross from the execution plane into the decision plane
as a domain-neutral **fact**, without leaking Tactus persistence internals and
without Tactus re-implementing Ictus. This document defines the versioned
compatibility boundary between Tactus and Ictus.

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
Tactus Ictus compatibility boundary
    ↓
Tactus normalized failure/recovery facts
```

- **Ictus owns execution semantics** and the `ExecutionResult` →
  `ExecutionObservation` transformation, including the Dagster result-mapping
  adapter.
- **Tactus consumes the observation contract** and normalizes/originates
  recovery facts at its integration boundary.
- Tactus does **not** define `ExecutionResult`, does **not** implement
  `ExecutionResult.to_observation()`, and does **not** map Dagster runs. Those
  remain Ictus-owned; Tactus never vendors or re-implements them.

Tactus records and applies lifecycle state; Ictus owns diagnosis and policy.
This boundary transports facts only. It never decides whether to retry, never
mutates a Work Order, and never dispatches recovery.

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

## Category mapping

Ictus v1 has a closed vocabulary. Tactus's internal failure classification
(`docs/architecture/TRIAGE_AND_RECOVERY.md`) is broader and maps onto it
**conservatively and many-to-one**. Tactus never invents new Ictus categories.

| Tactus failure classification | Ictus v1 category |
|---|---|
| `WORKER_TIMEOUT` | `WORKER_TIMEOUT` |
| `PROVIDER_UNAVAILABLE` | `PROVIDER_UNAVAILABLE` |
| `PROVIDER_QUOTA_EXHAUSTED` | `RESOURCE_EXHAUSTED` |
| `VERIFICATION_FAILURE` | `VERIFICATION_FAILURE` |
| `INTEGRATION_FAILURE` | `INTEGRATION_CONFLICT` |
| `TRANSIENT_RUNTIME_FAILURE` | `UNKNOWN` |
| `PROVIDER_API_TIMEOUT` | `UNKNOWN` |
| `EXECUTION_TIMEOUT` | `UNKNOWN` |
| `SCOPE_VIOLATION` | `UNKNOWN` |
| `DEPENDENCY_UNAVAILABLE` | `UNKNOWN` |
| `DEPENDENCY_RESOLVED` | `UNKNOWN` |
| `TASK_TOO_COMPLEX_SPLITTABLE` | `UNKNOWN` |
| `TASK_TOO_COMPLEX_NOT_SPLITTABLE` | `UNKNOWN` |
| `REQUEST_INVALID_OR_SUPERSEDED` | `UNKNOWN` |
| `UNKNOWN` | `UNKNOWN` |

## `UNKNOWN` semantics

`UNKNOWN` has exactly two, deliberately different, meanings:

- **Known Tactus classification with no precise Ictus equivalent** → mapped to
  `UNKNOWN`. This is a successful, conservative normalization.
- **Unknown incoming Ictus wire category** → **rejected, fail closed**. A
  category from a future/unknown Ictus version is never silently coerced to
  `UNKNOWN`.

## Fail-closed compatibility

| Condition | Result |
|---|---|
| `schema_version` unsupported | `UnsupportedObservationVersionError` |
| incoming `category` outside v1 vocabulary | `UnknownObservationCategoryError` |
| malformed / missing / wrong-type required field | `MalformedObservationError` |

All three derive from `ObservationContractError`.

## Evidence and provenance

Evidence references are preserved end-to-end (`kind`, `uri`, optional `sha256`,
optional `note`), and a normalized observation round-trips through the wire
representation. No secret or domain payload is stored — evidence is an opaque
pointer only.

## Purity

The boundary is pure: input fact → validation/normalization → output value. It
performs no Work Order mutation, no lifecycle transition, no persistence, no
scheduler invocation, no recovery dispatch and no backend calls. Tests assert
that normalization leaves a Work Order's lifecycle state, readiness, transition
records and failure observations unchanged.

## Implementation

`src/tactus/integrations/ictus/observation.py` — the only public entry point for
Ictus observation traffic.

## Relationship to issue #8

This boundary deliberately does **not** model recovery decisions. Ictus v1's
decision vocabulary (`RETRY`, `ABORT`, `ESCALATE`, `EXECUTE_CAPABILITY`) does
not yet include `REQUEUE_READY`, `REROUTE`, `BLOCK`, `SPLIT_REPLAN` or `RETIRE`.
That richer recovery-decision vocabulary belongs to issue #8 and requires a
deliberately scoped Ictus change, not an opportunistic extension of this
observation contract.
