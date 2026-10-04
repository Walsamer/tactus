# Tactus Control-Plane Architecture — Whiteboard Translation

This document translates the hand-drawn control-plane design into a versioned architecture description. It is a design source of truth, not an implementation claim.

The original hand-drawn sketch is preserved as a **non-authoritative reference
artifact** at [`whiteboard-original.jpg`](whiteboard-original.jpg). This Markdown
document — not the image — is the authoritative architecture specification.

The sketch focuses on five connected concerns:

1. Work Order lifecycle and state ownership.
2. Blocking and unblocking.
3. Scheduler, backend selection, and capacity.
4. Triage and recovery decisions after failures.
5. Human intervention for cases automation cannot resolve safely.

The design must remain consistent with the wider Tactus architecture:

```text
CONTROL PLANE
Tactus
   ↓
DECISION PLANE
Ictus
   ↓
EXECUTION / PROVENANCE PLANE
Dagster + Metaxy
   ↓
RUNTIME / SANDBOX PLANE
SandboxRuntime       AgentRuntime
OpenShell / ...      Pi / SoL-Pi / ...
   ↓
CAPABILITIES
Git + Stax
shell / tests / build / deploy / ...
```

## Responsibility split

### Tactus

Tactus owns operational coordination:

- Work Order identity and lifecycle state;
- admission from work sources;
- dependency readiness;
- blocked/unblocked status of an `OPEN` Work Order;
- scheduler eligibility;
- concurrency and budget gates;
- backend/runtime availability view;
- application of validated decisions;
- human-intervention queue;
- orchestration-level observability.

Normative ownership rules:

```text
Tactus
- sole authority over WorkOrder lifecycle state
- owns OPEN readiness/blocking status
- records/normalizes FailureObservations
- applies validated RecoveryDecisions
```

### Ictus

Ictus owns typed decisions and policy:

- classify an observation/failure into a bounded diagnosis;
- choose a typed recovery action;
- capability and policy validation;
- approval requirements;
- emit validated execution intents.

```text
Ictus
- owns diagnosis, policy and typed RecoveryDecisions
- consumes normalized observations from Tactus
- does NOT directly mutate WorkOrder state
```

Tactus must not bury recovery policy in ad-hoc scheduler branches. Ictus must not become a scheduler or durable workflow engine.

### Dagster

Dagster owns durable execution:

- runs and steps;
- retries/re-execution mechanics requested by a validated intent;
- persistence of execution history;
- workflow dependencies;
- durable execution observability.

```text
Dagster
- owns durable execution mechanics
- reports execution outcomes/errors
- does NOT own or mutate Tactus WorkOrder state
```

### Metaxy

Metaxy is interwoven with Dagster where data/artifact lineage is relevant. It does not replace Tactus lifecycle/audit state and is not required for every software-engineering Work Order.

### Runtime plane

The runtime plane supplies replaceable `SandboxRuntime` and `AgentRuntime` implementations. Runtime choice must not change Tactus lifecycle semantics.

## Core control loop

```mermaid
flowchart TD
    WS[Work Source] --> DRAFT[DRAFT]
    DRAFT -->|admit| OPEN[OPEN]
    DRAFT -->|withdraw| RETIRED[RETIRED]

    OPEN -->|readiness: executable| READY[OPEN + READY]
    OPEN -->|readiness: known blocker| BLOCKED[OPEN + BLOCKED]
    BLOCKED -->|blocker resolved; re-evaluate| READY
    READY -->|scheduler claim| ACTIVE[ACTIVE]
    OPEN -->|invalid / superseded before execution| RETIRED

    ACTIVE -->|successful result + verification| IMPLEMENTED[IMPLEMENTED]
    ACTIVE -->|superseded / cancelled| RETIRED
    ACTIVE -->|failure event| OBS[FailureObservation]

    OBS --> TRIAGE[Triage / diagnosis]
    TRIAGE --> ICTUS[Ictus: RecoveryDecision]
    ICTUS -->|retry / requeue / reroute| READY
    ICTUS -->|needs human/input/backend/dependency| BLOCKED
    ICTUS -->|split and replan| CHILDREN[Durably create child Work Orders]
    CHILDREN --> RETIRED
    ICTUS -->|RETIRE| RETIRED
```

