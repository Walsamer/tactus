# Initial GitHub Issue Plan

This file is a staging plan for GitHub Issues derived from the current architecture. Once the issues are created, GitHub becomes the executable collaboration backlog while these docs remain the architectural source of truth.

> **Status:** the backlog below has been created on GitHub. Each issue heading links to its live issue. Do not duplicate issue bodies into additional files.

Do not create one issue per sentence in the architecture. The issues below are bounded implementation outcomes that can be independently reviewed.

## Suggested labels

```text
area:control-plane
area:scheduler
area:backend
area:ictus
area:recovery
area:human
area:integration
type:architecture
type:implementation
status:ready
status:blocked
```

## Issue 1 — Freeze Work Order lifecycle v1 — [#1](https://github.com/Walsamer/tactus/issues/1)

**Area:** control-plane  
**Type:** architecture

### Goal

Turn `docs/architecture/WORK_ORDER_LIFECYCLE.md` into a precise v1 contract for lifecycle states and legal transitions.

### Acceptance criteria

- lifecycle states have one-sentence normative definitions;
- legal transition matrix is explicit;
- retry is confirmed as an action, not a lifecycle state;
- `FAIL`-as-state vs execution-result-only is explicitly decided or captured in an ADR;
- no Fleet-v3-only states are copied without justification.

### Depends on

None.

---

## Issue 2 — Implement WorkOrder v1 model and transition guards — [#2](https://github.com/Walsamer/tactus/issues/2)

**Area:** control-plane  
**Type:** implementation

### Goal

Implement the smallest Tactus WorkOrder model supporting the v1 lifecycle.

### Acceptance criteria

- typed WorkOrder identity and state;
- transition guard rejects illegal transitions;
- transition records include reason/time/source;
- unit tests cover every legal and representative illegal transition;
- no scheduler, Ictus, Dagster, or agent logic inside the model.

### Depends on

Issue 1.

---

## Issue 3 — Add typed BlockReason and unblock semantics — [#3](https://github.com/Walsamer/tactus/issues/3)

**Area:** control-plane  
**Type:** implementation

### Goal

Represent `BLOCKED` with structured reason/subreason/evidence rather than state alone.

### Acceptance criteria

- initial top-level block reasons implemented;
- human-input subreasons supported;
- worker-capacity waiting is explicitly not a block reason;
- unblock operation re-evaluates readiness rather than blindly setting READY;
- tests cover block → resolve → readiness behavior.

### Depends on

Issue 1 or Issue 2, depending on implementation order.

---

## Issue 4 — Define SchedulerPort and READY claim contract — [#4](https://github.com/Walsamer/tactus/issues/4)

**Area:** scheduler  
**Type:** architecture/implementation

### Goal

Define the scheduler boundary that turns eligible READY work into an atomic ACTIVE claim.

### Acceptance criteria

- input/output contract documented;
- global/per-project capacity gates represented;
- dependency/readiness recheck occurs at claim boundary;
- no recovery/triage policy in scheduler;
- lack of worker capacity leaves WO READY.

### Depends on

Issue 2.

---

## Issue 5 — Implement backend registry and health model — [#5](https://github.com/Walsamer/tactus/issues/5)

**Area:** backend  
**Type:** implementation

### Goal

Create a backend abstraction that exposes compatibility, health, and capacity to the scheduler.

### Acceptance criteria

- small typed health vocabulary (`AVAILABLE`, `DEGRADED`, `UNAVAILABLE`, `DISABLED` or justified equivalent);
- health observations carry timestamps;
- compatibility is separate from current capacity;
- no provider-specific names in core contracts;
- tests cover backend unavailable vs worker busy semantics.

### Depends on

Can proceed in parallel with Issue 4 after lifecycle terminology is stable.

---

## Issue 6 — Add scheduler backend selection and BACKEND_UNAVAILABLE blocking — [#6](https://github.com/Walsamer/tactus/issues/6)

**Area:** scheduler/backend  
**Type:** implementation

### Goal

Select among compatible healthy backends and correctly distinguish backend absence from temporary capacity saturation.

### Acceptance criteria

- compatible+available+capacity → claim ACTIVE;
- compatible+available but no capacity → remain READY;
- no compatible available backend → BLOCKED with `BACKEND_UNAVAILABLE`;
- backend recovery can trigger readiness re-evaluation;
- no direct provider retry logic here.

### Depends on

Issues 3, 4, 5.

---

## Issue 7 — Define Tactus → Ictus recovery observation contract — [#7](https://github.com/Walsamer/tactus/issues/7)

