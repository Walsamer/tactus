# Containment (TCT-WO-022 phase 2)

Status: **the boundary is implemented, enforced at spawn time, and proven with
real child processes.** The one real-runtime canary is *not* run: it needs
explicit operator approval and a scoped provider credential, and it ships as an
operator command (`scripts/run-agent-canary.sh`) rather than a code path only its
author can drive. Nothing here publishes: `publication_authority` stays `false`
in every attempt account.

This document states exactly what the boundary enforces, what it merely records,
and what it does **not** claim. Every enforcement claim names the test that
proves it; a claim without a test does not belong here.

## The boundary

```text
RuntimeSupervisor.run(request)
  1  preflight(descriptor)                 artifact digests + launcher digest (executes nothing)
  2  acquire writer lock                   ownership before mutation
  3  render containment.sb into the attempt directory
  4  denial matrix (real children)         write/read/egress/nesting/signal, through the same
                                           launcher and the same profile as the launch
     └─ not ok ⇒ nothing is spawned; the attempt record carries `invalid`
  5  spawn                                 pinned launcher → pinned runtime, attempt-local
                                           HOME/TMPDIR/git config, allowlisted env names only,
                                           task on stdin
  6  deadline / cancellation               SIGTERM then SIGKILL, whole process group
  7  termination confirmed                 group gone **and** no process carries the attempt
                                           marker (`TACTUS_ATTEMPT_DIR=<attempt dir>`)
  8  artifact validation, terminal record, release
```

Two properties make this an enforcement port rather than an observation port:

* **The tool that verifies is the tool that runs.** The launcher is pinned by
  path and digest, its `argv_prefix[0]` must be itself, and the runtime's version
  probe is executed *through* the launcher inside the denial matrix — so a
  descriptor cannot verify one binary and launch another
  (`tests/runtime/test_runtime_contract.py::test_launcher_is_pinned_and_the_probe_is_deferred_to_the_self_test`,
  `…::test_launcher_command_prefixes_the_pinned_runtime`).
* **A boundary that cannot be proven starts nothing.** A failed matrix, a
  launcher digest drift, a workspace inside a denied root, or an unreadable
  runtime leaves the attempt record `invalid` with no child and no patch
  (`tests/runtime/test_runtime_supervision.py::test_a_boundary_that_cannot_be_proven_starts_nothing`).

## The profile

Rendered from data by `src/tactus/runtime/containment.py` (`ContainmentPolicy`);
the pin supplies denied roots, egress mode and the runtime's recorded
allowances. The shape is:

```scheme
(version 1)
(deny default)                        ; Mach IPC and network denied by construction
(allow process-exec* process-fork)
(allow signal (target same-sandbox))  ; own children only, never the supervisor
(allow sysctl-read)
(allow file-read* (subpath "/"))      ; reads are a deny-list — see "Not claimed"
(deny file-read* (subpath "<denied root>")) …   ; one per configured root
(allow file-write* (subpath "<workspace>") (subpath "<attempt dir>") …)  ; exact allowlist
(allow <recorded allowance>) …        ; e.g. ipc-posix-shm, only if the pin records it
(deny network*)                       ; or (allow network-outbound (remote ip "localhost:PORT"))
                                      ; or (allow network*) for an approved canary only
```

`(deny default)` is not decoration: it is what closes **Mach IPC**, and that is
what closes the Keychain. Measured on this host: with `(allow default)` plus a
file-level denial of `$HOME`, `security find-generic-password` still *answered*
(`The specified item could not be found`); under `(deny default)` it fails at the
XPC layer instead. A path-only denial does not close the Keychain.

## Enforced — each with its proof

