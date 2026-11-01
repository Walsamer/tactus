# Runtime supervision (TCT-WO-022a)

Status: **the supervision port is implemented and hardened, and it becomes an
enforcement port whenever the descriptor pins a launcher.** The boundary itself —
rendered profile, pinned launcher, denial matrix, survivor sweep — is documented
in [`CONTAINMENT.md`](CONTAINMENT.md); the coding-agent adapter contract in
[`AGENT_ADAPTER_CONTRACT.md`](AGENT_ADAPTER_CONTRACT.md). It has a caller: the
Tactus-owned Dagster code location runs one supervised attempt per
`software.implement` run ([`AGENT_EXECUTION_SLICE.md`](AGENT_EXECUTION_SLICE.md)),
and that pin pins no launcher, so the attempt it runs is still uncontained.
**No real coding agent is attached yet**: no real runtime is pinned in this
repository, and the one real-runtime canary has not been run.

Tactus owns the *process* of one agent attempt. This document states exactly what
is enforced, what is merely observed, and what is deliberately **not** claimed.
Each claim in the enforced table names the test that proves it; a claim without
a test is not in that table.

## Lifecycle (ordering is the contract)

```text
run(request)
  1  preflight(pinned descriptor)        artifact digests + launcher digest; with a launcher it executes nothing
  2  acquire writer lock                 flock(LOCK_EX|LOCK_NB) — exclusive ownership FIRST
  3  settle previous attempt             attempt-marker sweep, then verified termination — or refuse (fail closed)
  4  create attempts/<slug>-<nonce>/     unique evidence dir; nothing is ever deleted or reused
  5  write request.json                  inside the attempt dir
  6  write attempt.json phase=spawning   before the child exists
  7  write prompt.txt (task on stdin)    an empty task is refused for a runtime that reads one
  8  render containment.sb               profile rendered per attempt, from the pinned policy
  9  denial matrix (real children)       same launcher, same profile as the launch; not ok ⇒ nothing is spawned
 10  spawn (start_new_session=True)      launcher → runtime; attempt-local HOME/TMPDIR/git config; allowlisted names only
 11  write phase=running + pgid + start marker
 12  deadline / cancellation loop        SIGTERM, then SIGKILL, whole group
 13  termination confirmation            group no longer signalable **and** no process carries the attempt marker
 14  artifact validation                 bounded reads; scope/patch/filesystem checks
 15  write terminal record               phase=settled | unterminated | invalid
 16  mark settled, release lock          only if 13 and 15 both hold
```

Three invariants fall out of this order, and all three are tested:

* **Nothing is touched before ownership.** A concurrent attempt fails at step 2
  having created, modified and deleted nothing
  (`tests/runtime/test_runtime_supervision.py::test_concurrent_attempt_cannot_touch_the_first_attempts_state`).
* **The lock outlives the process.** Release requires *termination confirmed* and
  *terminal record written*. A failed artifact validation still writes the record
  first, so the workspace is never falsely marked available
  (`tests/runtime/test_runtime_supervision.py::test_writer_lock_refuses_release_until_termination_is_confirmed`).
* **A boundary that cannot be proven starts nothing.** A failed matrix, a launcher
  digest drift or a workspace inside a denied root spawns no child, leaves the
  record `invalid` and releases the lock, because there is nothing to own
  (`tests/runtime/test_runtime_supervision.py::test_a_boundary_that_cannot_be_proven_starts_nothing`).

When the descriptor pins no launcher, steps 8–9 collapse to nothing and step 10
spawns the runtime directly: there is no profile and no matrix, the attempt
evidence records `containment.enabled=false` with the reason
(`src/tactus/runtime/supervision.py:719`), and the child keeps the account's
filesystem and network reach (see "Deliberately not claimed").

## Result contract (artifact shapes)

Two shapes are expressible, and the descriptor picks one:

| `result_contract` | The runtime writes | The supervisor takes |
|---|---|---|
| `agent-artifacts` (default) | `result.json` and `patch.diff` in the attempt output | the declared paths and the patch, re-read under a hard byte cap |
| `workspace-diff` | nothing | the changed paths and the patch, from the workspace's own `git diff` |

