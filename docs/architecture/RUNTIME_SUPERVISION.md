# Runtime supervision (TCT-WO-022a)

Status: bounded supervision port implemented; **no real coding agent attached yet**
(Issue #22 continues with a pinned real runtime).

Tactus owns the *process* of one agent attempt. This document states exactly what
is enforced, what is merely observed, and what is deliberately **not** claimed.

## What the supervisor owns

`tactus.runtime` supervises exactly one child process per attempt:

```text
RuntimeSupervisor.run(request)
  preflight(pinned descriptor)          -> RuntimeProvenance (executable sha256 + version)
  acquire writer lock                   -> exclusive per workspace state dir
  write request.json                    -> attempt_id, intent_id, capability, workspace, limits
  spawn (start_new_session=True)        -> new process group
  enforce deadline / cancellation       -> SIGTERM, then SIGKILL, to the whole group
  confirm termination                   -> group no longer signalable
  release writer lock                   -> only after confirmation
  validate agent artifacts              -> changed paths + patch within declared bounds
  return RuntimeEvidence                -> JSON, no secrets
```

## Enforced (and tested)

| Property | Mechanism | Test |
|---|---|---|
| Pinned runtime, no fallback | `RuntimeDescriptor` (exact argv + probe + version token); `preflight` fails closed on any mismatch | `test_preflight_fails_closed_on_version_mismatch`, `…_missing`, `…_not_executable` |
| Explicit write scope | `Workspace(allowed_scope=…)`; validated on changed paths **and** patch headers | `test_changed_paths_must_be_declared_inside_the_scope`, `test_patch_may_not_touch_undeclared_paths`, `test_path_escape_is_detected_and_rejected` |
| Patch bounds | `max_patch_bytes`, `max_changed_paths` | `test_oversized_patch_is_rejected` |
| Deadline | `timeout_seconds` covers the whole exchange | `test_timeout_terminates_the_tree_within_the_deadline` |
| Cancellation | `RuntimeSupervisor.cancel(reason)` | `test_cancellation_terminates_the_tree_and_records_the_reason` |
| Process-tree termination | `start_new_session=True`; `killpg` SIGTERM → SIGKILL; confirmation by observing the group gone | `test_crash_…`, timeout/cancel tests |
| No lock release while termination is unknown | `WriterLock.release()` raises unless `confirm_termination()` | `test_writer_lock_refuses_release_until_termination_is_confirmed`, `test_unconfirmed_termination_keeps_the_lock_and_reports_unterminated` |
| Exactly one writer | `flock(LOCK_EX|LOCK_NB)` per workspace state dir | `test_a_held_writer_lock_excludes_a_second_writer` |
| Restart safety | durable `attempt.json`; `recover()` terminates a leftover group before anything else | `test_recover_terminates_a_process_group_left_by_a_previous_process` |
| Structured evidence | exit code, pid/pgid, timing, signals, patch digest, changed paths, validation errors | `test_success_returns_structured_evidence_and_releases_the_lock` |
| No publication authority | child env is `PATH` + two Tactus ids only; no `HOME`, no tokens | `test_agent_environment_carries_no_publication_credentials` |

## Deliberately not claimed

* **Not a sandbox.** The child runs as the same OS account with the same
  filesystem and network reach. Write scope is *detected and rejected after the
  fact*, not prevented. Filesystem/network confinement requires an OS mechanism
  and is not part of this port.
* **Not an isolation boundary for the workspace.** Git-worktree isolation is
  Issue #23 (Stax); this port only requires a workspace directory and a scope.
* **Not publication.** Nothing here pushes, opens a PR, or reads credentials.
  Publication is a separate authorization (Issue #23) and cannot be inferred
  from a successful run.
* **Not a scheduler or retry engine.** One attempt, one process; retry policy
  stays with Ictus/Dagster semantics already in force.

## Agent contract (v1)

The supervisor writes `request.json` and the agent must write into `output_dir`:

```json
// request.json
{"attempt_id": "...", "intent_id": "...", "capability": "software.implement",
 "workspace": {"root": "/abs/ws", "allowed_scope": ["src"]},
 "output_dir": "/abs/state/agent-output", "limits": {...}}
```

```json
// result.json
{"schema_version": 1, "status": "completed" | "failed",
 "changed_paths": ["src/example.py"], "summary": "..."}
// patch.diff — unified diff; every header path must appear in changed_paths
```

A `completed` result without a patch, with a path outside scope, with an
undeclared patch header, or with an oversized patch is recorded as
`invalid` — never as success.

## Evidence statuses

| Status | Meaning |
|---|---|
| `success` | process settled, exit 0, artifacts within contract |
| `failed` | process settled but the agent failed (non-zero exit / no result / self-reported failure) |
| `timeout` | deadline reached; group terminated and confirmed |
| `cancelled` | `cancel()` was requested; group terminated and confirmed |
| `invalid` | artifacts violate the contract (scope, patch bounds, schema) |
| `unterminated` | termination could not be confirmed; **writer lock retained** |
