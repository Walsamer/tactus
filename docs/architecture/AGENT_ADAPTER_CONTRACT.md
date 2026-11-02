# Coding-agent adapter contract (TCT-WO-022b)

Status: **contract version `1.1.0` implemented and tested; the OS boundary behind
it is enforced and proven with real child processes** (see
[`CONTAINMENT.md`](CONTAINMENT.md)). No real coding agent is launched on this
route: the only pinned runtime here is the deterministic fake agent. The one
real-runtime canary is an operator command and has been run — the pinned CLI
reaches the model call inside the boundary, with one operator-designated session
credential staged into the run's own home and removed when the attempt ends.

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
        ├─ containment (when the descriptor pins a launcher)
        │                          profile, denial matrix, marker sweep
        ▼
pinned runtime                     edits a disposable workspace, emits artifacts
```

The adapter does **not** choose a runtime, rank backends, consult Ictus policy
or publish. Runtime selection is the caller's explicit `RuntimeDescriptor`.
Policy decisions remain Ictus-owned.

## The runtime descriptor is vendor-free

A runtime is described by data, never discovered by convention and never named in
code (`src/tactus/runtime/descriptor.py`). The descriptor carries:

* an **artifact set** — one file, or a tree with a declared digest order
  (path-sorted `sha256  relpath` manifest) and a declared file count, plus an
  entrypoint for a tree that is executed directly;
* an optional **interpreter** artifact, for a runtime that is launched by one;
* an optional **launcher** — the OS containment wrapper, pinned by path and
  digest, whose `argv_prefix[0]` must be itself;
* the exact **argv template** with the `{request}`, `{prompt}`, `{workspace}` and
  (behind a launcher) `{profile}` placeholders, and the exact **version probe**,
  which is executed *through* the pinned launcher inside the containment
  self-test rather than outside it;
* the environment variable **names** the attempt may receive;
* the **result contract** — `agent-artifacts` (the runtime writes `result.json`
  and `patch.diff`) or `workspace-diff` (it writes neither: the supervisor stages
  the workspace, diffs it against the revision recorded before the child spawned,
  and takes both the changed paths and the patch from that one source, so a
  general-purpose CLI is accounted for by what it actually changed). Both are
  pin data (`result_contract` in the pin file) and both are refused rather than
  guessed when the workspace cannot answer;
* next to it, a **containment policy** — launcher, denied roots, egress mode,
  recorded allowances — that names no runtime either.

Nothing in that module names a vendor. Codex, Pi, a local model and the
deterministic fake agent differ only in data: which artifacts are pinned, how the
task is fed, which allowances the runtime demonstrably needs, and whether egress
is denied, localhost-only or (for an approved canary) open. Selecting one is a
reviewed change to the pin, not a code change. There is no fallback list: a
missing, non-executable, unprobeable or version-mismatched runtime fails closed.

The data shape is tested on its own:
`tests/runtime/test_runtime_contract.py::test_file_artifact_pin_is_verified_and_recorded`,
`…::test_tree_artifact_pin_is_verified_with_its_manifest`,
`…::test_launcher_command_prefixes_the_pinned_runtime`,
`…::test_a_profile_placeholder_requires_a_launcher`,
`…::test_prompt_placeholder_and_stdin_source`,
`…::test_env_allowlist_declares_names_only`.

The result contract is tested against a runtime that edits the workspace and
writes no artifacts at all:
`tests/runtime/test_runtime_supervision.py::test_workspace_diff_contract_needs_no_agent_artifacts`,
`…::test_workspace_diff_contract_rejects_a_path_outside_the_scope`,
`…::test_workspace_diff_contract_requires_a_provider`.

## Contract (version `1.1.0`, id `tactus.coding-agent.adapter/v1`)

| Element | Declared value |
|---|---|
| Request | `request.json` with `attempt_id`, `intent_id`, `capability`, `task` (`tactus.runtime.request/v2`) |
| Task delivery | the task goes to `prompt.txt` in the attempt directory and to the child's stdin, never argv; a runtime that reads one on stdin refuses an empty task |
| Expected artifacts | `result.json` (schema `1`) and, on success, `patch.diff` |
| Result statuses | `success`, `failed`, `timeout`, `cancelled`, `invalid`, `unterminated` |
| Result digests | `patch_sha256`/`patch_bytes` for the patch, `task_sha256` for the task |
| Cancellation | `cancel(reason)` in the supervisor; SIGTERM then SIGKILL to the whole process group, confirmed by observation |
| Restart | verified process identity only; an attempt marker still in the process table makes the prior attempt `unresolved`, never `recovered`, and blocks new writers until an explicit operator decision |
| Credentials forwarded | none; `credential_environment` declares variable *names* only — a value is refused, and so is any `GH_*`, `GITHUB_*` or `SSH_*` name |
| Publication authority | none (`publication_authority=false`) |

The contract id is unchanged — it names the protocol family — while the version
moved to `1.1.0` because the request schema, the task digest and the credential
rule changed (`tests/integrations/agents/test_agent_adapter.py::test_contract_declares_version_request_cancellation_artifacts_and_statuses`,
`…::test_contract_names_credentials_without_carrying_them`).

The adapter re-checks successful evidence: the declared changed paths are
non-empty and within `max_changed_paths`, the patch is non-empty and within
`max_patch_bytes`, and the reported `patch_sha256`/`patch_bytes` match the exact
patch artifact re-read under a hard byte cap. Patch headers may not name a path
that was not declared. It also refuses an empty task for a runtime that reads one
on stdin, before an attempt directory exists
(`tests/integrations/agents/test_agent_adapter.py::test_a_runtime_that_reads_a_task_refuses_an_empty_one`).
On success the result exposes:

* `changed_paths` — the validated repository-relative paths;
* `patch_sha256` / `patch_bytes` — digest and size of the bounded patch;
* `task_sha256` — digest of the task the child was asked to perform; the task
  text itself stays in the attempt's `request.json`
  (`tests/integrations/agents/test_agent_adapter.py::test_the_result_carries_only_the_task_digest`);
* `attempt_dir`/`patch_path` and the complete supervisor evidence, containment
  block included when a launcher is pinned.

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
| task on stdin | the task reaches the child through stdin and its digest is in the result; an empty task is refused |
| credential hygiene | the supervisor constructs `PATH`, the two Tactus attempt/intent variables and the per-run attempt marker, with attempt-local `HOME`/`TMPDIR`/git config; the child receives no GitHub/publication token and no SSH agent socket, and only allowlisted variable *names*. A runtime that authenticates from a session *file* gets that one file staged into its own home at `0600` and unlinked when the attempt ends |

This is a **fake runtime**: no model provider, Pi, Codex, GitHub operation, Stax
process or credential is involved. The child still runs as the same OS account —
unless a launcher is pinned, in which case it runs inside the seatbelt boundary
whose denials are proven separately with real children
(`tests/runtime/test_containment.py::test_self_test_proves_every_denial`,
`tests/runtime/test_runtime_supervision.py::test_containment_is_proven_before_the_agent_runs`).
Neither suite calls a model.

## Remaining gates (explicit, not implied)

1. **The real-runtime canary's containment is measured; its edit is not.** The
   operator's run reaches the model call inside the boundary and ends there: this
   network answers the subscription backend with `HTTP 451` (`chatgpt.com`, in and
   outside the boundary alike), and the platform-key route returns
   `401 Unauthorized` for a placeholder key. Until a run completes one edit, no
   patch, push refusal and token account have been observed for a real runtime.
2. **The egress posture is the canary's, not the default.** Standing posture is
   `deny network*`, or one `localhost:<port>` for a locally served model; open
   egress exists for the length of the approved canary only, and the rendered
   policy (mode included) is copied into the attempt's containment evidence
   (`src/tactus/runtime/supervision.py:811`). A hosted provider therefore means
   open egress for one run, or a local allow-listing proxy.
3. **No real runtime is pinned in this repository.** `config/agent-runtime.json`
   pins the deterministic fake agent, and the Dagster pin loader here still reads
   the pre-phase-2 key set (`integrations/dagster/agent_attempt.py:79`), so it
   pins no launcher and its attempts record `isolation: none`. Launching a real
   coding agent requires an approved Work Order naming the provider/runtime/model
   and its pinned descriptor.
4. A Git worktree (including a Stax worktree) is **not** an OS sandbox and does
   not establish 1–3.

Until those gates are closed, this adapter is exercised only against the
deterministic fake agent and must not be pointed at a real provider.
