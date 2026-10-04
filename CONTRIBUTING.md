# Contributing

Tactus is in the architecture/bootstrap stage. Keep contributions small,
bounded, and aligned with the ownership boundaries described in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and the
[detailed control-plane architecture](docs/architecture/WHITEBOARD_OVERVIEW.md).

## Collaboration flow

```text
Architecture/docs → GitHub Issue → branch → PR → review → main
```

- `main` should remain working. Do not push architecture or implementation
  changes directly to `main` while review is active; open a PR instead.
- One bounded issue should normally produce one PR. Avoid implementing
  unrelated issues in the same branch.
- If a change alters the architecture, update the relevant architecture docs in
  the same PR so the docs remain the source of truth.
- Keep the existing plane model intact:

  ```text
  Tactus → Ictus → Dagster + Metaxy → Runtime/Sandbox → Capabilities
  ```

- Respect ownership boundaries. Do not import logic from another plane:
  Tactus owns lifecycle/coordination, Ictus owns typed decisions and policy,
  Dagster owns durable execution, and the runtime/sandbox implementations stay
  replaceable.
- Capture unresolved semantic choices as explicit issues or ADRs rather than
  guessing in code.

The executable backlog lives in GitHub Issues, derived from
[docs/ISSUE_PLAN.md](docs/ISSUE_PLAN.md). The docs describe the architecture;
the issues describe bounded work.
