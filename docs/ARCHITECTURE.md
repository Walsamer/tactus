# Tactus Architecture

> Tactus owns composition and coordination. Specialized systems retain
> ownership of their domain.

> Integrate through explicit contracts and adapters; do not absorb external
> source code merely for convenience.

## Architectural planes

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
shell
tests
build
deploy
etc.
```

Pixi sits around the complete stack as the reproducible environment/bootstrap
layer.

The planes are **responsibility boundaries**, not repository nesting. Tactus is
the master/system repository even though the implementation of Ictus, Dagster,
Metaxy, OpenShell, SoL-Pi, Stax, and others lives elsewhere.

## Detailed control-plane architecture

This document is the high-level entry point for the plane model. The detailed
control-plane design — Work Order lifecycle, blocking, admission/eligibility,
backend routing, execution handoff, triage/recovery, and human intervention —
lives in the following authoritative documents. Where a control-plane concern is described in more
detail there, those documents win over this summary.

- [Whiteboard overview](architecture/WHITEBOARD_OVERVIEW.md) — control-loop and
  responsibility split across Tactus, Ictus, Dagster, Metaxy, and the runtime
  plane.
- [Work Order lifecycle](architecture/WORK_ORDER_LIFECYCLE.md) — canonical
  lifecycle states, transition authority, blocking, and parent/child semantics.
- [Admission, backend routing, and execution handoff](architecture/SCHEDULING_AND_BACKENDS.md)
  — admission/eligibility gates, Ictus semantic backend routing, backend health,
  execution handoff to Dagster, and the backend-vs-execution-concurrency
  distinction.
- [Triage and recovery](architecture/TRIAGE_AND_RECOVERY.md) — diagnosis
  taxonomy, typed recovery actions, bounded retry, and split/replan.
- [Tactus ← Ictus execution-observation contract](architecture/ICTUS_OBSERVATION_CONTRACT.md)
  — the inbound, fail-closed validation/translation boundary for Ictus
  `ExecutionObservation` v1, and the separation of execution facts from
  domain/control facts.
- [Human intervention](architecture/HUMAN_INTERVENTION.md) — typed intervention
  requests and policy-bound human resolutions.
- [Corrective backlog](architecture/CORRECTIVE_BACKLOG.md) — corrected titles,
  scope and acceptance criteria for issues #4, #6, #8, #9 and #12, aligned to
  the frozen ownership split.
- [Roadmap](ROADMAP.md) — ordered implementation slices from the architecture
  baseline to Fleet migration, including the executable GitHub backlog.

Markdown in this repository is the authoritative architecture specification. The
original hand-drawn sketch is kept only as a non-authoritative reference artifact
at [`architecture/whiteboard-original.jpg`](architecture/whiteboard-original.jpg).

Three distinctions are load-bearing across all of these documents:

- **Lifecycle state, readiness status, observations, and recovery actions are
  different concepts.** The Work Order lifecycle is exactly
  `DRAFT | OPEN | ACTIVE | IMPLEMENTED | RETIRED`. `READY` and `BLOCKED` are
  readiness statuses of an `OPEN` Work Order, not lifecycle states. A
  `FailureObservation` is an event attached to an `ACTIVE` Work Order, not a
  state. `RETRY`, `REQUEUE_READY`, `REROUTE`, `SPLIT_REPLAN`, and
  `ESCALATE_HUMAN` are actions/decisions, not lifecycle states.
- **Failure does not transition the Work Order.** The Work Order remains
  `ACTIVE` while the failure is observed, classified, and triaged; Tactus applies
  the resulting recovery decision.
- **Backend health is system/runtime state, not Work Order truth.** A Work
  Order's state does not encode which backend is healthy; a lack of worker
  capacity leaves it `OPEN + READY` rather than `OPEN + BLOCKED`.

## Ownership boundaries

The single canonical ownership table is:

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

These distinctions are load-bearing and must not be conflated:

```text
domain dependency      ≠ Dagster step dependency
domain readiness       ≠ execution scheduling
semantic retry         ≠ Dagster RetryPolicy
backend routing policy ≠ Dagster worker scheduling
ACTIVE                 ≠ "CPU currently running"
```

Normative `WorkOrder.ACTIVE` semantic:

> `WorkOrder.ACTIVE` means an authoritative execution attempt exists / has been
> accepted for execution. It does **not** mirror Dagster's internal
> queued/running step state.

**Tactus must never become a workflow engine, run queue, temporal scheduler or
retry engine.** Dagster is authoritative over temporal execution state. Ictus is
authoritative over semantic recovery/routing decisions. Tactus is authoritative
over WorkOrder/domain state.

### Tactus — control plane (first-party)

Tactus is the master/system repository and owns the control plane:

- Work Order identity and domain lifecycle state (sole authority)
- domain dependencies and readiness (`OPEN` blocked/unblocked readiness status)
- admission/eligibility of admitted work
- domain facts/context (including normalized failure observations)
- application of validated semantic decisions (recovery, intervention)
- human/domain coordination, including the human-intervention queue
- system configuration
- integration adapters
- component registration
- aggregated observability
- future CLI/API/UI

Tactus decides and coordinates **what domain work is admitted and how its domain
state evolves**. It must not reimplement functionality already owned by lower
layers. In particular Tactus owns no run queue, no schedules/sensors, no
execution concurrency and no retry engine — those are Dagster's. Semantic
routing and recovery strategy are Ictus's. Tactus applies validated decisions
and owns the resulting lifecycle state.

### Ictus — decision plane (first-party external)

Ictus is first-party but independently versioned and independently
publishable/useful. It owns:

- typed decisions
- policy
- capability validation
- semantic routing (including backend routing policy)
- approvals
- diagnosis of execution observations and typed recovery decisions
- recovery strategy
- validated `ExecutionIntent`s

Boundary:

```text
Tactus
   ↓
