# End-to-end execution validation

Status: bounded runtime validation implemented for TCT-WO-004 (one success/restart
slice) and TCT-WO-005 (transient, terminal and escalation failure scenarios).

This document records the real, executable seam proof across Tactus, the pinned
Ictus decision/receipt bridge, and Dagster. It is deliberately narrower than a
production control loop: the harness drives one admitted decision, submits one
durable intent, simulates a Tactus process loss after send, reconciles the
receipt, applies one observation, and verifies one effect.

## What is exercised

`scripts/validate_execution_slice.py` performs this sequence:

1. Builds a versioned Tactus `StateSnapshot` (`RECOVERY` profile) with the
   canonical semantic attempt count/limit and a diagnostic-only step-retry
   index.
2. Runs the real pinned `ictus decide --approve` binary. The decision envelope
   is an Ictus `ALLOW` with an `ExecutionIntent`; Tactus does not synthesize it.
3. Applies the validated recovery decision through the existing closed
   `DecisionApplier`, persists the `ACTIVE -> OPEN + READY` projection, and
   acquires a fenced durable claim.
4. Accepts the exact Ictus intent through `SqliteExecutionAdmission` before any
   external send. The attempt and submission identities are durable.
5. Submits the intent through the pinned Ictus Dagster bridge to a real
   filesystem-backed `DagsterInstance` using `QueuedRunCoordinator` and
   `DefaultRunLauncher`, with an independent `dagster-daemon` loading
   `ictus_dagster.definitions`.
6. Simulates a Tactus crash after send/before receipt by closing and reopening
   the SQLite ledger. The pending submission is found, the same intent is
   resent, and Dagster returns the same run ID.
7. Strictly parses the terminal Ictus result as an `ExecutionObservation` v1
   envelope. Before applying it, the harness proves the boundary fails closed:
   an unknown receipt category and a malformed receipt are rejected, and
   correlations with a mismatched execution, intent, Work Order or intent
   digest are all denied.
8. Correlates the real observation with the recorded
   subject/intent/attempt/submission and run ID, then applies the success
   outcome. The attempt closes, the result inbox has one row, and the Work Order
   becomes `IMPLEMENTED`. Replaying the same observation applies no second
   effect.
9. Runs real Ictus boundary checks showing that a large step-retry index leaves
   the semantic decision `REEXECUTE`, while an exhausted canonical semantic
   budget (`3/3`) produces `ESCALATE`.

The harness asserts exactly one Dagster run, one execution attempt, one
submission and one applied result. The evidence record contains the pinned Ictus
commit and binary SHA-256, Dagster version, decision envelope, receipt IDs, run
ID, intent digest, observation, correlation identities and final Work Order
state.

## Reproduce

Prerequisites (offline-capable):

* the pinned Ictus checkout at the commit in
  `src/tactus/integrations/ictus/observation.py` (`ICTUS_COMMIT`);
* its existing `.venv` with Dagster 1.13.x and the `ictus_dagster` package;
* its already-built `target/debug/ictus` binary;
* no network access or dependency installation.

From the Tactus worktree:

```bash
TACTUS_ICTUS_ROOT=/path/to/agentic-control \
  scripts/run-execution-slice.sh \
  --evidence /tmp/tactus-execution-slice-evidence.json \
  --timeout 120
```

`scripts/run-execution-slice.sh` uses
`${TACTUS_ICTUS_ROOT}/.venv/bin/python` by default; override it with
`TACTUS_ICTUS_PYTHON` when the venv is elsewhere.

A passing run prints the Dagster run ID, Tactus attempt ID and evidence path, and
writes a JSON record with `"result": "passed"`.

The normal offline verifier remains:

```bash
TACTUS_ICTUS_ROOT=/path/to/agentic-control \
TACTUS_ICTUS_TARGET_DIR=/path/to/agentic-control/target \
  scripts/verify-goal.sh
```

Those two variables point the existing real-boundary conformance tests at the
pinned checkout and its prebuilt target; the verifier then runs the
unit/contract suite, including the new execution-cycle tests. The runtime slice
is intentionally not part of the default verifier because it requires the
pinned Ictus checkout, its venv and its prebuilt binary.

## Boundary ownership and the one temporary shim

The production boundary remains:

```text
Dagster / Ictus runtime
  -> Ictus ExecutionResult
  -> Ictus-owned ExecutionResult.to_observation()
  -> ExecutionObservation v1
  -> Tactus strict parse + correlation + outcome
```

The pinned Ictus Python bridge returns a validated `ExecutionResult` payload but
does not yet expose the Rust `ExecutionResult::to_observation()` conversion to
Python callers. For this bounded validation only, the harness copies the
Ictus-produced `observation.category` and `evidence` verbatim into the v1
observation envelope, then Tactus strictly parses and correlates that envelope.
The shim does not classify failures, invent categories, retry steps, choose a
route or make policy. It lives in `scripts/`, not `src/`, so production Tactus
code still cannot originate an `ExecutionObservation`.