The diagram intentionally treats **retry as an action**, not as a permanent lifecycle state. A retry/requeue decision returns the Work Order to `OPEN + READY` and creates a new execution attempt. The `OPEN + READY` and `OPEN + BLOCKED` nodes are **readiness statuses of the `OPEN` lifecycle state**, not peer lifecycle states. A failure event creates a `FailureObservation` while the Work Order remains `ACTIVE`; failure never becomes a lifecycle state. Successful completion does **not** pass through Ictus recovery: `ACTIVE → IMPLEMENTED` is a direct, independent outcome, and `IMPLEMENTED` / `RETIRED` are terminal.

## Canonical lifecycle states

The Work Order lifecycle is exactly:

```text
DRAFT | OPEN | ACTIVE | IMPLEMENTED | RETIRED
```

| Lifecycle state | Meaning |
|---|---|
| `DRAFT` | Work exists but is not yet admitted for execution. |
| `OPEN` | Work has been admitted; readiness is tracked separately as `READY` or `BLOCKED`. |
| `ACTIVE` | One execution attempt currently owns the Work Order lease. |
| `IMPLEMENTED` | The requested outcome passed required verification (whiteboard term: `IMPLEMENTED`). |
| `RETIRED` | Work is intentionally no longer executable, for example because it is superseded, invalidated, or replaced by split child work. |

`IMPLEMENTED` and `RETIRED` are terminal.

### OPEN readiness status (orthogonal, not lifecycle states)

While the lifecycle state is `OPEN`, the Work Order carries one readiness status:

| Readiness status | Meaning |
|---|---|
| `READY` | Work is executable and waiting for scheduling/capacity. |
| `BLOCKED` | Work cannot currently progress because a typed blocker exists. |

`READY` and `BLOCKED` are **not** peer lifecycle states. A readiness change is
**not** a lifecycle transition, and a resolved blocker must trigger
re-evaluation rather than blindly assigning `READY`.

### Failure is an observation, not a state

A failure that occurs while a Work Order is `ACTIVE` produces a
`FailureObservation` while the Work Order remains `ACTIVE`:

```text
ACTIVE
  ↓ failure event
FailureObservation
  ↓
Triage / Ictus RecoveryDecision
  ↓
Tactus applies the decision
```

There is no `FAILED` / `FAIL` Work Order lifecycle state.

## Supporting architecture documents

- [`WORK_ORDER_LIFECYCLE.md`](WORK_ORDER_LIFECYCLE.md)
- [`SCHEDULING_AND_BACKENDS.md`](SCHEDULING_AND_BACKENDS.md)
- [`TRIAGE_AND_RECOVERY.md`](TRIAGE_AND_RECOVERY.md)
- [`HUMAN_INTERVENTION.md`](HUMAN_INTERVENTION.md)
- [`../ROADMAP.md`](../ROADMAP.md)

## Design rules extracted from the sketch

1. **Lifecycle state, readiness status, observations, and recovery actions are different concepts.** `READY`/`BLOCKED` are `OPEN` readiness statuses; `FailureObservation` is an event attached to an `ACTIVE` Work Order; `RETRY`, `REQUEUE`, `REROUTE`, `SPLIT_REPLAN`, and `ESCALATE_HUMAN` are actions/decisions. None of them are lifecycle states.
2. **`READY` means executable but waiting.** Lack of worker capacity keeps a Work Order `OPEN + READY`; it is not a blocker.
3. **`BLOCKED` is an `OPEN` readiness status and always carries a typed reason.** The status alone is insufficient.
4. **Backend health is system state, not Work Order truth.** A Work Order may be reroutable when one backend fails.
5. **Triage produces a diagnosis plus a typed decision.** The decision then maps to an existing lifecycle transition and optional additional action.
6. **Human intervention uses the same typed action model as automation.** Humans do not directly mutate arbitrary state.
7. **Retry budgets are bounded.** Repeated transient failure eventually changes strategy or escalates instead of looping forever.
8. **Split-and-replan replaces the original execution unit with smaller children.** The parent retains provenance and becomes non-executable when the children take over.
9. **Failure does not transition the Work Order.** It remains `ACTIVE` until Tactus applies the resulting recovery decision or accepts successful completion.

## Open questions to resolve during implementation

The hand-drawn design intentionally leaves several choices open. They should become explicit ADRs or issue decisions rather than being guessed in code:

- Which block reasons are top-level reasons versus sub-reasons?
- Which backend health signals are authoritative and how long do they remain valid?
- Which failures permit a same-backend retry before requeue/reroute?
- What is the exact retry budget hierarchy: per attempt, per diagnosis, per Work Order, per backend?
- Which human actions require Ictus policy validation/approval before Tactus applies them?

Do not silently resolve these by copying Fleet v3 behavior. Tactus should preserve the useful semantics while establishing simpler, explicit contracts.
