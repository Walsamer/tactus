# Corrective Backlog — Ownership-Aligned Issue Rewrites

This document corrects existing GitHub issues that were written before the
Tactus / Ictus / Dagster ownership boundary was re-frozen. Their original text
drifts Tactus toward a workflow/execution engine and duplicates Dagster. The
corrected text below is the authoritative replacement wording.

This is a **documentation deliverable**: it does not rewrite GitHub issues by
itself. The operator applies the corrected title, scope and acceptance criteria
to the corresponding issues.

## Frozen ownership model

```text
Tactus
= WorkOrder/domain lifecycle
= domain dependencies/readiness
= admission/eligibility
= domain facts/context
= application of validated semantic decisions
= human/domain coordination

Ictus
= semantic decision making
= policy
= capability validation
= semantic routing
= recovery strategy

Dagster
= temporal/durable execution
= workflow graph
= run/step state
= schedules
= sensors/events
= run queue
= execution concurrency
= retries
= re-execution
= execution history
= execution observability
```

Load-bearing distinctions:

```text
domain dependency      ≠ Dagster step dependency
domain readiness       ≠ execution scheduling
semantic retry         ≠ Dagster RetryPolicy
backend routing policy ≠ Dagster worker scheduling
ACTIVE                 ≠ "CPU currently running"
```

**Tactus must never become a workflow engine, run queue, temporal scheduler or
retry engine.** Dagster is authoritative over temporal execution state. Ictus is
authoritative over semantic recovery/routing decisions. Tactus is authoritative
over WorkOrder/domain state.

Normative `WorkOrder.ACTIVE` semantic (verbatim):

> `WorkOrder.ACTIVE` means an authoritative execution attempt exists / has been
> accepted for execution. It does **not** mirror Dagster's internal
> queued/running step state.

---

## Issue #4

**Original title:** Define SchedulerPort and READY claim contract

**Problem:** A `SchedulerPort` with a READY claim contract frames Tactus as an
execution scheduler and implies a Tactus-owned run queue. Scheduling, run queues
and execution concurrency are Dagster's.

**Corrected title:** Define the Tactus admission/eligibility port and
execution-attempt acceptance contract

**Corrected scope:**

- Define a Tactus port that answers only: *is this `OPEN + READY` Work Order
  eligible for execution under domain readiness, required capabilities and
  configured eligibility gates?*
- Define how Tactus accepts a validated execution intent as a new execution
  attempt, transitioning `OPEN + READY → ACTIVE`.
- The contract must not define a run queue, run ordering, schedules, sensors,
  execution concurrency or retries.
- Backend routing/selection is an Ictus semantic decision consumed by Tactus,
  not a Tactus scheduling heuristic.
- Distinguish domain readiness from execution scheduling explicitly.

**Corrected acceptance criteria:**

- [ ] An admission/eligibility port and its inputs/outputs are defined.
- [ ] Accepting a validated execution intent produces `ACTIVE` with a recorded
      transition, consistent with the normative `WorkOrder.ACTIVE` semantic.
- [ ] The contract contains no run queue, schedules, sensors, execution
      concurrency or retry engine responsibilities.
- [ ] Backend routing is expressed as an Ictus-provided decision, not Tactus
      policy.
- [ ] Domain readiness ≠ execution scheduling is documented in the contract.

---

## Issue #6

**Original title:** Add scheduler backend selection and BACKEND_UNAVAILABLE
blocking

**Problem:** "Scheduler backend selection" places semantic routing policy and
worker scheduling in Tactus. Semantic routing is Ictus-owned and worker
scheduling is Dagster-owned.

**Corrected title:** Apply Ictus semantic backend routing and
BACKEND_UNAVAILABLE blocking

**Corrected scope:**

- Consume an Ictus-provided semantic routing decision that selects a compatible
  backend for an eligible Work Order.
- When routing yields no compatible backend, apply
  `OPEN + READY → OPEN + BLOCKED(reason=BACKEND_UNAVAILABLE)` as a readiness
  change within `OPEN`.
- Re-evaluate eligibility when backend availability changes; do not bulk-force
  blocked work to `READY`.
- Tactus must not implement backend selection heuristics, worker scheduling or
  execution concurrency; those remain with Ictus (routing policy) and Dagster
  (worker scheduling) respectively.

**Corrected acceptance criteria:**

- [ ] Tactus applies an Ictus semantic routing decision rather than computing
      backend selection itself.
- [ ] `BACKEND_UNAVAILABLE` is a typed block reason on `OPEN`, not a lifecycle
      state.
- [ ] Backend-routing policy is owned by Ictus and worker scheduling by
      Dagster; neither is moved into Tactus.
