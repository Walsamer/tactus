# Execution and recovery

Normative part of [Architecture Baseline v1](SYSTEM_ARCHITECTURE.md).

## Normal path

1. Ingest an approved Issue revision into a Tactus WorkOrder with provenance.
2. Admit the specification; compute OPEN readiness. Acquire a fenced domain claim.
3. Build initial context. Ictus validates proposal, policy and capability, chooses
   a route and returns a validated ExecutionIntent or typed nonexecution outcome.
4. Tactus rechecks source/domain revision, claim, approvals and exact route facts.
   In one transaction accept intent, create attempt, set ACTIVE and record dispatch.
5. Deliver through the single Dagster bridge with stable submission identity.
   Dagster durably records, queues and launches its worker. Record its receipt.
6. A workflow step invokes the bounded runtime adapter and collects verification
   evidence. Dagster owns step state, history and configured execution retries.
7. Tactus validates/deduplicates terminal result. Verified success closes the
   attempt and sets IMPLEMENTED, then recomputes dependent readiness.

## Two different kinds of recovery

| Event | Owner and action | WorkOrder effect |
|---|---|---|
| Transient network hiccup in a repeatable step | Dagster retries the step within approved execution limits | Same semantic attempt, stays ACTIVE. |
| Execution retries exhausted, persistent backend failure, unsuitable route, ambiguous task | Tactus records normalized observation/context; Ictus selects validated semantic recovery | Tactus applies the bound decision once. |
| Task too complex, approved decomposition needed | Ictus DECOMPOSE + validated bounded plan | Tactus safely materializes children. |
| Human judgment required | Ictus ESCALATE or approval requirement | Tactus persists intervention and blocks new admission. |
| Successful verified execution | Tactus applies success | IMPLEMENTED; no recovery-policy invocation. |

Ictus REEXECUTE requests a new semantic attempt after Tactus closes/reopens the
prior one. ROUTE authorizes a different compatible route through Ictus validation.
Neither is an in-process retry loop. Tactus records attempt counts/budget facts;
Ictus decides semantic allowance; Dagster accounts for execution repetition.
Step retries that repeat agent/provider calls still consume real resource budgets.

Current Ictus rules return ABORT on SUCCESS. That is not a Tactus success mapping:
Tactus handles success before recovery and must never retire successful work by
feeding it through that branch. Context mismatch or unknown failures produce a
typed error/human path, never invented success or silent fallback.

## Crash safety and duplicate prevention

Use a transactional Tactus submission outbox and result/decision inbox (or an
equivalent explicitly demonstrated durable protocol). These are delivery records,
not a competing execution queue. Exactly-once cross-process execution is not
assumed. Use durable deduplication, fencing and idempotent bounded effects.

- Crash before commit: no accepted attempt and nothing may launch.
- Crash after commit/before send: recover the same submission identity.
- Crash after Dagster acceptance/before acknowledgement: query/reconcile that
  identity; never create a new run blindly. Tags alone do not enforce uniqueness.
- Crash during runtime: inspect authoritative run/runtime state. Lease timeout
  alone cannot prove the previous writer stopped.
- Crash after result/before application: replay the inbox item transactionally.
- Crash after effect/application/before acknowledgement: return prior result;
  no duplicate lifecycle transition, child set or publication.

M2 uses one Dagster run per semantic attempt, with automatic run-level retries
disabled. Safe step retries remain supported. Run re-execution with descendant
runs requires explicit run-family correlation before enablement (M5). Never count
an execution recovery run as a new semantic attempt without an Ictus decision.

## Required vertical-slice evidence

Persist artifacts for five scenarios: success; transient step failure followed by
Dagster success without a new semantic attempt; backend failure followed by Ictus
recovery and Tactus application; ambiguous/irrecoverable case with durable human
escalation; restart at handoff and result-application boundaries with one authorized
side effect. Include denied/stale intent and duplicate/late result assertions.
Use a deterministic local subprocess capability and temporary workspace first.

Dagster's [run coordinator](https://dagster.io/docs/deployment/execution/run-coordinators)
controls the execution queue; its [run launcher](https://dagster.io/docs/deployment/execution/run-launchers)
starts run workers. Concurrency settings belong to
[Dagster execution configuration](https://dagster.io/docs/guides/operate/managing-concurrency).
