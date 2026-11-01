# Coding-agent adapter contract (TCT-WO-022b)

Status: **fake-runtime contract implemented and tested; no real coding agent is
launched.** The adapter wraps the hardened runtime supervisor from TCT-WO-022a.
A real Pi/Codex/provider runtime remains gated by an execution-permission
boundary and an explicit provider/model decision.

The adapter now has a **caller**: the Tactus-owned Dagster code location
(`integrations/dagster/agent_location.py`) executes exactly one attempt per
`software.implement` run under the operator-owned pin
`config/agent-runtime.json`. See
[`AGENT_EXECUTION_SLICE.md`](AGENT_EXECUTION_SLICE.md) for what that proves and
for the one link still pending: B4, the Ictus job skeleton that Ictus has agreed
and scheduled.

## Boundary

```text
Ictus / Dagster capability
        │  explicit RuntimeDescriptor (no fallback, no discovery)
        ▼
RuntimeAgentAdapter                versioned contract + result shape
        │
        ▼
RuntimeSupervisor                  lock, deadline, cancellation, termination,
        │                          artifact bounds, restart recovery
        ▼
fake child process                 edits a disposable workspace, emits artifacts
```

The adapter does **not** choose a runtime, rank backends, consult Ictus policy
or publish. Runtime selection is the caller's explicit `RuntimeDescriptor`.
Policy decisions remain Ictus-owned.

## Contract (version `1.0.0`, id `tactus.coding-agent.adapter/v1`)

| Element | Declared value |
|---|---|
| Request | `request.json` with `attempt_id`, `intent_id`, `capability` (`tactus.runtime.request/v1`) |
| Expected artifacts | `result.json` (schema `1`) and, on success, `patch.diff` |
| Result statuses | `success`, `failed`, `timeout`, `cancelled`, `invalid`, `unterminated` |
| Cancellation | `cancel(reason)` in the supervisor; SIGTERM then SIGKILL to the whole process group, confirmed by observation |
| Restart | verified process identity only; unresolved attempts block new writers until an explicit operator decision |
| Credentials forwarded | none (`credential_environment` is empty) |
| Publication authority | none (`publication_authority=false`) |

The adapter re-checks successful evidence: the declared changed paths are
non-empty and within `max_changed_paths`, the patch is non-empty and within
`max_patch_bytes`, and the reported `patch_sha256`/`patch_bytes` match the exact
patch artifact re-read under a hard byte cap. Patch headers may not name a path
that was not declared. On success the result exposes:

* `changed_paths` — the validated repository-relative paths;
* `patch_sha256` / `patch_bytes` — digest and size of the bounded patch;
* `attempt_dir`/`patch_path` and the complete supervisor evidence.

## Fake-agent canary (what is proven)

`tests/integrations/agents/test_agent_adapter.py` runs a real child process that
edits `src/example.py` in a disposable workspace and writes the matching unified
diff. The suite proves:

| Scenario | Observed adapter/supervisor outcome |
|---|---|
| real workspace edit | `success`; file changed; diff applies cleanly; digest and size match |
| crash | `failed`; non-zero exit; termination confirmed; writer lock released |
| timeout | `timeout`; process group terminated; lock released |
| cancellation | `cancelled`; reason recorded; termination confirmed |
| restart hold | new writer refused while the prior attempt is `spawning`; explicit operator resolution unblocks a later run |
| scope violation | `invalid`; escaped path rejected; lock released |
| credential hygiene | the supervisor constructs `PATH` plus the two Tactus attempt/intent variables; the child receives no GitHub/publication token, `HOME` or SSH agent socket |

This is a **fake runtime**: no model provider, Pi, Codex, GitHub operation, Stax
process or credential is involved. The child still runs as the same OS account.

## Remaining gates (explicit, not implied)

1. **Real-agent provider and model** are unselected. Launching a real coding
   agent requires an approved Work Order naming the provider/runtime/model and
   its pinned descriptor.
2. **OS execution-permission boundary is unresolved.** Filesystem, network and
   credential containment (sandbox profile, container, or distinct account),
   including termination authority over helpers, must be decided and implemented
   before any real agent canary.
3. A Git worktree (including a Stax worktree) is **not** an OS sandbox and does
   not establish (1) or (2).

Until both gates are closed, this adapter is exercised only against the
deterministic fake agent and must not be pointed at a real provider.
