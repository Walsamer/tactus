# AgentRuntime

`AgentRuntime` is the agent execution layer, distinct from sandboxing.

```text
AgentRuntime
├── Pi
│   └── SoL-Pi
└── future agent implementations
```

Pi is the initial/default agent runtime. SoL-Pi is an optional
optimization/extension layer around Pi.

Agent choice is orthogonal to sandbox choice (see `../sandbox/`). For example:

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
