# dagster

Dagster owns durable run/step execution, queueing, concurrency and execution-level
retry. Ictus owns semantic recovery. Dagster launches its workers; a runtime adapter
inside a capability supervises the agent.

See [Architecture Baseline v1](../../docs/architecture/SYSTEM_ARCHITECTURE.md).

## Contents

| Path | Owner | Purpose |
| --- | --- | --- |
| `definitions.py` | Tactus | A **Tactus-owned capability code location** used only by the execution-failure scenarios. Same job name and `settings` resource shape the pinned Ictus bridge expects; the capability implementation fails deterministically on request. |
| `failure_control.py` | Tactus | Bounded, Dagster-free failure-control surface (JSON file keyed by `ictus.intent_id`, loaded **after** authorization). Rejects intent-argument spellings so execution knobs cannot creep back into an `ExecutionIntent`. |

This directory is **not** the production Ictus execution backend. Ictus keeps the
production bridge (`ictus_dagster`: submission, receipts, query, result mapping,
workflow registry). The Tactus location exists so `TCT-WO-005` can exercise real
transient and terminal execution failures without modifying the pinned Ictus
revision, its policy, or an authorized `ExecutionIntent`.

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