Follow-up: expose the Ictus-owned result-to-observation conversion at the Python
boundary and replace the harness shim with that call. No Tactus production module
needs to change when that upstream API lands.

## Execution-failure scenarios (TCT-WO-005)

`scripts/validate_failure_scenarios.py` (wrapper: `scripts/run-failure-scenarios.sh`)
adds the failure half of the vertical slice. It leaves the success/restart slice
above untouched and reuses the same real boundaries: the pinned `ictus` binary,
the pinned `ictus_dagster.submission` bridge, a real filesystem `DagsterInstance`
with `QueuedRunCoordinator` plus an independent `dagster-daemon`, and Tactus's
durable admission, correlation and decision application.

### What is exercised

| Scenario | Execution facts | Tactus facts |
| --- | --- | --- |
| `transient` | execute attempt 1 fails, Dagster retries the step, the run succeeds | one semantic attempt, attempt closed, `IMPLEMENTED` |
| `terminal` | every execute attempt fails; the run is `FAILURE` and normalizes to `PROCESS_CRASH` | failure observation applied once (attempt closed, Work Order `ACTIVE`), then a real RECOVERY decision (`REEXECUTE`, `ALLOW`, intent present) authorizes exactly one new attempt |
| `escalation` | same terminal failure with an exhausted semantic budget (`3/3`) | the real `ESCALATE` decision (no intent) applies one audited `OPEN + BLOCKED` effect with a typed intervention request that survives a process restart |
| `duplicate` (asserted inside the scenarios above) | resubmitting the same intent returns the same Dagster run ID | replayed observations and replayed decisions apply no second effect |

### How the failure is produced without touching authorization

The pinned Ictus revision derives `ExecutionIntent.arguments` from Ictus's own
policy provider (`crates/policy/src/intent.rs` copies `proposal.arguments`; the
rules provider only sets `recovery_outcome`). There is therefore **no caller
channel** for execution knobs, and Tactus must not create one: injecting
`fail_until_attempt`/`fail_hard` into an authorized intent would change what was
authorized.

In production a capability simply fails; nobody injects the failure. These
scenarios reproduce that shape:

1. Ictus authorizes an intent whose `arguments` are its own (`{}` on the INITIAL
   path). Tactus admits and submits that exact payload.
2. The Ictus bridge submits by **code-location name**
   (`ICTUS_DAGSTER_CODE_LOCATION`) into the daemon's workspace. The harness
   registers a Tactus-owned capability location
   (`integrations/dagster/definitions.py`) under that name, so the *capability
   implementation* is Tactus-owned while submission, receipts and result mapping
   stay Ictus-owned.
3. That implementation reads a bounded, out-of-band JSON control file keyed by
   the run's `ictus.intent_id` tag, registered **after** authorization, and fails
   the requested attempts. `RetryPolicy(max_retries=3)` on the execute step keeps
   retry mechanics Dagster's.
4. The step names match the bridge's `FAILED_STEP_CATEGORY` table, so a terminal
   execute failure normalizes to `PROCESS_CRASH` and Ictus — not Tactus — decides
   what that means.

The mandatory invariant is asserted in every scenario:

```text
submitted ExecutionIntent == Ictus-authorized ExecutionIntent
```

including an identical `arguments` map, with `fail_attempts`/`fail_hard`/
`fail_until_attempt`/`fail_verify`/`hold_seconds` absent from the intent at every
level. `integrations/dagster/failure_control.py` also rejects those spellings so
they cannot silently reappear as intent arguments.

### Reproduce

```bash
TACTUS_ICTUS_ROOT=/path/to/agentic-control   scripts/run-failure-scenarios.sh   --evidence /tmp/tactus-failure-scenarios-evidence.json
```

Each scenario gets its own temporary SQLite ledger, `DAGSTER_HOME`, workspace and
daemon. The evidence record carries, per scenario: the Dagster run IDs, the
capability attempt ledger (attempt number, outcome, programme), the terminal
observation, the real recovery decision, the durable row counts, and the
intent-equality digests.

### What these scenarios do not claim

* They do not execute an agent or a code change; the capability is a bounded
  no-op whose only variable behaviour is failure.
* They do not prove Tactus-owned PR publication, workspace isolation or
  scheduling (Tactus Issues #22/#23 remain unimplemented and out of scope).
* The escalation evidence persists as the audited decision row, the guarded
  `OPEN + BLOCKED` projection and the typed intervention on the application
  record; there is no separate intervention table to restart from.

## Scope explicitly left outside this slice

* Automatic background receipt reconciliation. The harness demonstrates
  caller-driven reconciliation of the same durable submission, which matches the
  Ictus bridge contract.
* Production deployment, remote workers, multi-host submission coordination,
  operator UI, GitHub work-source ingestion and autonomous task decomposition.
