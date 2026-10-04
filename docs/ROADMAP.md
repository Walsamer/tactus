# Tactus Roadmap

This roadmap turns the current control-plane architecture into small collaborative implementation slices. It is intentionally ordered so multiple contributors can work in parallel without rebuilding Fleet v3 wholesale.

## Current principle

Build the contracts and one thin vertical slice first. Add complexity only after
the ownership boundary is proven.

The Markdown documents in this repository are the authoritative architecture
specification. The original hand-drawn sketch is kept only as a non-authoritative
reference artifact at
[`architecture/whiteboard-original.jpg`](architecture/whiteboard-original.jpg).

## Ownership freeze

Every roadmap slice below must respect the frozen ownership split:

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

> `WorkOrder.ACTIVE` means an authoritative execution attempt exists / has been
> accepted for execution. It does **not** mirror Dagster's internal
> queued/running step state.

Known issue drift is corrected in
[`architecture/CORRECTIVE_BACKLOG.md`](architecture/CORRECTIVE_BACKLOG.md).

## M0 — Architecture baseline

Status: **current**

Goals:

- version the whiteboard architecture;
- freeze terminology for lifecycle, blockers, admission/eligibility, backend health, triage, and human intervention;
- convert implementation work into GitHub Issues;
- keep Tactus/Ictus/Dagster ownership boundaries explicit.

Exit criteria:

- architecture documents merged;
- issue backlog created;
- unresolved semantic choices are explicit rather than hidden in code.

## M1 — Work Order lifecycle core

Goals:

- WorkOrder v1 model with the frozen five-state lifecycle
  (`DRAFT | OPEN | ACTIVE | IMPLEMENTED | RETIRED`);
- orthogonal `OPEN` readiness status (`READY`/`BLOCKED`) with transition guards;
- typed block reasons;
- normalized `FailureObservation` while a Work Order remains `ACTIVE`;
- parent/child provenance;
- small persistence interface/implementation only as needed for the vertical slice.

Do not add admission or backend sophistication yet.

## M2 — Admission, backend eligibility, and execution handoff

Goals:

- Tactus admission/eligibility gates over `OPEN + READY` Work Orders;
- distinguish domain readiness from execution scheduling;
- backend registry and health vocabulary as availability inputs, not execution
  state;
- Ictus-owned semantic backend routing policy;
- distinguish backend unavailable from execution concurrency unavailable (the
  latter is Dagster-owned);
- acceptance of a validated execution intent as a new execution attempt
  (`OPEN + READY → ACTIVE`);
- re-evaluate eligibility when backend availability changes.

Do not add Tactus-owned run queues, schedules, sensors, execution concurrency or
retry engines. Those are Dagster's.

## M3 — Tactus ↔ Ictus recovery boundary

Goals:

- normalize execution failures into a `FailureObservation` while the Work Order
  remains `ACTIVE`;
- receive a typed `RecoveryDecision`;
- map decisions to Tactus lifecycle transitions/readiness changes;
- keep policy in Ictus and lifecycle-state ownership in Tactus.

## M4 — Recovery actions

Goals:

- bounded **semantic** retry/requeue (distinct from Dagster `RetryPolicy`);
- backend reroute as an Ictus routing decision applied by Tactus;
- split-and-replan;
- dependency unblock;
- conservative unknown-failure escalation.

## M5 — Human intervention

Goals:

- typed intervention requests;
- allowed actions;
- audit/resolution record;
- no direct arbitrary state mutation;
- optional GitHub-facing status integration without making GitHub operational truth.

## M6 — Minimal end-to-end vertical slice

Target:

```text
WorkOrder
   ↓
Tactus lifecycle/admission
   ↓
Ictus validated decision/intent
   ↓
Dagster execution
   ↓
ExecutionResult
   ↓
Tactus state update
```

Use a safe demo capability. No production agent/sandbox complexity is required to prove the boundary.

## M7 — Agent/runtime integration

Goals:

- `AgentRuntime` port;
- Pi as initial implementation;
- optional SoL-Pi extension;
- `SandboxRuntime` port;
- OpenShell as an initial implementation while remaining replaceable.

## M8 — Source control capability

Goals:

- `SourceControlPort`;
- Git as underlying truth;
- Stax-backed worktree/change/PR workflow;
- isolate Work Order changes cleanly;
- preserve review-before-merge.

## M9 — Metaxy provenance where applicable

Goals:

- integrate Metaxy with Dagster for data/artifact lineage workloads;
- do not use Metaxy as Tactus's general audit log;
- prove lineage on one representative data/ML capability.

## M10 — Fleet migration

Goals:

- move WorkSource/control-plane responsibilities from Fleet to Tactus incrementally;
- keep the external GitHub Issue → automated work → PR workflow stable;
- retire Fleet subsystems only after equivalent Tactus paths are proven.

## Executable backlog

Implementation work is tracked as GitHub Issues. The corrected wording for the
ownership-sensitive issues (#4, #6, #8, #9, #12) is maintained in
[`architecture/CORRECTIVE_BACKLOG.md`](architecture/CORRECTIVE_BACKLOG.md). The
first meaningful tranche is the **[M1 — Control-Plane Vertical Slice](https://github.com/Walsamer/tactus/milestone/1)** milestone,
which spans roadmap slices M1–M4 plus the M6 vertical slice:

| Issue | Title |
|---|---|
| [#1](https://github.com/Walsamer/tactus/issues/1) | Formalize Work Order lifecycle and orthogonal status dimensions |
| [#2](https://github.com/Walsamer/tactus/issues/2) | Implement WorkOrder v1 model, OPEN readiness, and transition guards |
| [#3](https://github.com/Walsamer/tactus/issues/3) | Add typed BlockReason and unblock semantics |
| [#4](https://github.com/Walsamer/tactus/issues/4) | Define SchedulerPort and READY claim contract |
| [#5](https://github.com/Walsamer/tactus/issues/5) | Implement backend registry and health model |
| [#6](https://github.com/Walsamer/tactus/issues/6) | Add scheduler backend selection and BACKEND_UNAVAILABLE blocking |
| [#7](https://github.com/Walsamer/tactus/issues/7) | Define Tactus → Ictus recovery observation contract |
| [#8](https://github.com/Walsamer/tactus/issues/8) | Define/apply typed RecoveryDecision actions |
| [#9](https://github.com/Walsamer/tactus/issues/9) | Implement bounded retry and reroute semantics |
| [#10](https://github.com/Walsamer/tactus/issues/10) | Implement split-and-replan parent/child flow |
| [#11](https://github.com/Walsamer/tactus/issues/11) | Implement human intervention requests and typed resolutions |
| [#12](https://github.com/Walsamer/tactus/issues/12) | Minimal Tactus → Ictus → Dagster vertical slice |

## Collaboration flow

Architecture/docs → GitHub Issue → branch → PR → review → `main`.

One bounded issue should normally produce one PR, `main` should remain working,
architecture changes should update the relevant docs in the same PR, and
implementation must respect the plane ownership boundaries rather than
importing logic from other planes. See [CONTRIBUTING.md](../CONTRIBUTING.md)
for the lightweight contribution guidance.

## Parallel work guidance

Good parallel tracks after M0:

```text
Track A: WorkOrder lifecycle + blockers
Track B: Admission/backend eligibility contracts
Track C: Ictus recovery contract
```

These tracks should meet at explicit typed interfaces rather than sharing implementation details.