| Property | Mechanism | Test |
|---|---|---|
| Write inside the workspace succeeds | write allowlist contains workspace + attempt dir | `test_containment.py::test_self_test_proves_every_denial` |
| Write outside is refused, and leaves nothing | write allowlist is exact | same |
| Reads of a denied root are refused | one `deny file-read*` per configured root | same |
| Outbound network is refused (deny egress) | `(deny network*)` | same (connect probe reports `EPERM`) |
| Egress allowlist is exact, not approximate | `(allow network-outbound (remote ip "localhost:PORT"))` | `…::test_self_test_records_the_localhost_allowance_as_two_checks` |
| A child cannot widen its own sandbox | nested `sandbox_apply` is refused | `test_self_test_proves_every_denial` |
| A child cannot signal outside its sandbox | `(allow signal (target same-sandbox))` | same |
| The version probe runs inside the boundary | probe executed through the launcher | `…::test_self_test_runs_the_runtime_probe_through_the_launcher` |
| A launcher that ignores its profile is caught | every check is a real child | `…::test_self_test_catches_a_launcher_that_does_not_apply_the_profile` |
| The task never appears in argv | prompt written to the attempt dir, fed on stdin | `test_runtime_supervision.py::test_the_task_is_fed_on_stdin_never_on_the_command_line` |
| Only allowlisted credential names reach the child | env allowlist, values from the supervisor | `…::test_allowlisted_credentials_reach_the_child_and_nothing_else_does`, `…::test_child_environment_is_minimal_and_attempt_local` |
| Terminated means nothing of the attempt is alive | attempt-marker sweep after killpg, trusted only once a planted marker is visible | `test_containment.py::test_residual_processes_finds_a_detached_child_then_nothing`, `test_runtime_supervision.py::test_a_blind_sweep_never_reads_as_clear` |
| The launcher that was proven is the launcher that runs | policy and descriptor launchers must match by path, prefix and digest | `test_runtime_supervision.py::test_a_policy_launcher_that_differs_from_the_descriptor_is_refused` |
| A launcher pinned by path alone is refused | `Launcher.verify` requires a digest | `test_containment.py::test_a_launcher_pinned_by_path_alone_is_refused` |
| Attempt state may not sit inside the workspace | the write allowlist would cover the recovery record | `test_runtime_supervision.py::test_a_state_directory_inside_the_workspace_is_refused_when_contained` |
| A survivor keeps the workspace owned, also across restarts | recorded marker swept before a previous attempt may be `recovered` | `test_runtime_supervision.py::test_a_surviving_marker_keeps_the_workspace_owned` |
| Artifact drift is refused before anything runs | file digest, tree manifest digest + file count | `test_runtime_contract.py::test_tree_artifact_digest_drift_fails_closed`, `…::test_an_artifact_without_a_digest_is_refused` |
| Launcher drift is refused before anything runs | pinned launcher digest | `…::test_launcher_digest_mismatch_fails_closed` |
| A workspace inside a denied root is refused | policy validation before rendering | `test_containment.py::test_workspace_inside_a_denied_root_is_refused` |
| A runtime inside a denied root is refused | policy validation before rendering | `…::test_runtime_inside_a_denied_root_is_refused` |
| Tree digests are reproducible | path-sorted `sha256  relpath` manifest, order declared | `…::test_tree_digest_is_order_independent_and_change_sensitive` |

## Measured on this host (macOS 27.0, build 26A428, arm64)

These are the denials the matrix observes, run as real children:

| Probe | Observed |
|---|---|
| write inside the workspace | succeeds |
| write to a sibling path outside it | `Operation not permitted`, nothing created |
| read `~/.ssh/id_ed25519`, `~/.codex/auth.json`, `~/.gitconfig` | `Operation not permitted` |
| read the Ictus checkout on `/Volumes` | `Operation not permitted` |
| `ssh-add -l` | `Error connecting to agent: Operation not permitted` |
| `security find-generic-password` | refused at the XPC layer (Keychain closed) |
| `gh auth status` | `not logged into any GitHub hosts` (no credential reachable) |
| outbound connect, deny egress | `EPERM` |
| `git push` to a remote outside the workspace | rejected; the remote's ref list is unchanged |
| nested `sandbox-exec -p '(allow default)'` | `sandbox_apply: Operation not permitted` |
| `kill` of the supervisor or of pid 1 | `Operation not permitted` |

Cost, measured rather than estimated: **7 checks in 0.17 s cold / 0.12 s warm**
per attempt; artifact digest **~1.0 s** for a single 246 MB binary, **~3.8 s cold
/ 1.7 s warm** for a 14,922-file tree (a node-bundle runtime). Runtime startup
added by the boundary is the launcher process only (`sandbox-exec` execs in
place): the runtime's own startup dominates.