- [ ] Backend availability changes trigger eligibility re-evaluation, not an
      unconditional unblock.

---

## Issue #8

**Original title:** Define/apply typed RecoveryDecision actions

**Problem:** Without the ownership split, "apply typed actions" can be read as
Tactus owning recovery policy and execution retry mechanics.

**Corrected title:** Define/apply typed semantic RecoveryDecision actions

**Corrected scope:**

- Ictus owns the typed `RecoveryDecision` vocabulary, diagnosis and policy.
- Tactus records/normalizes `FailureObservation`s while the Work Order remains
  `ACTIVE`, and applies the validated decision as an existing lifecycle
  transition and/or readiness change.
- A `RecoveryDecision` action is a **semantic** decision. It must be kept
  distinct from Dagster's execution-level `RetryPolicy`, which Dagster applies
  to a run independently.
- Tactus must not invent recovery policy or a retry engine; it applies validated
  decisions.

**Corrected acceptance criteria:**

- [ ] The typed `RecoveryDecision` vocabulary is defined as Ictus-owned policy.
- [ ] Tactus applies validated decisions only; it embeds no recovery policy.
- [ ] `FailureObservation` is recorded while the Work Order remains `ACTIVE`.
- [ ] Semantic recovery actions are explicitly distinguished from Dagster
      `RetryPolicy` and execution retries.
- [ ] Applied decisions map only to existing lifecycle states plus `OPEN`
      readiness.

---

## Issue #9

**Original title:** Implement bounded retry and reroute semantics

**Problem:** "Retry semantics" reads as a Tactus-owned retry engine that would
duplicate Dagster.

**Corrected title:** Implement bounded semantic retry/requeue/reroute decisions
(distinct from Dagster RetryPolicy)

**Corrected scope:**

- Implement bounded **semantic** retry/requeue/reroute decisions as Ictus-owned
  recovery policy applied by Tactus.
- A semantic retry creates a new execution attempt: the Work Order returns to
  `OPEN + READY` and re-enters admission; it does not stay in a `RETRYING`
  lifecycle state.
- Bounded budgets must eventually change strategy (retry → requeue/reroute →
  split/replan or human escalation).
- Dagster owns execution-level retries (`RetryPolicy`) for a run; Tactus must
  not implement a retry engine, scheduler or run queue.

**Corrected acceptance criteria:**

- [ ] Semantic retry/requeue/reroute actions are bounded and Ictus-policy-driven.
- [ ] Applying a semantic retry returns the Work Order to `OPEN + READY` as a
      new attempt, not a new lifecycle state.
- [ ] Budget exhaustion changes strategy instead of looping.
- [ ] Tactus owns no execution retry engine; Dagster `RetryPolicy` is documented
      as separate execution-level mechanics.
- [ ] `semantic retry ≠ Dagster RetryPolicy` is stated in the implementation
      notes/contract.

---

## Issue #12

**Original title:** Minimal Tactus → Ictus → Dagster vertical slice

**Problem:** The slice can accidentally make Tactus the execution orchestrator.
The slice must prove the boundary, not blur it.

**Corrected title:** Minimal Tactus → Ictus → Dagster vertical slice with
ownership-consistent handoff

**Corrected scope:**

- Tactus: admit a Work Order, evaluate domain dependency readiness, apply
  admission/eligibility, accept an execution attempt (`ACTIVE`), and apply the
  returned result as `ACTIVE → IMPLEMENTED` or a validated recovery decision.
- Ictus: produce the semantic routing/recovery decision (which backend, what to
  do on failure).
- Dagster: own the durable run, execution state, concurrency and any
  execution-level retry.
- The slice must not introduce a Tactus run queue, scheduler, schedules,
  sensors, execution concurrency or retry engine.
- `WorkOrder.ACTIVE` means an authoritative execution attempt has been accepted;
  it does not mirror Dagster's queued/running step state.

**Corrected acceptance criteria:**

- [ ] One end-to-end path runs: `WorkOrder → Tactus admission → Ictus decision →
      Dagster execution → ExecutionResult → Tactus state update`.
- [ ] Tactus never inspects or mirrors Dagster's internal run/step queue state.
- [ ] Ictus owns the semantic decision(s) in the path.
- [ ] Dagster owns all temporal execution, concurrency and execution retries.
- [ ] Success follows `ACTIVE → IMPLEMENTED` without passing through recovery;
      failure produces a `FailureObservation` while the Work Order stays
      `ACTIVE`.
- [ ] The slice demonstrates the frozen ownership table rather than duplicating
      Dagster.
