# Triage and Recovery

## Purpose

Triage converts an execution failure/observation into a **diagnosis plus a typed recovery decision**.

The whiteboard explicitly separates:

```text
Diagnosis + Decision + optional additional action/trigger
```

That separation should be preserved.

Tactus records and applies lifecycle state. Ictus owns the typed decision/policy layer. Dagster executes validated recovery work where durable execution is needed.

A failure is an **observation**, not a lifecycle state. While a failure is being
observed, classified, and triaged, the Work Order remains `ACTIVE`. Only when
Tactus applies the validated `RecoveryDecision` does the lifecycle state change
(or readiness change within `OPEN`). This is normative:

```text
Tactus
- sole authority over WorkOrder lifecycle state
- records/normalizes FailureObservations
- applies validated RecoveryDecisions

Dagster
- owns durable execution mechanics
- reports execution outcomes/errors
- does NOT own or mutate Tactus WorkOrder state

Ictus
- owns diagnosis, policy and typed RecoveryDecisions
- consumes normalized observations from Tactus
- does NOT directly mutate WorkOrder state
```

## Recovery model

```mermaid
flowchart TD
    ACTIVE[WorkOrder = ACTIVE] -->|failure event| OBS[FailureObservation]
    OBS --> NORMALIZE[Tactus adapter: normalize observation]
    NORMALIZE --> DECIDE[Ictus: diagnosis + policy]
    DECIDE --> ACTION{RecoveryDecision}

    ACTION -->|RETRY| READY[OPEN + READY]
    ACTION -->|REQUEUE_READY| READY
    ACTION -->|REROUTE| READY
    ACTION -->|REPAIR_AND_RETRY| READY
    ACTION -->|BLOCK| BLOCKED[OPEN + BLOCKED]
    ACTION -->|ESCALATE_HUMAN| BLOCKED
    ACTION -->|SPLIT_REPLAN| SPLIT[Create child Work Orders]
    SPLIT --> RETIRED[Retire parent after durable child creation]
    ACTION -->|RETIRE| RETIRED
```

The Work Order remains `ACTIVE` while the `FailureObservation` is created and
triaged. The decision does not invent new lifecycle states. It selects an action
that leads to an existing lifecycle state (`OPEN`, `IMPLEMENTED`, or `RETIRED`),
and within `OPEN` Tactus re-evaluates readiness (`READY`/`BLOCKED`).

## Initial diagnosis taxonomy

The sketch suggests the following useful categories. Names below are normalized and may be refined, but should remain typed rather than free-form strings.

```text
TRANSIENT_RUNTIME_FAILURE
PROVIDER_API_TIMEOUT
PROVIDER_UNAVAILABLE
PROVIDER_QUOTA_EXHAUSTED
WORKER_TIMEOUT
EXECUTION_TIMEOUT
SCOPE_VIOLATION
DEPENDENCY_UNAVAILABLE
DEPENDENCY_RESOLVED
TASK_TOO_COMPLEX_SPLITTABLE
TASK_TOO_COMPLEX_NOT_SPLITTABLE
REQUEST_INVALID_OR_SUPERSEDED
VERIFICATION_FAILURE
INTEGRATION_FAILURE
UNKNOWN
```

Do not encode backend/provider-specific product names in the core taxonomy.

## Initial recovery actions

```text
RETRY
REQUEUE_READY
REROUTE
REPAIR_AND_RETRY
BLOCK
ESCALATE_HUMAN
SPLIT_REPLAN
RETIRE
NO_ACTION
```

An action may carry structured parameters, for example:

```yaml
action: REQUEUE_READY
reason: PROVIDER_API_TIMEOUT
exclude_backend: backend-x
retry_budget_consumed: 1
```

or:

```yaml
action: SPLIT_REPLAN
reason: TASK_TOO_COMPLEX_SPLITTABLE
strategy: smaller_independent_children
```

## Suggested initial decision table

This table captures the intent of the whiteboard without freezing implementation-specific retry counts prematurely.

| Diagnosis | Default decision | Result (lifecycle + readiness) | Additional effect |
|---|---|---|---|
| transient runtime failure | `RETRY` or `REQUEUE_READY` | `OPEN + READY` | consume bounded retry budget |
| provider API timeout | `RETRY` once if allowed, then `REROUTE`/`REQUEUE_READY` | `OPEN + READY` | degrade backend health |
| provider unavailable | `REROUTE` if alternate exists, otherwise `BLOCK` | `OPEN + READY` or `OPEN + BLOCKED` | backend-health update |
| provider quota exhausted | `REROUTE` if alternate exists, otherwise `BLOCK` | `OPEN + READY` or `OPEN + BLOCKED` | backend unavailable until window changes |
| dependency unavailable | `BLOCK` | `OPEN + BLOCKED` | wait for dependency event |
| dependency resolved | `REQUEUE_READY` | `OPEN + READY` | re-evaluate readiness |
| task too complex, splittable | `SPLIT_REPLAN` | parent `RETIRED` after split | create smaller child WOs |
| task too complex, not safely splittable | `ESCALATE_HUMAN` | `OPEN + BLOCKED` | human intervention request |
| worker/execution timeout | `REQUEUE_READY` within budget | `OPEN + READY` | consume retry budget; escalate when exhausted |
| scope violation | normally `ESCALATE_HUMAN` or constrained replan | `OPEN + BLOCKED` / `OPEN + READY` | never silently widen scope |
| request invalid/superseded | `RETIRE` | `RETIRED` | preserve reason/evidence |
| integration failure | `REPAIR_AND_RETRY` if policy permits; otherwise `ESCALATE_HUMAN` | `OPEN + READY` / `OPEN + BLOCKED` | preserve verification evidence, never widen scope silently |
| unknown | conservative `ESCALATE_HUMAN` | `OPEN + BLOCKED` | no speculative widening |

## Bounded retry policy

Retries must be explicitly budgeted.

At minimum distinguish:

- same-attempt micro-retry, if any;
- new execution attempt on the same backend;
- reroute to a different backend;
- Work Order-level recovery budget.

A repeated failure must eventually change strategy:

```text
retry → requeue/reroute → split/replan or human escalation
```

Never allow an unbounded loop where the same diagnosis repeatedly returns the Work Order to `OPEN + READY` without consuming a budget or changing conditions.

## Split and replan

Split/replan is a recovery action for work that is too large/complex but can be decomposed safely.

```mermaid
flowchart TD
    P[Parent ACTIVE] --> D[Ictus: SPLIT_REPLAN]
    D --> PLAN[Generate bounded child plan]
    PLAN --> VALIDATE[Validate child scopes/dependencies]
    VALIDATE --> CREATE[Durably create child WOs]
    CREATE --> RETIRE[Parent RETIRED]
    CREATE --> A[Child A OPEN + READY]
    CREATE --> B[Child B OPEN + READY]
    CREATE --> C[Child C OPEN + READY]
```

Rules:

- child WOs inherit provenance from the parent;
- child scopes must not silently exceed parent authorization;
- dependencies among children must be explicit;
- the parent is retired only after child creation succeeds durably (the parent
  remains `ACTIVE` until then);
- failure to split safely escalates rather than widening scope.

## Triage trigger vs scheduler trigger

Keep these separate:

- Scheduler decides **when/where READY work runs**.
- Triage decides **what should happen after an observation/failure**.

A backend-health observation may affect both, but through different interfaces:

```text
failure → Ictus recovery decision
backend health update → scheduler routing eligibility
```
