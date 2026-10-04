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

## Ownership boundaries

### Tactus — control plane (first-party)

Tactus is the master/system repository and owns the control plane:

- Work Order lifecycle
- triggers
- scheduling/coordination
- capability selection
- execution requests
- system configuration
- integration adapters
- component registration
- aggregated observability
- future CLI/API/UI

Tactus decides and coordinates **what should happen**. It must not reimplement
functionality already owned by lower layers, and it must not build another
workflow/retry engine (Dagster owns that).

### Ictus — decision plane (first-party external)

Ictus is first-party but independently versioned and independently
publishable/useful. It owns:

- typed decisions
- policy
- capability validation
- approvals
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

### Dagster — execution plane (third-party)

Dagster is the third-party execution substrate. It owns:

- durable execution
- run/step persistence
- retries
- re-execution
- execution dependencies
- execution history
- execution observability

Tactus/Ictus decide what should happen; Dagster owns **durably executing it**.

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

## Dependency classification

- **First-party (Tactus-owned):** Tactus itself.
- **First-party external:** independently versioned, first-party systems such as
  Ictus.
- **Third-party:** externally maintained systems such as Dagster, Metaxy,
  OpenShell, Pi, SoL-Pi, Stax, and Pixi.
- **Private / out of scope:** systems explicitly reserved but not part of the
  current architecture (for example Arcventory — see
  `integrations/private/arcventory/`).
