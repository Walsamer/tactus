# Runtime supervision (TCT-WO-022a)

Status: bounded supervision port implemented and hardened; **no real coding agent
attached yet** (Issue #22 continues with a pinned real runtime and an execution-
permission boundary that does not exist yet).

Tactus owns the *process* of one agent attempt. This document states exactly what
is enforced, what is merely observed, and what is deliberately **not** claimed.

## Lifecycle (ordering is the contract)

```text
run(request)
  1  preflight(pinned descriptor)        version + executable sha256; probe and launch must be the same binary
  2  acquire writer lock                 flock(LOCK_EX|LOCK_NB) — exclusive ownership FIRST
  3  settle previous attempt             verified termination, or refuse (fail closed)
  4  create attempts/<slug>-<nonce>/     unique evidence dir; nothing is ever deleted or reused
  5  write request.json                  inside the attempt dir
  6  write attempt.json phase=spawning   before the child exists
  7  spawn (start_new_session=True)      new process group
  8  write phase=running + pgid + start marker
  9  deadline / cancellation loop        SIGTERM, then SIGKILL, whole group
 10  termination confirmation            group no longer signalable
 11  artifact validation                 bounded reads; scope/patch/filesystem checks
 12  write terminal record               phase=settled | unterminated
 13  mark settled, release lock          only if 10 and 12 both hold
```

Two invariants fall out of this order, and both are tested:

* **Nothing is touched before ownership.** A concurrent attempt fails at step 2
  having created, modified and deleted nothing.
* **The lock outlives the process.** Release requires *termination confirmed* and
  *terminal record written*. A failed artifact validation still writes the record
  first, so the workspace is never falsely marked available.

## Recovery contract (fail-closed)

A durable `attempt.json` survives the supervising process. On the next attempt
(or an explicit `recover()`):

| Recorded phase | Process identity | Action |
|---|---|---|
| *(none)* / `settled` / `recovered` / `resolved` | — | proceed |
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

**Why the marker exists.** A recorded pgid may have been recycled by an unrelated
process after the original group emptied. Signalling it because "the number is
still there" would kill someone else's work, so the supervisor stores the OS
start-time marker at spawn and only signals when it still matches.

## Enforced (and tested)

| Property | Mechanism |
|---|---|
| Pinned runtime, no fallback | exact argv template + probe; probe and launch must resolve to the **same** executable |
| Exclusive ownership before mutation | lock acquired before any state write; per-attempt directories, never reused |
| Explicit write scope | declared paths **and** patch headers validated; absolute paths, `..`, undeclared headers rejected |
| Patch / result bounds | `stat` size checked before reading; reads capped (`max_patch_bytes`, `max_result_bytes`) |
| Deadline / cancellation | whole-exchange deadline; `cancel(reason)` |
| Process-tree termination | `start_new_session`; `killpg` SIGTERM → SIGKILL; confirmation by observation |
| No lock release while unknown | release requires confirmed termination **and** a written terminal record |
| Restart safety | marker-verified recovery, explicit `unresolved` state, operator resolution |
| Machine-checkable evidence | `status`, pid/pgid, signals, timings, declared vs patch paths, filesystem check, validation errors |

Statuses: `success | failed | timeout | cancelled | invalid | unterminated`.

## Deliberately not claimed

* **Not a sandbox.** The child runs as the same OS account with the same
  filesystem and network reach. Write scope is *detected and rejected after the
  fact*, not prevented.
* **Not verified filesystem enforcement.** The supervisor compares declared paths
  with patch headers, and with a workspace diff only when a provider is supplied
  (`filesystem.verified=false` otherwise). It never inspects what the child
  actually read or wrote outside the workspace.
* **No credential containment.** The child environment omits `HOME` and tokens,
  but a same-user child can still read files and use ambient credentials.
* **Not publication.** Nothing here pushes, opens a PR, or holds a token.
  Publication is a separate authorization (Issue #23) and cannot be inferred from
  a successful run.

## Prerequisite for TCT-WO-022b (real agent)

A real coding agent **must not** be started by this port until an execution-
permission boundary exists. Specifically:

1. **Filesystem containment** — the agent must not be able to read the operator's
   home, other repositories, or the state directory; a same-user Git worktree
   (including a Stax worktree) provides *none* of this.
2. **Network containment** — no outbound access by default; a worktree does not
   restrict sockets.
3. **Credential containment** — the ambient environment (Keychain, `~/.config`,
   `gh` credentials, SSH agent sockets) is reachable by any same-user child;
   omitting environment variables is hygiene, not containment.
4. **Termination authority** — the boundary must make SIGKILL of the whole tree
   meaningful (no helpers escaping into another session, no privileged daemons).

Until (1)–(4) are decided and implemented (an OS-level decision: sandbox profile,
container, or a distinct account), `022b` stays blocked and `023a` may only claim
**Git worktree isolation**, not process or network isolation.
