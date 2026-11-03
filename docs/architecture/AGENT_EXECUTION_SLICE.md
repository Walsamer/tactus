# Agent execution slice: `software.implement` through Dagster

Status: **the Tactus half is implemented, tested and runnable, and the boundary an
attempt can run behind is now enforced and proven without a model call; the
durable link reaches Ictus's runtime seam, which landed at Ictus `67877fb`.** The
pinned runtime is still the deterministic **fake** agent: no model provider, no
credentials, no publication — and, in this slice, still no containment, because
its pin pins no launcher. Issue #22 is **not** closed by this slice.

Phase 2 (Issue #22) added the enforcement half a real runtime needs: a rendered
seatbelt profile, a pinned launcher verified by path and digest, a denial matrix
run with real children before any agent process exists, the task on stdin instead
of argv, attempt-local `HOME`/`TMPDIR`/git config, an environment allowlist of
variable *names*, and a survivor sweep that gates `confirmed` and lock release
(see [`CONTAINMENT.md`](CONTAINMENT.md) and
[`RUNTIME_SUPERVISION.md`](RUNTIME_SUPERVISION.md)). That path is exercised
without a model call: `tests/runtime/test_containment.py`,
`tests/runtime/test_runtime_supervision.py` and
`tests/integrations/agents/test_agent_adapter.py` run real child processes and no
provider.

The canary itself has now been run by the operator, and its containment posture
is measured rather than argued. Under the shipped pin the pinned CLI starts
inside the boundary, which needs four named mach services and the operator's
session credential staged into the run's own home; the
denial matrix holds 8/8 with those allowances, and no ambient credential is
reachable (the denial of `$HOME`, `~/.codex`, `~/.ssh` and `~/Library/Keychains`
is unchanged). The run now **completes its edit inside the boundary**, and the
failure that preceded it is named rather than open: it ended before the model
call at `workspace routing discovery failed`, and the kernel log gave the denial
per failing run — `mach-lookup com.apple.trustd.agent`, TLS trust evaluation —
so every HTTPS request failed at the transport until that one allowance was
added. The staged credential, the network and the OS update are all exonerated
(the same staged home and argv complete the edit outside the boundary); `configd`
was tried first, changed nothing and was removed again. See
[`CONTAINMENT.md`](CONTAINMENT.md) "Reading a canary verdict" for the
measurements.

Two things the operator must not misread in the evidence: a verdict of `timeout`
can mean a *rejected credential*, because the CLI reconnects until the deadline —
the 401 is in `attempt.stderr_tail` and in the child's stdout, and
`attempt.auth_rejection` names it beside the verdict; and the `HTTP 451` from
that one subscription-backend path is neither a network nor a containment
failure and does not explain the earlier routing-discovery failure. Likewise the
`401 invalid_api_key` seen on `wss://api.openai.com` with a placeholder key was
the WebSocket transport rejecting a placeholder key — it never exercised the
HTTPS trust check, so it is not evidence that the platform-key route or any
routing path worked. `credential_env` stays documented as the alternative route
in the pin; the shipped route is the operator's session file.

## What this slice is

The first time an agent attempt is executed as part of the real composition
instead of a library sitting behind tests:

```text
checked WorkOrder                 (tactus.domain / persistence)
  -> INITIAL StateSnapshot        tactus.integrations.ictus.state_snapshot
  -> real pinned `ictus decide`   validated ExecutionIntent for software.implement
  -> Dagster run                  Tactus-owned code location, job software_agent_job
  -> one supervised attempt       tactus.runtime.RuntimeSupervisor + RuntimeAgentAdapter
  -> bounded patch evidence       result.json + patch.diff, digest-verified
  -> terminal result -> Tactus    correlate + apply once (durable mode only)
```

## What the pinned Ictus revision already provides

At the pinned revision (`c2aa092`) the capability route exists:

| Element | Value |
|---|---|
| capability | `software.implement` (RiskClass Medium, side effects, no approval gate) |
| job | `software_agent_job` |
| resource key | `software_agent_settings` |
| settings mapper output | `{"capability": "software.implement", "placeholder": true}` |
| capability arguments | discarded by the mapper (an authorized intent is not a config channel) |
| code-location addressing | `ICTUS_DAGSTER_CODE_LOCATION` names the location; the bridge resolves the job there by name |

The job itself is a **fail-closed placeholder**: one step that always raises with
`allow_retries=False`. That is deliberate — "a real implementation must be
registered by a capable backend" — and this slice is that backend.

## The seam: Ictus's port, registered here (B4, landed)

Dagster records the execution plan from **Ictus's** job definition when the run
is created, then replays it in whichever code location the environment names: the
recorded **step selection** and snapshots are replayed, while the plan itself is
rebuilt from the location's own job. Identical step names are therefore
*necessary* — and, as the experiment below shows, sufficient in practice — but
they are not a guarantee that Ictus's recorded graph is what executes. The
pinned job used to carry one placeholder step, and a run recorded against it
could not execute in a location that declared a different graph:

| | pinned Ictus `software_agent_job` (before B4) | the Tactus code location (now) |
|---|---|---|
| steps | `software_agent_placeholder_op` | Ictus's `software_agent_{prepare,execute,verify,finalize}_op` — registered, not defined |
| resource key | `software_agent_settings` | `software_agent_settings`, re-bound so `runtime_seam` is a resource default |

**B4 landed against Ictus `67877fb`; the pin has since advanced to `c2aa092`
for the identity-digest vectors, which leaves this route unchanged.**
`software_agent_job` and `software_publish_job` now carry a
`prepare → execute → verify → finalize` skeleton and reach a runtime only through
a port named in an operator-owned configuration file
(`contracts/runtime-seam.schema.json`).

`agent_location.py` therefore defines no graph at all any more: it registers
Ictus's two jobs unchanged and supplies the seam path as a **class-level
default** on the settings resource. The class is the point —
`ictus_dagster/submission.py::_run_config` always writes that resource's
run-config block, and a run-config document *replaces* a
`ConfigurableResource`'s instance values and falls back to the class defaults. An
instance-only default is wiped by the bridge's own run config; a class default is
what survives it. Unset, `runtime_seam` is `None` and the route keeps
Ictus's exact fail-closed placeholder failure, `INTEGRATION_CONFLICT` and
non-retryable.

That did require Tactus code, and the earlier claim that it would not is deleted
rather than softened:

| Requirement | What Tactus supplies |
|---|---|
| step keys | none of its own: it registers Ictus's two jobs unchanged |
| backend | `agent_backend:create_backend`, implementing `describe_workspace` / `execute` / `verify` |
| evidence | `runtime-evidence.schema.json`, translated from the supervisor's own account |
| bounds | none in the runtime pin: `write_scope`, the wall clock and the step budget arrive on the attempt request, and the pin carries only identity plus `runtime_caps`, which the runtime enforces itself and which can only be **tighter** than the seam's bounds, never a second opinion about them |

What this repository cannot establish, and does not claim: it has no Dagster
dependency (`pyproject.toml` declares none), no Ictus worker, no queued
coordinator and no Dagster instance, so it can show that the backend satisfies
the published contract and that the registered jobs have the pinned step keys —
and nothing at all about a run executing. `scripts/validate_agent_slice.py`
remains the tool that measures the durable path where a deployment exists, and
`tests/integrations/ictus/test_runtime_seam_conformance.py` measures the contract
against the pinned artifact.

Evidence that the rest of the chain already works, from two throwaway experiments
(neither committed):

1. A variant of the *location* whose single step impersonated the placeholder's
   name made the durable chain pass — and was rejected, because it would also
   relabel every failure `INTEGRATION_CONFLICT` through the bridge's step table.
2. **The decisive one:** a throwaway clone of the pinned Ictus revision whose
   `software_agent_job` carried the skeleton above switched the harness to
   `durable-submission` and passed end to end with **no Tactus change** — real
   Ictus ALLOW + intent, one Dagster run launched by the queued coordinator,
   one supervised attempt, the recorded patch checked to apply to a clean copy of
   the original workspace, the terminal result applied exactly once (a replay
   applied nothing), WorkOrder IMPLEMENTED.

That clone was a *patched* checkout, so its evidence record carries
`ictus_checkout.dirty: true` and lists the modified file. The harness refuses a
checkout with local changes at the pinned commit unless `--allow-dirty-ictus` is
passed, so a patched checkout's evidence can never be mistaken for the pinned
revision's.

Recipe to re-derive the durable evidence (throwaway, never committed): clone the
Ictus repository at the pin, replace the body of its `software_agent_job` with
the skeleton above (its ops may stay fail-closed — Dagster resolves the steps in
the *Tactus* location), then run the harness with `--allow-dirty-ictus`.

The harness binds `ictus_dagster` to the checkout the pin names (and refuses to
run when the import resolves elsewhere), so the seam is always judged against
the pinned revision rather than against whatever is installed in the venv.

## What Tactus implements

| Path | Purpose |
|---|---|
| `integrations/dagster/agent_attempt.py` | Dagster-free: load the operator runtime pin, verify the executable digest, supervise one attempt, re-check patch evidence, write the durable attempt account next to the artifacts. |
| `integrations/dagster/agent_location.py` | Dagster: registers Ictus's `software_agent_job` and `software_publish_job` unchanged and re-binds their settings resources so `runtime_seam` (from `TACTUS_RUNTIME_SEAM`) is a resource default — `None` when unset, which keeps Ictus's fail-closed placeholder. Defines no steps of its own. |
| `integrations/dagster/agent_backend.py` | Dagster-free: the seam backend Ictus resolves by name (`agent_backend:create_backend`). Runs one supervised attempt through the pin and reports it as `runtime-evidence.schema.json`. It does not use `control.spawn`; the supervisor owns the child's process group and confirms its termination. |
| `config/agent-runtime.json` | Operator-owned runtime pin (the analogue of `config/stax-pin.json`): runtime id/kind, adapter, executable path, exact SHA-256, argv and probe templates, expected version token, `runtime_caps`, and — since phase 2 — optional `artifacts`, `containment`, `env_allowlist` and `stdin_from`. It declares **no** write scope, deadline or step budget: those are the attempt's bounds and arrive on the attempt request. The shipped fake pin declares no launcher, so the attempt it describes runs uncontained; a pin that declares a launcher gets the boundary, verified at preflight. |
| `config/agent-runtime-canary.json` | The operator canary's pin: the Codex CLI release tree and executable by digest, the launcher by digest (with the host it was recorded on), denied roots, `egress: open` for that run only, the runtime's recorded allowances (`ipc-posix-shm` plus the four named mach services it cannot run without — `com.apple.cfprefsd.daemon`, `com.apple.cfprefsd.agent`, `com.apple.SecurityServer`, `com.apple.trustd.agent`), and its credential: the operator's session file (`credential_file`, `~/.codex/auth.json`) staged into the run's own home at `0600` and removed when the attempt ends. Exactly one credential route may be configured; `credential_env` (`CODEX_API_KEY`, a scoped platform key) is the documented alternative. Run through `scripts/run-agent-canary.sh`. |
| `scripts/run-agent-slice.sh`, `scripts/validate_agent_slice.py` | The evidence harness. |
| `tests/integrations/dagster/test_agent_attempt.py` | Dagster-free tests: pin drift, unknown/missing pin keys, unpinned runtime refused before anything is touched, success with real workspace edit, crash, timeout, scope violation, route-capability mismatch, JSON-safe accounts. |

The pin names **no vendor**. Runtime id, executable, argv/probe templates and the
expected version token are data, so the same contract carries Pi, Codex, a local
model or anything else that is a pinnable artifact with a probe and a protocol —
selecting one is a reviewed change to that file, not a code change. The
deterministic fake agent is the only runtime pinned today.

Failures are typed, not inferred from a step name. Ictus's seam raises a
`Failure` with `allow_retries=False` and an `ictus.failure_category` on every
path, and the bridge reports that typed category in preference to guessing one
from the step key — so `SCOPE_VIOLATION`, `WALL_CLOCK_EXCEEDED`,
`TERMINATION_UNCONFIRMED` and the rest reach a control plane as typed metadata,
and the **category** is what crosses into an `ExecutionResult`. This location
never reclassifies a failure and never invents a category. One kind is currently
reported more coarsely than it could be: a write-scope escape the supervisor
detects arrives as `RUNTIME_VERIFICATION_FAILED` rather than `SCOPE_VIOLATION`.
Both map to `VERIFICATION_FAILURE`, and the category does not change, but the
kind a recovery decision would key on is lost.

The runtime backend and the working workspace come from operator configuration —
the seam file named by `TACTUS_RUNTIME_SEAM` and `TACTUS_AGENT_WORKSPACE_ROOT` —
and the attempt's bounds (write scope, wall clock, step budget) from the request
Ictus constructs, **never** from the intent: the pinned mapper sends only
`{"capability", "placeholder"}`, `placeholder` is unused, and the backend refuses
any capability but `software.implement` — a promotion routed at this backend
fails closed rather than reporting a publication that never happened.

## Modes of the harness

The harness probes the seam *before* submitting anything and records what it
proved in the evidence file:

| Mode | When | What it proves |
|---|---|---|
| `durable-submission` | pinned job steps equal the Tactus location's steps | The whole chain, including durable submission through the pinned bridge and Tactus applying the terminal result exactly once. |
| `in-process` | the deployment configures no seam, so the route keeps Ictus's placeholder failure | A real Ictus ALLOW + intent for `software.implement`, a real Dagster execution of Ictus's registered job, one supervised attempt under the pin, and a recorded patch that applies to a clean copy of the original workspace. `blocked_dependencies` reports the seam probe's own verdict. |

Tactus's own result-application path is already proven by
`scripts/run-execution-slice.sh` for the generic capability, so the in-process
mode does not weaken that evidence — it simply cannot run it without the
bridge's `ExecutionResult`.

## Running it

```sh
TACTUS_ICTUS_ROOT=/path/to/ictus-checkout-at-the-pin scripts/run-agent-slice.sh \
    --evidence /tmp/tactus-agent-slice-evidence.json
```

The checkout must be **at** `PINNED_ICTUS_COMMIT` and carry a virtualenv with
`dagster`/`ictus_dagster` plus a prebuilt `target/debug/ictus`. The canonical
`./agentic-control` checkout legitimately sits ahead of the pin; use a checkout
at the pinned commit (that is what `tests/integrations/ictus/conftest.py` builds
for the conformance tests).

## Not claimed (Issue #22 stays open)

1. **No real coding agent.** `runtime_kind: fake-canary`; the provider/model pin
   and an approved bounded canary are open (Issue #22 AC1).
2. **This slice's shipped pin is uncontained.** The supervisor enforces a
   boundary whenever the descriptor pins a launcher
   ([`CONTAINMENT.md`](CONTAINMENT.md)) and the pin loader accepts a
   `containment` block; `config/agent-runtime.json` declares none, so the fake
   canary's child keeps the account's filesystem and network reach and its account
   records `isolation: "none (same OS account: no filesystem or network
   boundary)"`. Anything else — a workspace inside a denied root, a launcher
   digest drift, a failing denial matrix — is refused before a child exists; what
   the boundary *does* refuse is proven in
   [`RUNTIME_SUPERVISION.md`](RUNTIME_SUPERVISION.md). The workspace may still not
   be the Tactus checkout itself.
3. **No publication.** `publication_authority: false` in every account; nothing
   pushes, opens a PR or holds a token (Issue #23).
4. **No automatic repair.** A failed attempt is a terminal step failure; Tactus
   and Ictus decide what a failure means. Nothing re-runs an agent to "retry" a
   publication or an execution.