`workspace-diff` exists because a general-purpose coding CLI speaks neither the
Tactus artifact format nor a diff on request; accounting for it by an artifact
format it does not have would have recorded a correct run as a failure. In that
mode the diff is the supervisor's, its headers are still checked against the
declared paths, and its size is still capped by `max_patch_bytes`
(`tests/runtime/test_runtime_supervision.py::test_workspace_diff_contract_needs_no_agent_artifacts`).

## Recovery contract (fail-closed)

A durable `attempt.json` survives the supervising process. On the next attempt
(or an explicit `recover()`), the marker sweep runs first — a helper can outlive
its own group — and only then the recorded process identity is judged:

| Recorded phase | Process identity | Action |
|---|---|---|
| *(none)* / `settled` / `recovered` / `resolved` | — | proceed |
| any phase that has not settled | attempt marker still in the process table | mark `unresolved`; require operator; **never `recovered`** |
| any phase that has not settled | survivor sweep unverifiable (`ps` unavailable) | mark `unresolved`; require operator |
| `running` | group gone or pgid now foreign | mark `recovered`; **no signal** |
| `running` | start marker **matches** the live group | terminate, verify, mark `recovered` |
| `running` | start marker **differs** (pgid reused) | mark `recovered`, `reused_pgid=true`; **never signal** |
| `running` | identity **unverifiable** (`ps` unavailable) | mark `unresolved`; require operator |
| `spawning` | unknown (crash before/inside spawn) | mark `unresolved`; require operator |
| `unterminated` | any | re-attempt verification; otherwise `unresolved`; require operator |
| `unresolved` | any | refuse; require operator |

`run()` refuses with `RuntimeRecoveryRequired` and **mutates nothing** (the record
of the unresolved attempt is preserved). The only way past a hold is:

* verified recovery (terminate + confirm), or
* `RuntimeSupervisor.resolve_unresolved(operator=…, reason=…)` — an explicit,
  audited human decision. It signals nothing.

Every recovery action is appended to `recovery.jsonl`.

**Why the start marker exists.** A recorded pgid may have been recycled by an
unrelated process after the original group emptied. Signalling it because "the
number is still there" would kill someone else's work, so the supervisor stores
the OS start-time marker at spawn and only signals when it still matches
(`tests/runtime/test_runtime_supervision.py::test_recover_never_signals_a_reused_pgid`).

**Why the attempt marker exists.** A process group can be empty while a detached
helper still runs, so a group that is gone is not termination. The per-run marker
(`TACTUS_ATTEMPT_DIR=<attempt dir>`) is inherited by every descendant and swept
after every group kill and again before a restart; a recorded marker still
present in the process table makes the previous attempt `unresolved`, never
`recovered` (`tests/runtime/test_runtime_supervision.py::test_a_surviving_marker_keeps_the_workspace_owned`).
The marker is the attempt directory name, not the logical attempt id, so two runs
of the same intent cannot block each other.

## Enforced (and tested)

