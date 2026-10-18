# Tactus — project agent entry point

This repository is **Tactus** (Fleet project id `tactus`): the control plane —
validated intents, decision vocabulary, StateSnapshots, recovery decisions and
the cross-system contract.

## Boundaries — keep the systems separate

- **Tactus** (this repo, `Walsamer/tactus`) owns control-plane state and the
  cross-system contract.
- **Ictus** (`Walsamer/ictus`, Fleet project `agentic-control`) is the
  execution / Dagster bridge. Do not implement execution or submission here, and
  do not edit the Ictus repository from a Tactus Work Order.
- **Fleet** (`../dev-ops-fleet`) is the controller. Its
  [`AGENTS.md`](../dev-ops-fleet/AGENTS.md) is authoritative for the controller;
  this file is authoritative for this repository.

## Read first

- `docs/architecture/CROSS_SYSTEM_CONTRACTS.md`
- `docs/architecture/EXECUTION_AND_RECOVERY.md`
- `docs/architecture/OWNERSHIP_BOUNDARIES.md`
- `docs/architecture/BASELINE_V1_RECONCILIATION.md`
- `integrations/ictus/README.md` and `integrations/ictus/conformance.py`
  (`PINNED_ICTUS_COMMIT`).

## Hard rules

- The Ictus pin is exact (`PINNED_ICTUS_COMMIT`, mirrored in CI and docs). Never
  float it. Advance it only through an authorized Work Order; `TCT-WO-003` advanced
  it to `f4581f9` and is merged via PR #35.
- Do not implement Ictus/Dagster execution semantics in this repository.
- Do not edit the Ictus repository from a Tactus Work Order.
- Run the registered verifier `./scripts/verify-goal.sh`; do not weaken gates.
- Never push, deploy, or publish from here; Fleet owns integration and promotion.
- **Publication is a PR by default.** A request to "push <branch>" means open a
  pull request for review, not a direct push. Direct pushes to `main` require an
  explicit operator override in the same request; force-push is never allowed.

## State

`main` pins Ictus at `f4581f94906d07decd3aca6ad3e83ae42e6cf9ae`, advanced via PR #35.
The older `chore/advance-ictus-pin-f4581f9` branch is superseded by that merge.
Never float the pin; advance it only through an authorized Work Order.
