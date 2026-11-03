# dagster

Dagster owns durable run/step execution, queueing, concurrency and execution-level
retry. Ictus owns semantic recovery. Dagster launches its workers; a runtime adapter
inside a capability supervises the agent.

See [Architecture Baseline v1](../../docs/architecture/SYSTEM_ARCHITECTURE.md).

## Contents

| Path | Owner | Purpose |
| --- | --- | --- |
| `definitions.py` | Tactus | A **Tactus-owned capability code location** used only by the execution-failure scenarios. Same job name and `settings` resource shape the pinned Ictus bridge expects; the capability implementation fails deterministically on request. |
| `agent_location.py` | Tactus | The Tactus-owned code location for `software.implement` and `software.promote`: it **registers Ictus's** `software_agent_job` / `software_publish_job` unchanged and re-binds their settings resources so `runtime_seam` (from `TACTUS_RUNTIME_SEAM`) is a resource default. It defines no steps of its own, because Dagster pins the recording location's step keys on the run. Unset means `None`, which keeps Ictus's fail-closed placeholder. |
| `agent_backend.py` | Tactus | Dagster-free: the seam backend Ictus resolves by name (`agent_backend:create_backend`). Runs one supervised attempt through the pin and reports it as `runtime-evidence.schema.json`. It never uses `control.spawn` — the supervisor owns the child's process group and confirms its termination — so the `termination` it reports is ours, not Ictus's. |
| `agent_attempt.py` | Tactus | Dagster-free attempt body: operator runtime pin (`config/agent-runtime.json`), digest-verified executable, one supervised attempt, patch evidence re-checked, durable attempt account. |
| `failure_control.py` | Tactus | Bounded, Dagster-free failure-control surface (JSON file keyed by `ictus.intent_id`, loaded **after** authorization). Rejects intent-argument spellings so execution knobs cannot creep back into an `ExecutionIntent`. |

This directory is **not** the production Ictus execution backend. Ictus keeps the
production bridge (`ictus_dagster`: submission, receipts, query, result mapping,
workflow registry). The Tactus location exists so `TCT-WO-005` can exercise real
transient and terminal execution failures without modifying the pinned Ictus
revision, its policy, or an authorized `ExecutionIntent`.

`agent_location.py` is the same pattern one step further: a Tactus-owned
*capability implementation* for `software.implement`. Because Dagster replays the
execution plan recorded by Ictus's own job definition, that job must declare the
same steps as the Ictus job — see
[`AGENT_EXECUTION_SLICE.md`](../../docs/architecture/AGENT_EXECUTION_SLICE.md).

**Containment status (Issue #22 phase 2).** The runtime supervisor can now enforce
OS containment — a pinned seatbelt launcher with the runtime's version probe run
through it, a profile rendered per attempt, a denial matrix proven with real
children before anything is spawned, the task on stdin instead of argv,
attempt-local `HOME`/`TMPDIR`/git config, an allowlist of environment variable
*names*, and a survivor sweep that must come back clear before termination is
confirmed and the writer lock released — and every one of those proofs is named in
[`RUNTIME_SUPERVISION.md`](../../docs/architecture/RUNTIME_SUPERVISION.md), with
the attempt contract in
[`AGENT_ADAPTER_CONTRACT.md`](../../docs/architecture/AGENT_ADAPTER_CONTRACT.md).
The backend runs behind it, but the shipped pin still declares no launcher:
`config/agent-runtime.json` carries identity plus `runtime_caps` (the runtime's own
containment settings the runtime enforces itself — capture/patch/result byte caps,
changed-path cap, termination grace, poll interval) and treats them as strictly
tighter than the seam's bounds, not as another opinion about them, and no `write_scope`, deadline or step budget, which come
from the attempt request. A pin that named a launcher would get the boundary; this
one does not, so the attempt is still uncontained and its account says
`isolation: none`.

## Boundary rules

* The authorized `ExecutionIntent` is submitted unchanged
  (`submitted == authorized`, including `arguments`); failure configuration lives
  outside the intent.
* Retry mechanics stay Dagster's (`RetryPolicy` on the execute step, attempt from
  `context.retry_number`); Tactus's semantic attempt count never moves because of
  a step retry.
* Step names match the bridge's `FAILED_STEP_CATEGORY` table so a terminal execute
  failure normalizes to `PROCESS_CRASH` and Ictus decides what it means.
* Nothing here classifies failures, chooses a route, retries semantically or
  originates an `ExecutionObservation`.
* The capability work is a **real OS subprocess** per attempt
  (`capability_process.py`): fresh temporary working directory, minimal
  `PATH`-only environment, 30 s deadline, captures bounded **while reading**
  (4096 bytes per stream, then the child is killed and reaped), and the real
  pid/exit code/digests recorded as evidence. A non-zero exit is a step
  `Failure`, so the run fails and normalizes to `PROCESS_CRASH`.
* **This is process hygiene, not a sandbox.** The child runs as the same OS
  account with the same filesystem and network reach; `python -I` only prevents
  inherited interpreter configuration. No filesystem or network isolation is
  claimed, and the evidence record states `isolation: none`.

Reproduce:

```bash
TACTUS_ICTUS_ROOT=/path/to/agentic-control scripts/run-failure-scenarios.sh
```