Ictus
   ↓
Dagster
```

Ictus source is not copied into Tactus; it is integrated through a contract.
Ictus is authoritative over semantic recovery/routing decisions. It owns
diagnosis and typed recovery decisions but does **not** directly mutate Work
Order state; Tactus applies the validated decision and owns the resulting
lifecycle state.

### Dagster — execution plane (third-party)

Dagster is the third-party execution substrate, and it is authoritative over
temporal execution state. It owns:

- temporal/durable execution
- workflow graph
- run/step state and persistence
- schedules
- sensors/events
- run queue
- execution concurrency
- retries (`RetryPolicy`)
- re-execution
- execution dependencies
- execution history
- execution observability

Tactus/Ictus decide what should happen; Dagster owns **durably executing it**.
Dagster reports execution outcomes and errors, but it does **not** own or mutate
Tactus Work Order state. Tactus must never mirror Dagster's internal
queued/running step state as Work Order truth.

### Metaxy — provenance, Dagster-integrated (third-party)

Metaxy is a third-party provenance/metadata system, tightly integrated with the
Dagster execution plane rather than merely another unrelated Tactus adapter.

```text
Dagster
   +
Metaxy
```

Dagster owns execution lineage. Metaxy enriches execution with:

- data/artifact lineage
- versions
- feature/sample provenance
- incremental data/ML traceability
- code/data version relationships

Metaxy is **not** Tactus's universal audit log. General Work Order / agent /
decision provenance belongs to Tactus + Ictus + Dagster. Metaxy is especially
relevant for data/ML workloads.

### SandboxRuntime — sandbox abstraction

`SandboxRuntime` is an abstraction, not OpenShell itself. The initial/default
candidate backend is OpenShell; other sandbox implementations may be added
later.

```text
SandboxRuntime
├── OpenShellBackend
└── OtherSandboxBackend
```

Tactus is not tightly coupled to OpenShell. The sandbox implementation must
remain replaceable without changing the higher-level Tactus, Ictus, or Dagster
architecture.

### AgentRuntime — agent runtime (separate from sandboxing)

`AgentRuntime` is distinct from sandboxing. The initial/default agent runtime is
Pi, with SoL-Pi as an optional optimization/extension layer around Pi.

```text
AgentRuntime
├── Pi
│   └── SoL-Pi
└── future agent implementations
```

Sandbox choice and agent choice remain **orthogonal**. For example:

```yaml
execution:
  sandbox: openshell
  agent: pi
```

could later become:

```yaml
execution:
  sandbox: another-sandbox
  agent: another-agent
```

without changing the rest of the architecture.

### Git + Stax — source control capability

Git is the underlying source-control truth. Stax is the higher-level
source-control/workspace/change-management capability and is important to
Tactus's future push/pull workflow. Expected responsibility:

- worktrees
- isolated change lanes
- branches
- stacked changes
- stacked PRs
- restacking
- synchronization
- publishing changes
- safe recovery

Tactus should eventually expose a `SourceControlPort`, with Stax as the primary
implementation. Raw Git/Stax-specific semantics must not be scattered
throughout the Tactus core.

### Pixi — reproducible environment/bootstrap

Pixi is the outer reproducible environment/bootstrap layer. It may eventually
provide commands such as:

```text
pixi run dev
pixi run test
pixi run tactus
pixi run dagster
pixi run verify
```

Pixi does not replace every ecosystem-native package manager:

```text
Rust    → Cargo
Python  → Python/uv as appropriate
Node    → npm/pnpm/bun as appropriate
System/reproducible environment → Pixi
```

Pixi is intentionally not over-configured during the bootstrap stage.

## Replaceability principles

- Dependencies integrate through explicit contracts and adapters.
- The sandbox backend and the agent runtime are independently replaceable.
- The source-control capability is accessed through a port abstraction, not
  scattered raw semantics.
- External systems keep ownership of their domain; Tactus re-validates and
  remains the authority for composition and coordination.
- No external system owns Tactus state; external results are bounded or
  advisory unless explicitly promoted.

## Implementation language and portability

The control plane is implemented in **Python 3.12+**, managed with `uv`.
Python is the pragmatic default because the first-party decision plane (Ictus)
and the execution plane (Dagster) are Python systems, so contracts and adapters
sit naturally alongside them.

The **domain core** (`src/tactus/domain/`) is deliberately dependency-free: pure
value types, entities, guards and graph invariants with no I/O, framework or
persistence coupling. That keeps the load-bearing invariants portable, so a
specific reliability-critical component can later be implemented in Rust behind
the same contract **when one concrete component genuinely justifies it** — not
preemptively, and without rewriting the domain model around it.

## Dependency classification

- **First-party (Tactus-owned):** Tactus itself.
- **First-party external:** independently versioned, first-party systems such as
  Ictus.
- **Third-party:** externally maintained systems such as Dagster, Metaxy,
  OpenShell, Pi, SoL-Pi, Stax, and Pixi.
- **Private / out of scope:** systems explicitly reserved but not part of the
  current architecture (for example Arcventory — see
  `integrations/private/arcventory/`).
