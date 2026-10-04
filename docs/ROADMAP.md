# Tactus Roadmap

This roadmap turns the current control-plane architecture into small collaborative implementation slices. It is intentionally ordered so multiple contributors can work in parallel without rebuilding Fleet v3 wholesale.

## Current principle

Build the contracts and one thin vertical slice first. Add complexity only after
the ownership boundary is proven.

The Markdown documents in this repository are the authoritative architecture
specification. The original hand-drawn sketch is kept only as a non-authoritative
reference artifact at
[`architecture/whiteboard-original.jpg`](architecture/whiteboard-original.jpg).

## M0 — Architecture baseline

Status: **current**

Goals:

- version the whiteboard architecture;
- freeze terminology for lifecycle, blockers, scheduler, backend health, triage, and human intervention;
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

Do not add scheduler sophistication yet.

## M2 — Scheduler and backend model

Goals:

- READY queue/selection contract;
- concurrency/budget admission gates;
- backend registry and health vocabulary;
- distinguish backend unavailable from worker capacity unavailable;
- atomic claim into ACTIVE;
- unblock/re-evaluate when backend availability changes.

## M3 — Tactus ↔ Ictus recovery boundary

Goals:

- normalize execution failures into a `FailureObservation` while the Work Order
  remains `ACTIVE`;
- receive a typed `RecoveryDecision`;
- map decisions to Tactus lifecycle transitions/readiness changes;
- keep policy in Ictus and lifecycle-state ownership in Tactus.

## M4 — Recovery actions

Goals:

- bounded retry/requeue;
- backend reroute;
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
Tactus lifecycle/scheduler
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

Implementation work is tracked as GitHub Issues. The first meaningful
tranche is the **[M1 — Control-Plane Vertical Slice](https://github.com/Walsamer/tactus/milestone/1)** milestone,
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
Track B: Scheduler/backend contracts
Track C: Ictus recovery contract
```

These tracks should meet at explicit typed interfaces rather than sharing implementation details.