| Property | Mechanism | Proof |
|---|---|---|
| Pinned runtime, no fallback | exact argv template + probe; there is no discovery and no alternative runtime | `tests/runtime/test_runtime_contract.py::test_preflight_records_the_pinned_runtime_provenance`, `…::test_preflight_fails_closed_on_version_mismatch` |
| Pinned artifact set | one file digest, or a tree manifest digest in a declared order plus its file count; an artifact without a digest is refused | `tests/runtime/test_runtime_contract.py::test_file_artifact_pin_is_verified_and_recorded`, `…::test_tree_artifact_pin_is_verified_with_its_manifest`, `…::test_tree_artifact_digest_drift_fails_closed`, `…::test_tree_artifact_file_count_drift_fails_closed`, `…::test_an_artifact_without_a_digest_is_refused` |
| Pinned launcher, verified before anything runs | launcher path + digest; `argv_prefix[0]` must be the launcher; drift refuses | `tests/runtime/test_runtime_contract.py::test_launcher_digest_drift_fails_before_anything_runs`, `tests/runtime/test_containment.py::test_launcher_digest_mismatch_fails_closed`, `…::test_launcher_prefix_must_name_the_launcher_itself` |
| The version probe runs *inside* the boundary | with a launcher pinned, preflight executes nothing; the probe runs inside the denial matrix | `tests/runtime/test_runtime_contract.py::test_launcher_is_pinned_and_the_probe_is_deferred_to_the_self_test`, `tests/runtime/test_containment.py::test_self_test_runs_the_runtime_probe_through_the_launcher` |
| The runtime that runs is the launcher-prefixed one | `command()` prefixes the pinned launcher and substitutes the profile | `tests/runtime/test_runtime_contract.py::test_launcher_command_prefixes_the_pinned_runtime` |
| Writes are an allowlist | `(allow file-write* …)` names the workspace and the attempt dir only | `tests/runtime/test_containment.py::test_profile_is_deny_by_default_with_an_exact_write_allowlist`, `…::test_self_test_proves_every_denial` |
| Reads of the configured denied roots are refused | one `deny file-read*` per configured root — a deny-list, see below | `tests/runtime/test_containment.py::test_self_test_proves_every_denial` |
| A workspace or runtime inside a denied root is refused | policy validation before the profile is rendered | `tests/runtime/test_containment.py::test_workspace_inside_a_denied_root_is_refused`, `…::test_runtime_inside_a_denied_root_is_refused` |
| Egress is exact per mode, never implicit | `deny` / `localhost:<port>` / `open` render different profile lines | `tests/runtime/test_containment.py::test_egress_sbpl_is_explicit_per_mode`, `…::test_self_test_records_the_localhost_allowance_as_two_checks`, `…::test_egress_rejects_an_unknown_mode` |
| A launcher that ignores its profile is caught | every check is a real child through the same launcher | `tests/runtime/test_containment.py::test_self_test_catches_a_launcher_that_does_not_apply_the_profile` |
| A child cannot widen its own sandbox, or signal outside it | nested `sandbox_apply` fails; signals are `(target same-sandbox)` | `tests/runtime/test_containment.py::test_self_test_proves_every_denial` |
| The boundary is proven before the agent process exists | matrix inside `run()`, after the lock and before the spawn | `tests/runtime/test_runtime_supervision.py::test_containment_is_proven_before_the_agent_runs` |
| A proven policy is the one that runs | the profile digest in the evidence is the matrix's digest | `tests/runtime/test_runtime_supervision.py::test_containment_is_proven_before_the_agent_runs` |
| A descriptor launcher without a policy, or a policy without a launcher, is refused | constructor invariant | `tests/runtime/test_runtime_supervision.py::test_a_descriptor_launcher_without_a_policy_is_refused`, `…::test_a_policy_without_a_pinned_launcher_is_refused` |
| The task never appears in argv | prompt written to the attempt dir and fed on stdin | `tests/runtime/test_runtime_supervision.py::test_the_task_is_fed_on_stdin_never_on_the_command_line` |
| An empty task is refused for a runtime that reads one | supervisor refuses before spawning; the adapter refuses before an attempt dir exists | `tests/runtime/test_runtime_supervision.py::test_a_runtime_that_needs_a_task_refuses_an_empty_one`, `tests/integrations/agents/test_agent_adapter.py::test_a_runtime_that_reads_a_task_refuses_an_empty_one` |
| Attempt-local HOME, TMPDIR and git config | `_child_environment` points all three into the attempt dir | `tests/runtime/test_runtime_supervision.py::test_child_environment_is_minimal_and_attempt_local` |
| Only allowlisted credential *names* reach the child | values come from the supervisor, never from the ambient environment | `tests/runtime/test_runtime_supervision.py::test_allowlisted_credentials_reach_the_child_and_nothing_else_does`, `…::test_agent_environment_carries_no_publication_credentials` |
| Exclusive ownership before mutation | lock acquired before any state write; per-attempt directories, never reused | `tests/runtime/test_runtime_supervision.py::test_a_held_writer_lock_excludes_a_second_writer`, `…::test_each_attempt_gets_its_own_evidence_directory` |
| Explicit write scope | declared paths **and** patch headers validated; absolute paths, `..`, undeclared headers rejected | `tests/runtime/test_runtime_supervision.py::test_path_escape_is_detected_and_rejected`, `…::test_patch_may_not_touch_undeclared_paths` |
| Patch / result bounds | `stat` size checked before reading; reads capped (`max_patch_bytes`, `max_result_bytes`) | `tests/runtime/test_runtime_supervision.py::test_oversized_patch_is_rejected_before_being_loaded`, `…::test_oversized_result_is_rejected_before_being_loaded` |
| Deadline / cancellation | whole-exchange deadline; `cancel(reason)`; whole group | `tests/runtime/test_runtime_supervision.py::test_timeout_terminates_the_tree_within_the_deadline`, `…::test_cancellation_terminates_the_tree_and_records_the_reason` |
| Process-tree termination | `start_new_session`; `killpg` SIGTERM → SIGKILL; confirmation by observation | `tests/runtime/test_runtime_supervision.py::test_crash_is_recorded_as_failed_with_confirmed_termination` |
| Terminated means nothing of the attempt is alive | killpg, then the attempt-marker sweep | `tests/runtime/test_containment.py::test_residual_processes_finds_a_detached_child_then_nothing`, `tests/runtime/test_runtime_supervision.py::test_a_surviving_marker_keeps_the_workspace_owned` |
| A survivor keeps the workspace owned | the sweep gates `termination.confirmed`, so the lock is not released | `tests/runtime/test_runtime_supervision.py::test_a_surviving_marker_keeps_the_workspace_owned` |
| No lock release while unknown | release requires confirmed termination **and** a written terminal record | `tests/runtime/test_runtime_supervision.py::test_unconfirmed_termination_keeps_the_lock_and_reports_unterminated` |
| Restart safety | marker sweep, then marker-verified recovery, explicit `unresolved`, operator resolution | `tests/runtime/test_runtime_supervision.py::test_recover_marks_unverifiable_identity_as_unresolved`, `…::test_crash_while_spawning_blocks_new_writers`, `…::test_operator_resolution_releases_the_hold_and_is_audited` |
| Machine-checkable evidence | `status`, pid/pgid, signals, timings, declared vs patch paths, filesystem check, containment policy, profile digest, matrix checks, survivors | `tests/runtime/test_runtime_supervision.py::test_success_returns_structured_evidence_and_releases_the_lock` |

