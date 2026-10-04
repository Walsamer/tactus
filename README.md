# Tactus

Tactus is the control plane and master integration repository for an autonomous
software-engineering and agentic execution system. It composes specialized,
independently maintained systems behind explicit contracts rather than
reimplementing their functionality.

## Master repository does not mean monorepo

Tactus is the *master* repository because it owns composition, configuration,
contracts, adapters, and the migration path — not because it contains the source
code of every system in the stack.

Cloning Tactus should eventually provide the authoritative entry point for
constructing, configuring, testing, and operating the complete Tactus stack.
External and independently useful systems remain separate repositories/packages
and are integrated through explicit interfaces.

## Current architecture

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

These are architectural responsibility boundaries, not repository nesting.
Tactus remains the master/system repository even though the implementation of
Ictus, Dagster, Metaxy, OpenShell, SoL-Pi, Stax, and others lives elsewhere.

## Current status

Tactus is currently in the **architecture/bootstrap stage**. This repository
documents the intended system composition and boundaries and now contains the
first domain core: the Work Order lifecycle model and its dependency graph. It
does **not** yet implement scheduling, triage/recovery policy, adapters, or any
orchestration functionality.

Implemented so far (`src/tactus/domain/`):

- the five-state Work Order lifecycle (`DRAFT | OPEN | ACTIVE | IMPLEMENTED |
  RETIRED`) with transition guards and transition provenance;
- orthogonal `OPEN` readiness (`UNKNOWN | READY | BLOCKED`), which is not a
  lifecycle transition;
- `FailureObservation` attached to an `ACTIVE` Work Order without changing its
  lifecycle state;
- a first-class, acyclic Work Order dependency graph with atomic edge rewrites.

Ictus compatibility boundary (`src/tactus/integrations/ictus/`):

- a versioned, fail-closed compatibility boundary for Ictus
  `ExecutionObservation` v1 (pinned to Ictus `833175d`), with a conservative
  Tactus → Ictus category mapping and full evidence/provenance preservation.
  See [the observation contract](docs/architecture/ICTUS_OBSERVATION_CONTRACT.md).

## Development

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).

```bash
uv run pytest            # run the domain test suite
scripts/verify-goal.sh   # repository verifier gate
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — high-level plane model and ownership
  boundaries.
- [Detailed control-plane architecture](docs/architecture/WHITEBOARD_OVERVIEW.md)
  — [Work Order lifecycle](docs/architecture/WORK_ORDER_LIFECYCLE.md),
  [scheduling and backends](docs/architecture/SCHEDULING_AND_BACKENDS.md),
  [triage and recovery](docs/architecture/TRIAGE_AND_RECOVERY.md), and
  [human intervention](docs/architecture/HUMAN_INTERVENTION.md).
- [Roadmap](docs/ROADMAP.md) — ordered implementation milestones and the
  executable GitHub backlog.
- [Dependencies](docs/DEPENDENCIES.md)
- [Contributing](CONTRIBUTING.md)
