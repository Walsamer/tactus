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
| DecisionEnvelope / RecoveryDecision | Ictus → Tactus | Versioned validated decision: `schema_version` 1 (frozen capability-bearing shape) or 2 (conditional); binds subject, snapshot identity/revision/digest, proposal and policy identity/version/verdict, capability validation, approvals, the resolved outcome and an expiry; no direct state mutation. |
| ExecutionIntent | Ictus → Tactus admission → Dagster bridge | Exact allowed capability, selected route and restrictions bound to validated context. |
| SubmissionReceipt / ExecutionStatus | Dagster bridge → Tactus | Durable receipt or explicit rejection/unknown; opaque execution ID with submission identity. |
| ExecutionResult / ExecutionObservation | Execution bridge → Tactus | Correlated terminal facts/evidence; no domain lifecycle decision. |
| RuntimeSeam / RuntimeEvidence | Tactus code location → Ictus worker → Tactus runtime backend | Operator-owned seam selection (a `module:attribute` backend, the workspace descriptor by path, and the attempt's bounds) and the bounded attempt's evidence: outcome, typed failure kind, changed paths, `sha256:` patch digest and bytes, steps, exit status, `termination{requested,signals,confirmed}` and duration. Bounds live in exactly one place, the attempt request; the runtime pin carries identity only. |
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

Canonical fact semantics for the new profile. Ictus owns the fact-key namespace
(`crates/core/src/context.rs`), so these keys are Ictus's own and are emitted
verbatim — there is no translation layer between the two ends:

- `attempts.semantic`: number of already accepted semantic attempts;
  zero before the first; includes the failed current attempt during recovery.
- `attempts.max_semantic`: authorized total accepted-attempt limit.
  Another attempt is permitted only when count < limit. Missing/invalid budget
  is a typed validation failure, not an implicit zero/default unlimited budget.
- `attempts.step_retries`: execution-owned diagnostic count only. It cannot
  consume or refill the semantic attempt allowance by substitution.

Existing `retry.attempt`/`retry.budget` policy facts and Tactus `attempt.number`
are legacy profile inputs. Ictus still reads the `retry.*` pair as a complete
fallback for foreign payloads; Tactus never emits them, and its profile guard
rejects a snapshot that carries them, because one payload must not state the
same budget in two vocabularies. A compatibility adapter must explicitly
translate known versions and test off-by-one behavior; never guess their
meaning.

## Validated decisions and intents

At minimum retain schema/profile versions, decision ID, proposal ID, subject,
snapshot ID and digest/revision, semantic attempt ID when applicable, policy
version/verdict, capability validation result, route constraints/selection,
approval references, permitted effect, issued/expiry time and evidence refs.
ExecutionIntent additionally binds capability arguments, authorized scope,
limits, backend/provider/runtime/model and idempotency identity.

Admission is **outcome-first**, never verdict-first. `EXECUTABLE` is the only
outcome that carries an `ExecutionIntent` and may start new work. `NO_EXECUTION`
was validated and permitted and reaches no execution backend, so Tactus acts on
`proposal.decision` (ESCALATE and ABORT today; DECOMPOSE once its proposal
binding can be verified) through the same applier and decision ledger as any
other authorising decision. An executable intent must bind the validated
proposal digest and the snapshot it was validated against: an intent for a
different payload is not the result of that validation. `DENIED`, `PENDING`,
`NO_ROUTE` and `INVALID` are typed non-authorising results: the caller receives
the outcome, and no Work Order transition, intervention request or ledger row is
written. Reading the policy verdict instead of the resolved outcome is the bug
this contract exists to prevent — a `NO_EXECUTION` decision also carries
`ALLOW`/`MODIFY`. A malformed payload fails closed with an exception; a
well-formed refusal is not an exception. A refusal carries its resolved outcome,
the proposal's decision and the policy verdict — not Ictus's prose, which the
envelope does not carry — and a no-route or invalid-context outcome is never
reinterpreted as permission to try another backend.

The envelope is version tolerant: `schema_version` 1 and 2 are accepted and an
unknown version is refused. Version 1 is the frozen capability-bearing shape and
always carries `capability_validation`; version 2 carries it exactly when the
decision carries a capability, so ESCALATE, ABORT and DECOMPOSE carry none.
Whether a decision carries a capability, and whether it may carry a route,
approvals, grants or an intent, are shape rules derived from `proposal.decision`
— they are not version rules, and a decision that carries no capability must not
claim capability validation.

Expiry is compared to `validation.validated_at`, which Ictus derives from the
snapshot's fact time, never to a wall clock. A wall-clock liveness rule would
need its own explicitly injected clock and is not part of admission.

The decision ledger records a versioned digest: a new row is `sha256:v1:`
prefixed, an existing bare 64-hex row reads as `sha256:v0`, and comparing two
different versions is an immutable-record conflict rather than a replay.

The canonical form is Ictus's identity-digest protocol v1 (`docs/IDENTITY_DIGEST.md`,
`contracts/digest-vectors.json` `schema_version` 1), including its number domain:
an integer outside −2^63…2^64−1, a non-finite value, and a document literal that
only rounds to its double are refused with a typed error naming the rule and the
JSON path, never hashed as the nearest representable value. The rule is enforced
wherever a digest is computed and at the text ingress that parses Ictus output, so
a document Ictus would refuse cannot be digested here either. The published
vectors are mirrored in `tests/integrations/ictus/test_digest_vectors.py` until a
pin advance brings the artefact into the pinned revision.

DECOMPOSE is refused with a typed reason, and no child Work Order is created:
the bounded child plan is not carried in the envelope, and
`validation.proposal_digest` cannot be re-verified because Ictus currently
serializes the proposal in struct field order rather than canonical form.
Canonical digest vectors are required before a plan may be acted on.

The legacy unvalidated triple (`{proposal, policy_decision, execution_intent}`)
is **deprecated**, not removed: the same admission boundary still accepts it,
because plain `ictus decide` (no `--context`) still emits that shape and the
conformance harness's default report exercises it — CI runs that report and the
validated-envelope report side by side. It stays only until a `DecisionContext`
producer exists;
Tactus builds a context in evidence code, never at runtime.

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