Two refusals exist in the code and are **not** covered by a test, so they are
listed here as behaviour, not as proof: preflight refuses an argv whose `argv[0]`
is not the pinned executable, so one binary cannot be verified and another
launched (`src/tactus/runtime/descriptor.py:411`), and a run without a pinned
launcher records `containment.enabled=false` (`src/tactus/runtime/supervision.py:719`).

Statuses: `success | failed | timeout | cancelled | invalid | unterminated`.

## Deliberately not claimed

* **Reads are a deny-list, not an allowlist.** The profile allows
  `(allow file-read* (subpath "/"))` and then denies one subtree per configured
  denied root, so everything outside those roots stays readable — including other
  attempt directories under the same state root (requests and prompts, never
  credentials). Callers must pass every sensitive root they know about; an
  unlisted path is readable, and when no file exists under a configured root to
  probe, the read check is recorded as skipped, never as passed
  (`src/tactus/runtime/containment.py:77`).
* **`sandbox-exec` is deprecated by Apple, and a host without it fails closed.**
  The launcher is pinned, so its absence is drift: `ContainmentUnavailable`
  before any child exists
  (`tests/runtime/test_containment.py::test_launcher_missing_fails_closed`,
  `…::test_which_launcher_reports_the_host_tool_when_present`). No alternative
  boundary (container, distinct account) is implemented.
