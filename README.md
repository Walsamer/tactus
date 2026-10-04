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
documents the intended system composition and boundaries; it does not yet
implement Work Orders, scheduling, triage, adapters, or any orchestration
functionality.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Dependencies](docs/DEPENDENCIES.md)