**Area:** ictus/recovery  
**Type:** architecture/implementation

### Goal

Normalize a failed execution into the domain-neutral observation sent to Ictus without exposing Tactus persistence internals.

### Acceptance criteria

- mapping is explicit and versioned;
- representative failure categories map cleanly;
- evidence/provenance retained;
- no lifecycle mutation occurs inside the mapper;
- contract is compatible with current Ictus versioned types or requires a clearly scoped Ictus PR.

### Depends on

Can proceed in parallel after Issue 1.

---

## Issue 8 — Define/apply typed RecoveryDecision actions — [#8](https://github.com/Walsamer/tactus/issues/8)

**Area:** ictus/recovery  
**Type:** implementation

### Goal

Apply an Ictus recovery decision to Tactus using a closed action vocabulary.

### Acceptance criteria

Support at least:

```text
RETRY
REQUEUE_READY
REROUTE
BLOCK
ESCALATE_HUMAN
SPLIT_REPLAN
RETIRE
```

- each action maps to explicit Tactus transitions/effects;
- unknown actions fail closed;
- Ictus does not directly mutate Tactus state;
- Tactus does not duplicate Ictus decision policy.

### Depends on

Issues 2, 3, 7.

---

## Issue 9 — Implement bounded retry and reroute semantics — [#9](https://github.com/Walsamer/tactus/issues/9)

**Area:** recovery/backend  
**Type:** implementation

### Goal

Implement bounded retry/requeue/reroute behavior for transient and backend failures.

### Acceptance criteria

- retry budget is explicit and persisted with execution history;
- repeated same diagnosis cannot loop forever;
- API timeout can degrade backend health;
- quota/unavailability can reroute through scheduler;
- publication/transport failures are not confused with execution failures;
- tests prove exhaustion path escalates/blocks.

### Depends on

Issues 5, 6, 8.

---

## Issue 10 — Implement split-and-replan parent/child flow — [#10](https://github.com/Walsamer/tactus/issues/10)

**Area:** recovery/control-plane  
**Type:** implementation

### Goal

Support `SPLIT_REPLAN` for work that is too complex but safely decomposable.

### Acceptance criteria

- child WOs preserve parent provenance;
- child scopes cannot silently widen parent authorization;
- child dependencies are explicit;
- parent retires only after children are durably created;
- failed/unsafe split escalates instead of partially mutating state.

### Depends on

Issues 2, 8.

---

## Issue 11 — Implement human intervention requests and typed resolutions — [#11](https://github.com/Walsamer/tactus/issues/11)

**Area:** human  
**Type:** implementation

### Goal

Represent human-required recovery without creating a second lifecycle system.

### Acceptance criteria

- intervention request model with reason, summary, allowed actions, status, evidence;
- Work Order remains `BLOCKED(HUMAN_INPUT_REQUIRED)`;
- human resolution is a typed command, not direct database mutation;
- secret requests never store secret values;
- resolution re-evaluates all blockers before READY;
- audit trail is retained.

### Depends on

Issues 3, 8.

---

## Issue 12 — Minimal Tactus → Ictus → Dagster vertical slice — [#12](https://github.com/Walsamer/tactus/issues/12)

**Area:** integration  
**Type:** implementation

### Goal

Prove the ownership boundaries with one safe demo Work Order from admission to execution result.

### Target flow

```text
WorkOrder
   ↓
Tactus OPEN/READY
   ↓
Scheduler claim ACTIVE
   ↓
Ictus validation / ExecutionIntent
   ↓
Dagster demo capability
   ↓
ExecutionResult
   ↓
Tactus COMPLETED or failure→recovery path
```

### Acceptance criteria

- uses existing Ictus/Dagster contracts rather than reimplementing them;
- one success case reaches COMPLETED;
- one controlled failure reaches Ictus recovery decision and a valid Tactus state;
- no Pi/OpenShell/Stax/Metaxy requirement for this proof;
- architecture docs updated if implementation exposes a wrong assumption.

### Depends on

Issues 2, 4, 7, 8; use minimal implementations of backend/capacity where necessary rather than waiting for every later feature.

---

## Recommended parallel assignment

For two or three contributors:

```text
Contributor A
  Issue 1 → 2 → 3

Contributor B
  Issue 4 → 5 → 6

Contributor C / shared
  Issue 7 → 8

Then integrate
  Issue 12

Follow-on
  Issues 9, 10, 11
```

If only two people are available, one person can take lifecycle/blocking and the other scheduler/backend while Issue 7 is handled as a small joint contract review before implementation.
