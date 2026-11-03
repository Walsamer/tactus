# ictus

Ictus owns typed decisions, policy, capability validation, routing and approval requirements. Tactus persists human grants/interventions and applies validated effects. Ictus remains an independent repository.

The outbound Tactus → Ictus boundary is a **versioned** `StateSnapshot` profile
(`snapshot.profile_version = 2`, phases `INITIAL`/`RECOVERY`, canonical semantic
count/limit, raw backend and authorization facts, deterministic digest). See
[the contract](../../docs/architecture/ICTUS_STATE_SNAPSHOT_CONTRACT.md). The
inbound `ExecutionObservation` boundary is strict and fail-closed. The inbound
decision boundary consumes the validated `DecisionEnvelope`.

## Consuming the validated decision

`tactus.integrations.ictus.envelope` admits a decision from Ictus and normalizes
either accepted wire shape into one `DecisionAdmission`:

| Shape | `DecisionSource` | Notes |
|---|---|---|
| `DecisionEnvelope` | `ENVELOPE` | Validated and versioned; carries `outcome`, `validation` and an `expiry` |
| `{proposal, policy_decision, execution_intent}` | `LEGACY_TRIPLE` | **Deprecated**, unvalidated; kept only while no `--context` producer exists |

A payload carrying both shapes, or neither, is refused rather than guessed.

- **Version policy.** Envelope `schema_version` 1 and 2 are accepted; anything
  else is refused as an unsupported version. Version 1 is the frozen
  capability-bearing shape and always carries `capability_validation`; version 2
  carries it exactly when the decision carries a capability
  (`EXECUTE_CAPABILITY`, `REEXECUTE`, `ROUTE`).
- **Outcomes.** `EXECUTABLE` is the only outcome with an `ExecutionIntent`.
  `NO_EXECUTION` was validated and permitted and reaches no execution backend, so
  the effect comes from `proposal.decision`. `DENIED`, `PENDING`, `NO_ROUTE` and
  `INVALID` are returned as typed non-authorising results — no Work Order
  transition, no intervention request, no decision-ledger row, and no exception,
  because a well-formed refusal is an answer and not a fault. Only a malformed
  payload raises.
- **Expiry** is compared to `validation.validated_at` (fact time), never to a
  wall clock.
- **Ledger digests** are versioned: new rows are `sha256:v1:` prefixed, an
  existing bare 64-hex row reads as `sha256:v0`, and comparing two versions
  raises an immutable-record conflict.
- **DECOMPOSE** is refused with `ProposalBindingUnverifiedError` and creates no
  children: `verify_proposal_binding` is the single hook that fails closed, and
  the canonical `validation.proposal_digest` is not re-verified against the
  published `contracts/digest-vectors.json` here yet.

The work-order effect is unchanged: the same total decision-to-effect mapping and
the same decision ledger apply, whichever shape the decision arrived in.

## Real-boundary conformance

`conformance.py` runs the deterministic fixtures through the **actual** pinned
Ictus binary (no Python policy mirror). It verifies the exact source revision
and builds `ictus` into a disposable target directory:

```sh
TACTUS_ICTUS_ROOT=/path/to/agentic-control \
  uv run python integrations/ictus/conformance.py \
  tests/integrations/ictus/data/initial_snapshot.v2.json \
  tests/integrations/ictus/data/recovery_snapshot.v2.json
```

By default it reports the legacy triple that `ictus decide` still emits. With
`--context` it wraps the same fixtures in a `DecisionContext` and reports the
validated envelope from `ictus decide --context`: `shape`, `outcome`, `verdict`,
`proposal.decision`, `has_intent` and the process return code. A decision is
recognized by shape, never by the exit code — exit 3 now means "validated but not
executable" as well as "refused". The context builder lives in the harness as
evidence code; Tactus has no runtime `--context` producer yet.

```sh
TACTUS_ICTUS_ROOT=/path/to/agentic-control \
  uv run python integrations/ictus/conformance.py --context \
  tests/integrations/ictus/data/initial_snapshot.v2.json \
  tests/integrations/ictus/data/recovery_snapshot.v2.json
```

| Item | Value |
|---|---|
| Pinned Ictus repository | `Walsamer/ictus` |
| Pinned Ictus commit | `c2aa092c3e31b8dc5a6db6d126b1ce9c5089b2f1` |
| Overrides | `TACTUS_ICTUS_ROOT`, `TACTUS_ICTUS_TARGET_DIR` |

The normal test entry point (`uv run pytest`) executes the same boundary; a
missing or off-pin checkout fails visibly instead of skipping.

## GitHub Actions

`.github/workflows/ci.yml` provisions the boundary rather than skipping it:

1. checks out Tactus;
2. checks out `Walsamer/ictus` at the exact pinned commit into `.ictus/`;
3. verifies `git -C .ictus rev-parse HEAD` against the pin before any build;
4. installs a stable Rust toolchain and runs `cargo fetch --locked` so the
   harness's `cargo build --offline --locked` can resolve from the local cache;
5. exports `TACTUS_ICTUS_ROOT=.ictus` and
   `TACTUS_ICTUS_TARGET_DIR=.ictus/target` for `uv run pytest` and
   `scripts/verify-goal.sh`.

The conformance build writes only to the isolated `TACTUS_ICTUS_TARGET_DIR`
(`.ictus/target`, which Ictus gitignores); the pinned checkout itself stays
clean. A missing checkout, a pin mismatch, a build failure, a non-JSON payload
or a decision-contract regression fails the job; nothing is silently skipped.
CI runs the report twice — the legacy triple and the validated envelope — so the
deprecated shape stays visibly exercised while it is still supported.

See [Architecture Baseline v1](../../docs/architecture/SYSTEM_ARCHITECTURE.md).
