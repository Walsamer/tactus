# Agent execution slice: `software.implement` through Dagster

Status: **the Tactus half is implemented, tested and runnable, and the boundary an
attempt can run behind is now enforced and proven without a model call; the
durable link waits on one Ictus change that is agreed and scheduled (B4).** The
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
is measured rather than argued. Under the shipped pin the pinned CLI starts and
reaches the model call inside the boundary, which needs three named mach services
and the operator's session credential staged into the run's own home; the
denial matrix holds 8/8 with those allowances, and no ambient credential is
reachable (the denial of `$HOME`, `~/.codex`, `~/.ssh` and `~/Library/Keychains`
is unchanged). What the run does *not* yet show is a completed edit: this
network answers the subscription backend with `HTTP 451` (`chatgpt.com`, inside
and outside the boundary alike), so the attempt authenticates and then fails at
that endpoint. The platform-key route is what reaches the model endpoint
(measured: `401 Unauthorized`, `invalid_api_key`, with a dummy key), and it stays
documented as the alternative in the pin. See
[`CONTAINMENT.md`](CONTAINMENT.md) "Measured on this host" for each observation.

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

At the pinned revision (`c140af3`, Ictus PR #21) the capability route exists:

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

## The seam, and why it is a contract (blocked dependency B4)

Dagster records the execution plan from **Ictus's** job definition when the run
is created, then replays it in whichever code location the environment names: the
recorded **step selection** and snapshots are replayed, while the plan itself is
rebuilt from the location's own job. Identical step names are therefore
*necessary* — and, as the experiment below shows, sufficient in practice — but
they are not a guarantee that Ictus's recorded graph is what executes. The
pinned placeholder carries one step:

| | pinned Ictus `software_agent_job` | Tactus `software_agent_job` |
|---|---|---|
| steps | `software_agent_placeholder_op` | `prepare_op` → `execute_op` → `verify_op` |
| resource key | `software_agent_settings` | `software_agent_settings` |

A run submitted against the pinned revision fails with
`DagsterExecutionStepNotFoundError: Can not build subset plan from unknown step:
software_agent_placeholder_op`. The two honest ways out are *renaming the Tactus
steps to impersonate the placeholder* (which would also relabel every failure as
`INTEGRATION_CONFLICT` through the bridge's `FAILED_STEP_CATEGORY` table — a
mislabelled fact fed to Ictus) or **giving the Ictus job a real skeleton**.

**B4 (Ictus-owned, agreed and scheduled, not implementable from a Tactus Work
Order):** replace the placeholder step of `software_agent_job` with a contract
skeleton of the same kind `capability_execution_job` already uses for foreign
backends, keeping the placeholder behaviour as the fail-closed default until a
backend implements it. Ictus has taken this as its next slice, together with the
identical change for `software_publish_job` (Issue #23), and will express the
runtime seam as a port named in configuration — **no Ictus dependency on Tactus
code**. The seam requirements sent to Ictus are exactly the contract this
location already satisfies:

| Requirement | Value Tactus implements |
|---|---|
| step keys | `prepare_op` → `execute_op` → `verify_op` (one dict in/out each) |
| resource key | `software_agent_settings` |
| settings shape | what the pinned mapper emits: `{"capability", "placeholder"}` |
| failure vocabulary | the bridge's `FAILED_STEP_CATEGORY` names, so a failure is normalized by Ictus's table |

Once that lands, `scripts/run-agent-slice.sh` switches to durable submission
automatically (see "Modes" below) and no Tactus code changes.

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
| `integrations/dagster/agent_location.py` | Dagster: job `software_agent_job` with `prepare_op` → `execute_op` → `verify_op`, resource `SoftwareAgentSettings`. Fails closed without `TACTUS_AGENT_WORKSPACE_ROOT`; no retry policy and `allow_retries=False`, so an agent is never silently re-run. |
| `config/agent-runtime.json` | Operator-owned runtime pin (the analogue of `config/stax-pin.json`): runtime id/kind, adapter, executable path, exact SHA-256, argv and probe templates, expected version token, allowed scope, limits, and — since phase 2 — optional `artifacts`, `containment`, `env_allowlist` and `stdin_from`. The shipped fake pin declares **none** of those, so the attempt it describes runs uncontained; a pin that declares a launcher gets the boundary, verified at preflight. |
| `config/agent-runtime-canary.json` | The operator canary's pin: the Codex CLI release tree and executable by digest, the launcher by digest, denied roots, `egress: open` for that run only, the runtime's recorded allowances (`ipc-posix-shm` plus the three named mach services it cannot start without — `com.apple.cfprefsd.daemon`, `com.apple.cfprefsd.agent`, `com.apple.SecurityServer`), and its credential: the operator's session file (`credential_file`, `~/.codex/auth.json`) staged into the run's own home at `0600` and removed when the attempt ends. Exactly one credential route may be configured; `credential_env` (`CODEX_API_KEY`, a scoped platform key) is the documented alternative. Run through `scripts/run-agent-canary.sh`. |
| `scripts/run-agent-slice.sh`, `scripts/validate_agent_slice.py` | The evidence harness. |
| `tests/integrations/dagster/test_agent_attempt.py` | Dagster-free tests: pin drift, unknown/missing pin keys, unpinned runtime refused before anything is touched, success with real workspace edit, crash, timeout, scope violation, route-capability mismatch, JSON-safe accounts. |

The pin names **no vendor**. Runtime id, executable, argv/probe templates and the
expected version token are data, so the same contract carries Pi, Codex, a local
model or anything else that is a pinnable artifact with a probe and a protocol —
selecting one is a reviewed change to that file, not a code change. The
deterministic fake agent is the only runtime pinned today.

Step names matter: the bridge normalizes a failure by step key
(`execute_op` → `PROCESS_CRASH`, `verify_op` → `VERIFICATION_FAILURE`), so this
location never reclassifies a failure and never invents a category.

Runtime selection, workspace and limits come from the operator pin and the
process environment, **never** from the intent: the pinned mapper sends only
`{"capability", "placeholder"}`, `placeholder` is unused, and `capability` is
only *checked* against this location's route constant — a run config naming any
other capability fails closed, and the attempt's capability is always the
constant, never the incoming label.

## Modes of the harness

The harness probes the seam *before* submitting anything and records what it
proved in the evidence file:

| Mode | When | What it proves |
|---|---|---|
| `durable-submission` | pinned job steps equal the Tactus location's steps | The whole chain, including durable submission through the pinned bridge and Tactus applying the terminal result exactly once. |
| `in-process` | the pinned job still has only the placeholder step (today, until B4 lands) | A real Ictus ALLOW + intent for `software.implement`, a real Dagster execution of the Tactus job, one supervised attempt under the pin, and a recorded patch that applies to a clean copy of the original workspace. `blocked_dependencies` names B4. |

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
