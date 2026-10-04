# Tactus Roadmap

This roadmap turns the current control-plane architecture into small collaborative implementation slices. It is intentionally ordered so multiple contributors can work in parallel without rebuilding Fleet v3 wholesale.

## Current principle

Build the contracts and one thin vertical slice first. Add complexity only after the ownership boundary is proven.

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

- WorkOrder v1 model;
- lifecycle states and transition guards;
- typed block reasons;
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

- normalize execution failures into an Ictus-facing observation;
- receive a typed `RecoveryDecision`;
- map decisions to Tactus lifecycle transitions/actions;
- keep policy in Ictus and state ownership in Tactus.

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
