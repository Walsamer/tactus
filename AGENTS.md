# Tactus — project agent entry point

This repository is **Tactus** (Fleet project id `tactus`): the control plane,
and the single authority for Work Order state, lifecycle, coordination and
outcomes.

## Ownership boundaries — keep the systems separate

Normative detail: [`docs/architecture/OWNERSHIP_BOUNDARIES.md`](docs/architecture/OWNERSHIP_BOUNDARIES.md).

- **Tactus** (this repository, `Walsamer/tactus`) — authoritative Work Order
  state, lifecycle, coordination and outcomes: identity, dependencies,
  readiness, claim/lease fencing, human intervention records, applying a
  validated recovery decision, split/replan children, and acceptance of
  completion. Also the factual backend registry.
- **Ictus** (`Walsamer/ictus`, Fleet project `agentic-control`) — decision
  policy, admission and recovery: capability/authorization decisions, backend
  and model compatibility routing, sufficiency of human grants, and bounded
  semantic recovery (re-execute, reroute, decompose, escalate, abort).
- **Dagster** (inside Ictus) — durable execution orchestration: runs, steps,
  queue and concurrency limits, run history and execution retries.
- **Runtime adapters** (inside a Dagster capability) — agent execution and
  supervision: starting, restricting, supervising and cancelling the agent
  process runtime.
- **Fleet v3** (`../dev-ops-fleet`) — the existing controller during migration:
  Work Order authoring, dispatch, integration and promotion today; Tactus is its
  successor for domain authority. Fleet's
  [`AGENTS.md`](../dev-ops-fleet/AGENTS.md) is authoritative for the controller;
  this file is authoritative for this repository.

Do not implement another plane's responsibility here, and never edit another
repository from a Tactus Work Order.

## Read first

- `docs/architecture/CROSS_SYSTEM_CONTRACTS.md`
- `docs/architecture/EXECUTION_AND_RECOVERY.md`
- `docs/architecture/OWNERSHIP_BOUNDARIES.md`
- `docs/architecture/BASELINE_V1_RECONCILIATION.md`
- `integrations/ictus/README.md` and `integrations/ictus/conformance.py`
  (`PINNED_ICTUS_COMMIT`).

## Hard rules

- The Ictus pin is exact and single-source: `PINNED_ICTUS_COMMIT` in
  `integrations/ictus/conformance.py` and `ICTUS_COMMIT` in
  `src/tactus/integrations/ictus/observation.py`, mirrored in
  `.github/workflows/ci.yml` and the docs. Never float it. Advance it only
  through an authorized Work Order (`TCT-WO-003` advanced it to `86816d6`,
  merged via PR #37).
- Do not implement Ictus/Dagster execution semantics in this repository.
- Do not edit the Ictus repository from a Tactus Work Order.
- Run the registered verifier `./scripts/verify-goal.sh`; do not weaken gates.
  It includes `scripts/check-agent-consistency.py`, which fails on stale agent
  instructions or pin references.
- Never push to `main`, force-push, deploy or publish directly. Fleet owns
  integration and promotion; GitHub remains authoritative for refs and PRs.
- **Publication is a PR by default.** A request to "push <branch>" means open a
  pull request for review, not a direct push. Direct pushes to `main` require an
  explicit operator override in the same request; force-push is never allowed.
- Keep this file and the pin references consistent in the same change: run
  `python3 scripts/check-agent-consistency.py` before publishing.

## State

`main` pins Ictus at `e7419210d5bdfec3169e375d4957077951cc231c`, advanced via
PR #37 (`chore(ictus): advance the pinned Ictus revision to 86816d6`).
Never float the pin; advance it only through an authorized Work Order.