## Egress

Three modes, and nothing implicit:

| Mode | Profile | Use |
|---|---|---|
| `deny` (default) | `(deny network*)` | standing posture; a locally served model needs no egress at all |
| `localhost:<port>` | one local port | a locally served model |
| `open` | `(allow network*)` | **only** a bounded, operator-approved canary |

Seatbelt cannot express a host allowlist: `remote ip` accepts only `*` or
`localhost` (`sandbox-exec: host must be * or localhost in network address`).
A hosted provider therefore means either open egress for the length of one run,
or a local allow-listing proxy in front of it. The canary uses `open`, records
`"egress": {"mode": "open"}` in its evidence, and everything else stays denied —
no readable credential, no SSH agent, no `gh`, workspace-only writes, no
publication authority.

## Failing closed

| Situation | Behaviour |
|---|---|
| Launcher missing, not executable, or digest drift | `ContainmentUnavailable` before any child exists |
| `sandbox-exec` absent (Apple has deprecated it) | same: the launcher is pinned, so absence is drift |
| Denial matrix not fully green | nothing spawned, record `invalid`, writer lock released (no child existed) |
| Workspace or runtime inside a denied root | refused before the profile is rendered |
| Artifact digest or file count drift | `RuntimePreflightError` before the lock |
| Attempt marker still in the process table | termination stays unconfirmed, lock held, status `unterminated`; a later attempt records `unresolved` and requires an operator |
| Survivor sweep itself unavailable (`ps` fails) | treated as unverifiable ⇒ not confirmed |
| Survivor sweep cannot see a planted marker | treated as unverifiable ⇒ not confirmed, and a later attempt records `unresolved` |
| Containment policy and descriptor name different launchers | refused before a child exists |
| Launcher declared without a digest | refused: a path-only pin can be swapped in place |
| State directory inside the workspace while contained | refused before the profile is rendered |

Re-recording a pin (a new macOS build changes `/usr/bin/sandbox-exec`) is an
operator change reviewed like any other: the launcher digest and the host build
live in the pin file, and a mismatch is a refusal, never a warning.

## Not claimed

* **Reads are a deny-list.** Everything is readable except the configured denied
  roots. Any sensitive path that is not configured is readable, including other
  attempt directories under the same state root (requests and prompts, never
  credentials). Callers must pass every sensitive root they know about.
* **`sandbox-exec` is deprecated** by Apple. It works today; a release that
  removes it fails closed rather than running uncontained. The alternatives
  (container, distinct account) are not implemented.
* **The profile is not portable across OS updates.** Two constructs that look
  reasonable abort `sandbox-exec` on this host (`require-not` in a filter, and a
  filtered `(allow file-read* …)` other than `(subpath "/")`, which is why reads
  are expressed as a deny-list). The self-test is what catches a semantic change;
  a silent weakening is not possible, because every claim above is a check that
  must pass before a spawn.
* **Allowances are permissions too.** Codex 0.162.1 needs `(allow ipc-posix-shm)`
  to start a thread under `(deny default)`; that is a same-user shared-memory
  channel. Pinned runtimes record their allowances in the pin file, so an
  allowance can be reviewed and cannot grow silently. Pi 1.3.0 needs none.
* **The sweep is a hygiene gate, not an adversarial one.** It marks every child
  with a per-run variable and finds survivors by that marker; a survivor that
  clears its own environment (`env -u`) is not found. What it does catch is the
  normal case — a `nohup`, `setsid` or background helper an agent left behind —
  and its blindness is now measured (a planted marker must be visible) rather than
  assumed.
* **`workspace-diff` needs git.** The mode stages the workspace and diffs against
  the revision recorded before the child spawned, so a run that stages or commits
  its own work is still accounted for; a workspace that is not a git checkout is
  refused in that mode rather than reported as an empty diff.
* **No protection against the operator's own account.** The child runs as the
  same user; the boundary denies paths, IPC and network, it does not create a
  security principal. A distinct account or a VM remains the stronger option.
* **Not publication.** Nothing here pushes, opens a PR or holds a publishing
  token; publication is a separate authorization (Issue #23).