* **Open egress is a real hole, and only an approved canary may have it.**
  `(allow network*)` is its own mode, never a default, and it is recorded as such
  (`tests/runtime/test_containment.py::test_egress_sbpl_is_explicit_per_mode`);
  the rendered policy, egress mode included, is copied into the attempt's
  containment evidence (`src/tactus/runtime/supervision.py:811`). No attempt has
  run with it: the canary that would has not been run, so the evidence path for
  `open` is unobserved end to end.
* **Not verified filesystem enforcement of the outcome.** The supervisor compares
  declared paths with patch headers, and with a workspace diff only when a
  provider is supplied (`filesystem.verified=false` otherwise). The matrix proves
  what the OS refuses; it does not inspect every read or write the child
  attempted.
* **No protection against the operator's own account.** The child is the same
  user. Path, IPC and network denial is not a separate security principal; a
  distinct account or a VM remains the stronger option.
* **Not publication.** Nothing here pushes, opens a PR, or holds a token.
  Publication is a separate authorization (Issue #23) and cannot be inferred from
  a successful run.
* **Without a pinned launcher nothing changed.** Same OS account, same filesystem
  and network reach; write scope is *detected and rejected after the fact*, never
  prevented. The containment block in the evidence says so
  (`containment.enabled=false`) and this port is then an observation port, not a
  sandbox.

## Prerequisite for a real coding agent (what phase 2 closed, what is still open)

Phase 2 closed the four questions this section used to list as open, and each
closure is the testable claim above rather than a promise:

1. **Filesystem containment** — the agent must not be able to read the operator's
   home, other repositories, or the state directory. Writes are an allowlist and
   reads of the configured denied roots are refused; reads remain a deny-list, so
   this is only as good as the configured roots.
2. **Network containment** — no outbound access by default (`deny network*`); one
   local port when a locally served model needs it; open egress only for a
   bounded, approved canary.
3. **Credential containment** — attempt-local `HOME`/`TMPDIR`/git config, git
   system config pinned to `/dev/null`, and only the variable *names* the
   descriptor allowlists. `(deny default)` also closes Mach IPC, which is what
   closes the Keychain; the denial of `security` and `gh` was measured on the
   host (see [`CONTAINMENT.md`](CONTAINMENT.md)), not asserted by a test here.
4. **Termination authority** — whole-group SIGTERM/SIGKILL plus the attempt-marker
   sweep, so a detached helper keeps the workspace owned instead of being missed.
   Two independent checks run: the attempt-marker sweep (`ps -E`, which on macOS
   hides the environment of some binaries) and `lsof` holders of the workspace and
   attempt directory (which does not depend on the environment, and catches a
   detached helper that inherited the workspace as its cwd —
   `tests/runtime/test_runtime_supervision.py::test_a_survivor_is_found_even_when_its_environment_is_invisible`).
   If neither check can run, the result is `unverifiable` and the lock stays held
   (`…::test_an_unavailable_survivor_check_never_reads_as_clear`). A survivor that
   holds none of those paths and exposes no environment is not detectable: the
   checks are a hygiene gate against stray helpers, not an adversarial boundary.

What is still open, and why no real agent is started:

1. **No real runtime is pinned on the Dagster route.** `config/agent-runtime.json`
   still pins the deterministic fake agent and declares no `containment` block, so
   every attempt through `software_agent_job` is fake and uncontained. The loader
   *does* accept `containment`, `artifacts`, `env_allowlist`, `stdin_from` and
   `result_contract`, so a pin that names a launcher gets the boundary
   (`integrations/dagster/agent_attempt.py`). The only real-runtime pin is the
   canary's own (`config/agent-runtime-canary.json`), run by hand.
2. **The one canary has not been run.** A hosted model needs open egress for the
   length of one run and a scoped provider credential: both need explicit operator
   approval, and the operator's subscription quota was spent on the earlier
   measurement work.
3. **The durable half waits on Ictus B4** (see
   [`AGENT_EXECUTION_SLICE.md`](AGENT_EXECUTION_SLICE.md)); it is proven only in a
   throwaway clone.

Until (1) and (2) are closed, `022b` stays blocked and `023a` may only claim
**Git worktree isolation**, not process or network isolation.
