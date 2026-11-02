# Containment (TCT-WO-022 phase 2)

Status: **the boundary is implemented, enforced at spawn time, and proven with
real child processes.** The one real-runtime canary runs as an operator command
(`scripts/run-agent-canary.sh`), and it has been run: under the shipped pin the
pinned CLI starts inside the boundary and authenticates with the staged session
credential, and then ends before the model call (measured; see "Reading a canary
verdict" for what that does and does not mean). Nothing here publishes:
`publication_authority` stays `false` in every attempt account.

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
(allow <recorded allowance>) …        ; e.g. ipc-posix-shm, or one named mach service,
                                      ; only if the pin records it
(deny network*)                       ; or (allow network-outbound (remote ip "localhost:PORT"))
                                      ; or (allow network*) for an approved canary only
```

`(deny default)` is not decoration: it is what closes **Mach IPC**, and Mach IPC
is what closes the Keychain — a path-level denial alone does not. Measured on
this host: with `(allow default)` plus a file-level denial of `$HOME`,
`security find-generic-password` still *answered* (`The specified item could not
be found`); under `(deny default)` it fails at the XPC layer instead.

### The three mach services the pinned runtime needs

A deny-default profile is not free for a hosted runtime. Measured with
`codex-cli 0.162.1` and the shipped pin's profile: with `ipc-posix-shm` alone the
CLI cannot start at all — `thread/start failed: failed to load configuration:
Failed to synchronize managed preferences (code -32600)` — because it
synchronises its managed preferences through `cfprefsd`, which Mach IPC denial
also closes. The canary pin therefore records exactly three named services and
nothing else:

| Allowance | Why the runtime needs it |
|---|---|
| `mach-lookup (global-name "com.apple.cfprefsd.daemon")` | managed-preferences sync (the `cfprefsd` daemon) |
| `mach-lookup (global-name "com.apple.cfprefsd.agent")` | managed-preferences sync (the per-user agent) |
| `mach-lookup (global-name "com.apple.SecurityServer")` | TLS trust evaluation via securityd; without it every HTTPS call fails `invalid peer certificate: UnknownIssuer` |

The honest sentence is this: **the CLI cannot start under a deny-default profile
without them, so the profile grants three named mach services and nothing else.**
A blanket `(allow mach-lookup)` is exactly what must not land, and does not:
allowances are listed in the pin file, so each one is reviewable and cannot grow
silently.

What those three services do and do not grant the child is measured, not assumed
(see "Measured on this host"). In short: the child can now *use* the Security
framework and its own preferences domain — it can talk to securityd, list its
keychain search list (`/Library/Keychains/System.keychain`), and create and use a
keychain *file of its own* inside its writable workspace. It still cannot read
the operator's keychain stores (`~/Library/Keychains/login.keychain-db` is
`Operation not permitted`), and the operator's preferences stay out of reach
because the child's `HOME` is attempt-local (`defaults read com.apple.dock` is
`Domain not found` inside the boundary and outside it for that home).

**What is deliberately not proven: reading an individual keychain item.** A child
with securityd access can *ask*; whether an item comes back depends on that item's
ACL, and answering the authorisation prompt is something a headless sandboxed child
cannot do. Testing it would have meant touching the operator's real secrets, and a
machine-specific test that only passes where a particular item happens to exist is
worse than no test: it would look like coverage and prove nothing. So the denial
matrix proves the portable half — the store *file* stays denied
(`read_denied_keychain_store`) — and this paragraph states the other half as
unmeasured rather than dressed up.

## Enforced — each with its proof

| Property | Mechanism | Test |
|---|---|---|
| Write inside the workspace succeeds | write allowlist contains workspace + attempt dir | `test_containment.py::test_self_test_proves_every_denial` |
| Write outside is refused, and leaves nothing | write allowlist is exact | same |
| Reads of a denied root are refused | one `deny file-read*` per configured root | same |
| A keychain store stays unreadable whatever mach services are allowed | the `~/Library/Keychains` denial is a path denial, independent of `SecurityServer` | same (`read_denied_keychain_store`), `tests/canary/test_run_agent_canary.py::test_the_denial_matrix_covers_the_mach_allowances_and_the_keychain_store` |
| A staged session credential exists only for the attempt | attempt-home seed written at `0600` before the spawn, removed when the child is gone | `test_runtime_supervision.py::test_a_seeded_home_file_is_readable_only_during_the_attempt` |
| Outbound network is refused (deny egress) | `(deny network*)` | same (connect probe reports `EPERM`) |
| Egress allowlist is exact, not approximate | `(allow network-outbound (remote ip "localhost:PORT"))` | `…::test_self_test_records_the_localhost_allowance_as_two_checks` |
| A child cannot widen its own sandbox | nested `sandbox_apply` is refused | `test_self_test_proves_every_denial` |
| A child cannot signal outside its sandbox | `(allow signal (target same-sandbox))` | same |
| The version probe runs inside the boundary | probe executed through the launcher | `…::test_self_test_runs_the_runtime_probe_through_the_launcher` |
| A launcher that ignores its profile is caught | every check is a real child | `…::test_self_test_catches_a_launcher_that_does_not_apply_the_profile` |
| The task never appears in argv | prompt written to the attempt dir, fed on stdin | `test_runtime_supervision.py::test_the_task_is_fed_on_stdin_never_on_the_command_line` |
| Only allowlisted credential names reach the child | env allowlist, values from the supervisor | `…::test_allowlisted_credentials_reach_the_child_and_nothing_else_does`, `…::test_child_environment_is_minimal_and_attempt_local` |
| A staged session file lives only as long as the attempt | written into the attempt's own home at `0600` before the spawn, unlinked in the supervisor's `finally` | `…::test_a_seeded_home_file_is_readable_only_during_the_attempt` |
| Terminated means nothing of the attempt is alive | two independent checks after killpg: attempt-marker sweep, and `lsof` holders of the workspace/attempt dir | `test_containment.py::test_residual_processes_finds_a_detached_child_then_nothing`, `test_runtime_supervision.py::test_a_survivor_is_found_even_when_its_environment_is_invisible` |
| An unavailable check never reads as "clear" | both checks unavailable ⇒ `unverifiable`, lock held | `test_runtime_supervision.py::test_an_unavailable_survivor_check_never_reads_as_clear` |
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

Re-measured on macOS 27.2 (build 26B123) after that update replaced
`/usr/bin/sandbox-exec`: the launcher digest moved and was re-recorded, the
denial matrix still holds 8/8, and the rows about the subscription backend and
about the staged session credential were measured again (one contained run, one
run of the same staged home and argv outside the boundary). The platform-key row
is the 27.0 observation.

These are the denials the matrix observes, run as real children:

| Probe | Observed |
|---|---|
| write inside the workspace | succeeds |
| write to a sibling path outside it | `Operation not permitted`, nothing created |
| read `~/.ssh/id_ed25519`, `~/.codex/auth.json`, `~/.gitconfig` | `Operation not permitted` |
| read `~/Library/Keychains/login.keychain-db` | `Operation not permitted` — the keychain *store* stays denied at the file level even with `SecurityServer` allowed |
| read the Ictus checkout on `/Volumes` | `Operation not permitted` |
| `ssh-add -l` | `Error connecting to agent: Operation not permitted` |
| `security find-generic-password` | refused at the XPC layer (Keychain closed) **only** while no mach allowance is declared. With the canary's three services it reaches securityd: `security list-keychains` answers `/Library/Keychains/System.keychain`, a keychain *file* can be created in the writable workspace, an item added to it and read back. No operator item is readable without its ACL, and the operator's stores are unreadable (row above) |
| `defaults read com.apple.dock` | `Domain not found` — the child's `HOME` is attempt-local, so it syncs its own preferences domain, not the operator's |
| `gh auth status` | `not logged into any GitHub hosts` (no credential reachable) |
| outbound connect, deny egress | `EPERM` |
| start the pinned CLI under the profile with `ipc-posix-shm` only | `Failed to synchronize managed preferences (code -32600)` — never reaches the model call |
| the same with the three named mach services, a bogus platform key | reaches the model call: `401 Unauthorized … auth error code: invalid_api_key` on `wss://api.openai.com/v1/responses` |
| the same with the operator's session file staged | auth accepted (no 401); the contained attempt ends before the model call: `workspace routing discovery failed` against `chatgpt.com/backend-api/ps/mcp`, retried 5/5, then `turn.failed` |
| the same staged home and argv (`HOME` seeded with `.codex/auth.json` alone, no `config.toml`) with **no** boundary | the edit completes: exit 0, 11.5 s, exactly `src/calc.py` and `tests/test_calc.py`, no 401 and no `ps/mcp` request at all |
| `curl` of that endpoint, in the attempt's own profile and uncontained | `451`, `{"message":"no_biscuit_no_service"}`, both ways; sibling paths `/backend-api/me` `401`, `/models` and `/conversation/init` `403` |
| `git push` to a remote outside the workspace | rejected; the remote's ref list is unchanged |
| nested `sandbox-exec -p '(allow default)'` | `sandbox_apply: Operation not permitted` |
| `kill` of the supervisor or of pid 1 | `Operation not permitted` |

### Reading a canary verdict

The rows above that an operator can misread are spelled out:

* **`timeout` can mean a rejected credential.** The pinned CLI reconnects a refused
  connection with backoff until the attempt deadline, so the supervisor's verdict
  reads `timeout` while the cause is an authentication rejection. The deadline is
  the real bound and is not shortened to make a report prettier; instead the canary
  classifies the cause into `attempt.auth_rejection` (`detected`, `marker`,
  `detail`) beside the verdict, and the 401 itself lives in `attempt.stderr_tail`
  and in the supervisor's `output.stdout`.
* **The `HTTP 451` is one endpoint refusing, and it does not explain the canary.**
  `chatgpt.com/backend-api/ps/mcp` answers `451` with
  `{"message":"no_biscuit_no_service"}` from an OpenAI edge proxy
  (`x-openai-proxy-wasm: v0.1`), while the same host at the same moment answers
  `/backend-api/me` `401` and `/models`, `/conversation/init` `403` — with no
  proxy configured, with a browser user agent, over IPv6, and identically inside
  the attempt's own profile and outside it. The network is reachable and the
  boundary does not block that host; the refusal belongs to that one path, and
  **why** it refuses is unknown. It is *not* evidence that the endpoint is broken
  or that the pinned CLI is out of step: the authenticated CLI run outside the
  boundary never requests that path at all and completes the edit.

The real question is the failure the contained run actually reports, and it is
open:

* **The contained attempt ends before the model call, at `workspace routing
  discovery failed`**, retried five times against
  `chatgpt.com/backend-api/ps/mcp`, then `turn.failed` (measured 2026-11-02: exit
  1 after 19 s, `attempt.auth_rejection.detected` `false`, no 401 anywhere, patch
  empty). The authenticated CLI outside the boundary, with the operator's own
  login, completes the same task; and **with the same staged home and argv** —
  `HOME` seeded with `.codex/auth.json` alone, no `config.toml`, the same fixture
  and the same supervisor `containment=None` — it completes it too (exit 0,
  11.5 s, exactly the two intended files, no `ps/mcp` request). So the staged
  credential alone is not the cause: **the difference lies inside the
  boundary**, and which denial produces it is not identified. No allowance was
  added to find out, because the recorded allowances are a measured list and
  changing them is an operator decision, not a canary one; the next step is that
  controlled experiment, and its result belongs in the table above.

Cost, measured rather than estimated: **8 checks in 0.17–0.18 s** (two runs, this
host) per attempt; artifact digest **~1.0 s** for a single 246 MB binary, **~3.8 s
cold / 1.7 s warm** for a 14,922-file tree (a node-bundle runtime). Runtime startup
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
no ambient credential is reachable inside the boundary, no SSH agent, no `gh`,
workspace-only writes, no publication authority.

That sentence used to say "no readable credential", and it was true while the
canary took a platform key from the environment. It is no longer the whole truth,
so it is stated exactly: **no ambient credential is reachable inside the
boundary** — the denial of `$HOME`, `~/.codex`, `~/.ssh`, `~/Library/Keychains`
and `/Volumes` is unchanged, so the operator's own `auth.json` stays unreadable
there — and **one operator-designated session credential is staged into the run's
own home, recorded in evidence, and removed at the end**. The staged copy is the
only credential inside the boundary, it exists for the length of the attempt,
its digest is in the evidence, and the attempt directory that remains afterwards
is checked to be free of it.

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
| Both survivor checks unavailable (no `lsof`, `ps` failing) | treated as unverifiable ⇒ not confirmed, and a later attempt records `unresolved` |
| A survivor holds the workspace or attempt dir | `workspace-holder` recorded, termination unconfirmed, lock held |
| Containment policy and descriptor name different launchers | refused before a child exists |
| Launcher declared without a digest | refused: a path-only pin can be swapped in place |
| Launcher pinned by digest without `recorded_on` | refused at pin load: a digest that names no host cannot explain its next mismatch |
| State directory inside the workspace while contained | refused before the profile is rendered |

### An OS update moves the launcher pin

A macOS update replaces `/usr/bin/sandbox-exec`, and the very next canary run
stops before it stages anything. The pin records the digest **with the host it
belongs to** — `launcher.sha256` beside `launcher.recorded_on`, e.g.
`"macOS 27.2 (26B123)"`, both pin parsers refuse a digest that does not name its
host — so the refusal names both sides instead of two bare digests:

```text
tactus.runtime.containment.ContainmentUnavailable: containment launcher digest mismatch:
  pinned <digest> recorded on macOS 27.2 (26B123), observed <digest>
  (/usr/bin/sandbox-exec); this host is macOS 27.2 (26B123); re-record the launcher
  pin for this host only after confirming the Apple signature chain and the new OS build
```

That refusal is the pin working. Re-recording it is a deliberate operator act,
reviewed like any other: confirm the new binary's Apple signature chain
(`codesign -dvvv /usr/bin/sandbox-exec` → `Identifier=com.apple.sandbox-exec`,
`Authority=macOS Software Signing` → `Apple Code Signing Certification Authority`
→ `Apple Root CA`), record `sw_vers` before and after in the commit that moves
the digest, move `recorded_on` with it, and change nothing else in the pin.
**Do not read the file's mtime as evidence that nothing changed**: Apple's updater
stamps binaries with the OS build's timestamp, so a replaced `sandbox-exec` can
carry an mtime from before the update — measured here, 27.0 (26A428) → 27.2
(26B123) swapped the binary under an `Oct 23 01:10` mtime.

Never auto-accept a new digest and never let a tool "fix" it by itself: a pin
that rewrites itself on mismatch is a rubber stamp, not a pin. There is
deliberately no list of host/digest pairs to fall back on — such a list accepts
whatever a host presents, which is the same silence with more bookkeeping. One
digest, one named host, and a mismatch is a refusal, never a warning.

## Where this is verified

CI verifies the boundary in two places, split by where it can exist. The Linux
job (`python (pytest + verifier)`) runs everything that needs no seatbelt —
profile rendering from pin data, the exact write allowlist, the recorded
allowances, the policy hash, the launcher pin and every refusal path that needs
no child — and skips the rest with a reason naming what it could not exercise.
The `macos (containment boundary)` job on `macos-latest` runs exactly the tests
marked `seatbelt` and `macos_process_table`; `tests/conftest.py` states that rule
once, and on macOS nothing is skipped, so a launcher that has disappeared fails
the job instead of passing it. What it covers: the denial matrix against real
children through the pinned launcher, the canary's staging and fixture driven
through it, and both survivor checks. Outside CI's reach: the real-runtime
canary — one model call through the pinned runtime inside the boundary — needs
operator approval and a scoped credential and stays an operator command, and the
host-specific posture (a workspace or `TMPDIR` inside a denied root) is exercised
only on a developer's machine, where `./scripts/verify-goal.sh` runs the whole
suite, both jobs' tests included.

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
  to start a thread under `(deny default)`, and three named mach services
  (`com.apple.cfprefsd.daemon`, `com.apple.cfprefsd.agent`,
  `com.apple.SecurityServer`) before it can load its configuration or evaluate
  TLS trust at all — see "The three mach services the pinned runtime needs". They
  are a same-user shared-memory channel, two preferences daemons and securityd:
  enough to use the Security framework and its own preferences domain, not enough
  to read the operator's keychain stores (measured above). Pinned runtimes record
  their allowances in the pin file, so an allowance can be reviewed and cannot
  grow silently. Pi 1.3.0 needs none.
* **Survivors are found by two checks, and neither is complete.** The marker
  sweep reads `ps -E`, which on macOS does not expose the environment of every
  binary, so a leftover `/bin/sleep` or `/bin/sh` helper is invisible to it — and
  a helper that clears its own environment is invisible by construction. The
  second check asks `lsof` which processes still hold the workspace or the attempt
  directory open or as their working directory, which does not depend on the
  environment: a detached helper normally inherits the workspace as its cwd and is
  caught there (`test_runtime_supervision.py::test_a_survivor_is_found_even_when_its_environment_is_invisible`).
  A survivor that neither holds those paths nor exposes its environment is not
  detectable, and the code says so rather than pretending otherwise. Note what
  that permits: such a process holds no handle *between* writes, so it can still
  write into the workspace by absolute path after the lock has been handed over.
  It is why an unavailable check (`unverifiable`) keeps the lock instead of being
  read as clear, and why the workspace is disposable evidence rather than a
  trusted store.
* **`workspace-diff` needs git.** The mode stages the workspace and diffs against
  the revision recorded before the child spawned, so a run that stages or commits
  its own work is still accounted for; a workspace that is not a git checkout is
  refused in that mode rather than reported as an empty diff.
* **No protection against the operator's own account.** The child runs as the
  same user; the boundary denies paths, IPC and network, it does not create a
  security principal. A distinct account or a VM remains the stronger option.
* **Not publication.** Nothing here pushes, opens a PR or holds a publishing
  token; publication is a separate authorization (Issue #23).
