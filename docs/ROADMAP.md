# Shared GitHub roadmap — Architecture Baseline v1

[GitHub Issues](https://github.com/Walsamer/tactus/issues) are canonical shared
implementation intent. This document is navigation, not a second backlog.

| Milestone | Outcome |
|---|---|
| M0 — Architecture Convergence | Review the paired baseline and reconcile current Fleet intake. |
| M1 — Control / Decision Boundary | Typed contexts/decisions, facts, durable admission and domain application. |
| M2 — Minimal Vertical Slice | Five-scenario Issue → local execution → verified domain outcome proof. |
| M3 — Runtime + Development Workflow | Real agent, Stax publication, safe decomposition. |
| M4 — Fleet Migration | Shadow, bounded authority transfer and rollback. |
| M5 — Production Hardening | Operational recovery, run families, security and server portability. |

Older M-numbers/session plans are historical. Existing Tactus issues are updated
or explicitly superseded; Ictus receives a linked backlog. Dependencies are full
Issue URLs in each issue body. Only an explicit fleet:ready label permits intake
after source-revision and current Fleet project/configuration gates are satisfied.
Initial automation queue is empty pending those gates and baseline review.

Use the issue template and preserve the chain:
Architecture → Issue → human branch OR Fleet WorkOrder + branch → PR → review → main.
