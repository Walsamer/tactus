# SandboxRuntime

`SandboxRuntime` is an abstraction, not a specific sandbox.

```text
SandboxRuntime
├── OpenShellBackend
└── OtherSandboxBackend
```

OpenShell is the initial/default candidate backend. Other implementations may
be added later. Tactus is not tightly coupled to OpenShell; the sandbox
implementation must remain replaceable without changing Tactus, Ictus, or
Dagster.

Sandbox choice is orthogonal to agent choice (see `../agents/`).
