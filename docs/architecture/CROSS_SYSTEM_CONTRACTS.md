# Cross-system contracts

Normative part of [Architecture Baseline v1](SYSTEM_ARCHITECTURE.md). These are
transport-independent requirements, not claims that new schemas already exist.
Ictus `contracts/` remains the generic wire-format authority. Tactus owns its
domain adapter and provenance envelope. Keep stdio for local decision calls;
no HTTP, event bus or service topology is mandated.

## Contract inventory

| Contract | Producer → consumer | Required meaning |
|---|---|---|
| WorkSourceRevision | GitHub adapter → Tactus/current Fleet | Immutable source specification and change detector; see migration document. |
| DecisionContext / StateSnapshot profile | Tactus → Ictus | Generic subject, snapshot identity/revision, phase INITIAL or RECOVERY, facts with provenance, authorization and constraints. |
| ValidatedDecision / RecoveryDecision | Ictus → Tactus | Typed effect plus policy/capability validation and binding to exact context; no direct state mutation. |
| ExecutionIntent | Ictus → Tactus admission → Dagster bridge | Exact allowed capability, selected route and restrictions bound to validated context. |
| SubmissionReceipt / ExecutionStatus | Dagster bridge → Tactus | Durable receipt or explicit rejection/unknown; opaque execution ID with submission identity. |
| ExecutionResult / ExecutionObservation | Execution bridge → Tactus | Correlated terminal facts/evidence; no domain lifecycle decision. |
| HumanResolution | authenticated operator → Tactus | Request, actor, typed permitted action, bounded grant and evidence. |

## Snapshot profile

Retain the generic schema (subject, facts, capabilities, constraints). Version the
Tactus fact profile independently and reject unsupported profiles. Define initial
context with no fabricated previous observation. Recovery context includes only
validated, correlated observations. Both include WorkOrder revision, source body
digest/revision, active attempt where applicable, scope, dependency facts, budget
authorization, approval grants and backend descriptors/observations.

Backend facts carry stable IDs, capability/provider/runtime/model descriptors,
administrative enablement, timestamped health, expiry and provider quota facts.
Tactus supplies these facts without policy-filtering a preferred candidate list.
Ictus decides compatibility, freshness suitability and route; deterministic
tie-breaking is part of Ictus policy.

Canonical fact semantics for the new profile:

- `recovery.semantic_attempts`: number of already accepted semantic attempts;
  zero before the first; includes the failed current attempt during recovery.
- `recovery.max_semantic_attempts`: authorized total accepted-attempt limit.
  Another attempt is permitted only when count < limit. Missing/invalid budget
  is a typed validation failure, not an implicit zero/default unlimited budget.
- `execution.step_retry_index`: execution-owned diagnostic count only. It cannot
  consume or refill the semantic attempt allowance by substitution.

Existing `retry.attempt`/`retry.budget` policy facts and Tactus `attempt.number`
are legacy profile inputs. A compatibility adapter must explicitly translate
known versions and test off-by-one behavior; never guess their meaning.

## Validated decisions and intents

At minimum retain schema/profile versions, decision ID, proposal ID, subject,
snapshot ID and digest/revision, semantic attempt ID when applicable, policy
version/verdict, capability validation result, route constraints/selection,
approval references, permitted effect, issued/expiry time and evidence refs.
ExecutionIntent additionally binds capability arguments, authorized scope,
limits, backend/provider/runtime/model and idempotency identity.

Only an ALLOW verdict with sufficient current approval grants can yield an
executable intent. DENY and REQUIRE_APPROVAL have no executable intent. A typed
no-route/invalid-context outcome carries reasons for a domain blocker; Tactus
does not reinterpret it as permission to try another backend. Approval
requirements and missing inputs remain explicit outputs, not exceptions that
cause a fallback.

Validation evidence must come from a trusted Ictus invocation or authenticated
boundary, with persisted immutable payload/digest. A self-asserted JSON `ALLOW`
or opaque ID is insufficient. M1 defines exact serialization/version bumps.
Tactus verifies binding, freshness, schema support and current domain authority;
it does not rerun Ictus selection policy. Unknown kinds/versions fail closed.

Keep the generic integration vocabulary: REEXECUTE (domain RETRY/REQUEUE), ROUTE
(REROUTE), DECOMPOSE (SPLIT_REPLAN), ESCALATE (ESCALATE_HUMAN), ABORT (RETIRE),
EXECUTE_CAPABILITY. RecoveryDecision is a validated semantic result envelope,
not a second competing enum with domain nouns in the Ictus core. Nonexecution
validation outcomes can produce BLOCKED readiness. RETRY v1 compatibility must
be deliberate and tested; do not assume every contract becomes version 2 when
only DecisionProposal changes.

## Execution bridge

Submission uses one stable `(WorkOrder, semantic_attempt, intent)` identity and
an immutable intent digest. A duplicate identical request returns the same
receipt; an identity reused with different content is rejected. A timeout means
UNKNOWN, not rejected. Reconcile before any replacement. Receipt/query/result
contracts must support a persistent instance and independent run worker;
`execute_in_process` remains a useful example, not the production submission API.

Result contains schema version, result/event identity, intent/attempt/run
correlation, terminal execution status, normalized observation, evidence digests,
timestamps and effect/termination information. Distinguish process exit from
verified domain success. Late/duplicate/mismatched results cannot mutate a newer
attempt. Dagster step retry notifications are not terminal semantic failures.

Shared Rust/Python/Tactus fixtures must cover initial execution, budget boundary,
no route, approval pending/revoked, unknown versions, stale/wrong subject, duplicate
results and denied proposal. Schema conformance alone is not behavioral proof.
