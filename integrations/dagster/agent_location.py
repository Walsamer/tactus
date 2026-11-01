"""Tactus-owned Dagster code location that serves the ``software.implement`` route.

This is **not** a policy engine, a scheduler or a second workflow engine. It owns
exactly one thing that neither Tactus's domain core nor the pinned Ictus revision
can own themselves: a capability implementation. The Ictus bridge submits an
authorized ``ExecutionIntent`` by *code-location name* plus the job name resolved
from its own ``workflows.toml``, so the job here keeps that name and the resource
key/shape the bridge emits and nothing else:

===================  ==========================================================
``software_agent_job``  the job name the pinned bridge routes ``software.implement`` to
``software_agent_settings``  the resource key whose config the bridge sends: ``{"capability", "placeholder"}``
===================  ==========================================================

Boundary contract (see ``docs/architecture/AGENT_EXECUTION_SLICE.md``)
---------------------------------------------------------------------
* The intent is submitted unchanged and is **not** a side channel: the pinned
  settings mapper sends only ``{"capability", "placeholder"}``, so runtime
  selection, workspace and limits come from the operator-owned pin
  (``config/agent-runtime.json``) and from explicit process environment, never
  from an authorized intent. ``capability`` is only checked against this code
  location's own route constant; ``placeholder`` is unused.
* The runtime is explicit and pinned (:mod:`agent_attempt`): no discovery, no
  fallback, digest-verified before preflight and spawning.
* Supervision, deadlines, process-group termination and restart recovery belong
  to :class:`tactus.runtime.RuntimeSupervisor`; this module only wires one
  attempt into a Dagster step and reports its bounded evidence.
* A failed attempt is a **terminal step failure** (``allow_retries=False``): an
  agent must never be silently re-run by Dagster to "repair" execution. Tactus
  and Ictus decide what a failure means.
* Step names are the pinned bridge's own vocabulary (``execute_op`` →
  ``PROCESS_CRASH``, ``verify_op`` → ``VERIFICATION_FAILURE``), so a failure is
  normalized by the bridge's table and never reclassified here.
* Nothing here publishes: the attempt summary states ``publication_authority``
  false, and no credential is forwarded to the child.

Environment (set by the operator or by ``scripts/run-agent-slice.sh``)
----------------------------------------------------------------------
``TACTUS_AGENT_WORKSPACE_ROOT``  absolute path of the workspace the agent may edit (required)
``TACTUS_AGENT_RUNTIME_CONFIG``  runtime pin file (default: ``config/agent-runtime.json``)
``TACTUS_AGENT_STATE_DIR``       supervisor state directory (default: inside the workspace)
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from dagster import ConfigurableResource, Definitions, Failure, In, Out, job, op

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
for _import_root in (_HERE, _REPO_ROOT / "src"):  # loaded by path (`python_file`)
    if str(_import_root) not in sys.path:
        sys.path.insert(0, str(_import_root))

from agent_attempt import (  # noqa: E402  (path bootstrap above)
    AgentAttemptError,
    AgentRouteMismatch,
    AgentRuntimePin,
    assert_route_capability,
    load_agent_runtime_pin,
    run_agent_attempt,
)
from tactus.integrations.agents import AgentAdapterError  # noqa: E402
from tactus.runtime import (  # noqa: E402
    RuntimeRecoveryRequired,
    RuntimeSupervisionError,
    Workspace,
    WorkspaceScopeError,
    normalize_changed_path,
)

#: Intent-id run tag written by the Ictus submission bridge.
INTENT_TAG = "ictus.intent_id"

#: The one capability this code location implements. It is a **route constant**,
#: never intent-derived: the pinned settings mapper sends a constant label and
#: Tactus checks it against this value instead of trusting or reporting it.
CAPABILITY = "software.implement"

#: Operator configuration (never read from the intent).
CONFIG_ENV = "TACTUS_AGENT_RUNTIME_CONFIG"
WORKSPACE_ENV = "TACTUS_AGENT_WORKSPACE_ROOT"
STATE_ENV = "TACTUS_AGENT_STATE_DIR"


class SoftwareAgentSettings(ConfigurableResource):
    """Resource config the pinned Ictus bridge sends for ``software.implement``.

    The fields must accept exactly what the bridge's own settings mapper emits
    (``{"capability": "software.implement", "placeholder": true}``) so the run
    config validates here. They select **nothing**: ``placeholder`` is unused and
    ``capability`` is only checked against :data:`CAPABILITY`, so an intent can
    never choose a capability, runtime, workspace or limit through this path.
    """

    capability: str = CAPABILITY
    placeholder: bool = True


def _route_capability(settings: SoftwareAgentSettings) -> str:
    """Return the route capability, refusing a run config that names another one."""

    try:
        return assert_route_capability(settings.capability, CAPABILITY)
    except AgentRouteMismatch as exc:
        raise Failure(
            description=str(exc),
            metadata={"tactus.agent.capability": settings.capability},
            allow_retries=False,
        ) from exc


def _environment() -> dict[str, str]:
    """Resolve the operator-owned configuration, failing closed."""

    raw_workspace = os.environ.get(WORKSPACE_ENV, "").strip()
    if not raw_workspace:
        raise Failure(
            description=(
                f"{WORKSPACE_ENV} is not set: a bounded agent workspace must be named "
                "explicitly; this code location has no default workspace"
            ),
            metadata={"tactus.missing_config": WORKSPACE_ENV},
        )
    workspace = Path(raw_workspace).expanduser()
    if not workspace.is_absolute():
        raise Failure(
            description=f"{WORKSPACE_ENV} must be an absolute path (got {raw_workspace!r})",
            metadata={"tactus.invalid_config": WORKSPACE_ENV},
        )
    if not workspace.resolve().is_dir():
        raise Failure(
            description=f"{WORKSPACE_ENV} is not a directory: {workspace}",
            metadata={"tactus.invalid_config": WORKSPACE_ENV},
        )
    config_path = os.environ.get(CONFIG_ENV, "").strip()
    state_dir = os.environ.get(STATE_ENV, "").strip()
    return {
        "workspace_root": str(workspace.resolve()),
        "config_path": config_path,
        "state_dir": state_dir,
    }


def _pin(environment: dict[str, str]) -> AgentRuntimePin:
    try:
        return load_agent_runtime_pin(
            environment["config_path"] or None, repo_root=_REPO_ROOT
        )
    except AgentAttemptError as exc:
        raise Failure(
            description=f"agent runtime pin rejected: {exc}",
            metadata={"tactus.agent.runtime_pin": "rejected"},
        ) from exc


def _attempt_id(context: Any, intent_id: str) -> tuple[str, int]:
    """One semantic attempt, one Dagster step attempt: retries are visible, never hidden."""

    attempt = int(context.retry_number) + 1
    return intent_id, attempt


@op(out=Out(dict))
def prepare_op(context, software_agent_settings: SoftwareAgentSettings) -> dict:
    """Resolve and verify the pinned runtime before any child process exists."""
    capability = _route_capability(software_agent_settings)
    environment = _environment()
    pin = _pin(environment)
    context.log.info(
        "tactus.agent.prepare %s",
        json.dumps(
            {
                "capability": capability,
                "runtime_id": pin.runtime_id,
                "runtime_kind": pin.runtime_kind,
                "executable": str(pin.executable),
                "executable_sha256": pin.executable_sha256,
                "workspace_root": environment["workspace_root"],
                "allowed_scope": list(pin.allowed_scope),
            },
            sort_keys=True,
        ),
    )
    return {
        "capability": capability,
        "runtime": pin.as_dict(),
        "workspace_root": environment["workspace_root"],
        "config_path": environment["config_path"],
        "state_dir": environment["state_dir"],
    }


@op(ins={"prepared": In(dict)}, out=Out(dict))
def execute_op(context, prepared: dict) -> dict:
    """Supervise exactly one bounded agent attempt (never retried automatically)."""
    intent_id = str((context.run_tags or {}).get(INTENT_TAG, "")).strip()
    if not intent_id:
        raise Failure(
            description=(
                f"the run carries no {INTENT_TAG} tag: an attempt is only run for an "
                "identified authorized intent"
            ),
            metadata={"tactus.missing_tag": INTENT_TAG},
        )
    intent_id, attempt = _attempt_id(context, intent_id)
    try:
        summary = run_agent_attempt(
            intent_id=intent_id,
            capability=str(prepared["capability"]),
            attempt=attempt,
            workspace_root=prepared["workspace_root"],
            repo_root=_REPO_ROOT,
            config_path=prepared["config_path"] or None,
            state_dir=prepared["state_dir"] or None,
        )
    except (AgentAdapterError, AgentAttemptError, RuntimeSupervisionError) as exc:
        metadata: dict[str, Any] = {
            "tactus.agent.attempt": "unsupervised",
            "tactus.agent.error": type(exc).__name__,
            "tactus.agent.intent_id": intent_id,
            "tactus.agent.attempt_number": attempt,
        }
        # A fail-closed recovery hold is an operator decision, not a retry: name the
        # durable record the operator has to settle.
        if isinstance(exc, RuntimeRecoveryRequired):
            metadata["tactus.agent.recovery_phase"] = exc.phase
            metadata["tactus.agent.recovery_record"] = str(exc.record_path)
            metadata["tactus.agent.attempt_id"] = exc.attempt_id
        raise Failure(
            description=f"agent attempt could not be supervised: {exc}",
            metadata=metadata,
            allow_retries=False,
        ) from exc

    context.log.info("tactus.agent.attempt %s", json.dumps(summary, sort_keys=True))
    if summary["status"] != "success":
        raise Failure(
            description=(
                f"agent attempt {summary['attempt_id']} ended {summary['status']!r} "
                f"(capability {summary['capability']}, runtime "
                f"{summary['runtime']['runtime_id']})"
            ),
            metadata={
                "tactus.agent.attempt_id": summary["attempt_id"],
                "tactus.agent.status": summary["status"],
                "tactus.agent.runtime_id": summary["runtime"]["runtime_id"],
                "tactus.agent.runtime_kind": summary["runtime"]["runtime_kind"],
                "tactus.agent.attempt_dir": summary["process"]["attempt_dir"],
                "tactus.agent.errors": list(summary["errors"]),
                "ictus.intent_id": intent_id,
            },
            allow_retries=False,
        )
    return {"capability": prepared["capability"], "attempt": summary}


@op(ins={"executed": In(dict)}, out=Out(dict))
def verify_op(context, executed: dict) -> dict:
    """Re-check the reported patch evidence against the artifacts on disk."""

    summary: dict[str, Any] = executed["attempt"]
    artifacts = summary["artifacts"]
    limit = int(summary["limits"]["max_patch_bytes"])
    patch_path = Path(str(artifacts["patch_path"]))
    try:
        with patch_path.open("rb") as handle:
            raw = handle.read(limit + 1)
    except OSError as exc:
        raise Failure(
            description=f"agent patch artifact is unreadable: {patch_path}: {exc}",
            metadata={"tactus.agent.attempt_id": summary["attempt_id"]},
        ) from exc
    if len(raw) > limit:
        raise Failure(
            description=(
                f"agent patch artifact exceeds the {limit}-byte limit: {patch_path}"
            ),
            metadata={"tactus.agent.attempt_id": summary["attempt_id"]},
        )
    digest = hashlib.sha256(raw).hexdigest()
    problems: list[str] = []
    if digest != artifacts["patch_sha256"]:
        problems.append(
            f"patch digest {digest} does not match the reported {artifacts['patch_sha256']}"
        )
    if len(raw) != artifacts["patch_bytes"]:
        problems.append(
            f"patch is {len(raw)} bytes, reported {artifacts['patch_bytes']}"
        )
    changed = [str(path) for path in artifacts["changed_paths"]]
    if not changed:
        problems.append("a successful attempt reported no changed paths")
    # The scope rule is the pin's, not the account's: compare the two, then reuse
    # the single implementation of "covered by the allowed scope".
    environment = _environment()
    pin = _pin(environment)
    reported_scope = [str(entry) for entry in summary["workspace"]["allowed_scope"]]
    try:
        normalized_reported = [normalize_changed_path(entry) for entry in reported_scope]
        normalized_pinned = [normalize_changed_path(entry) for entry in pin.allowed_scope]
    except WorkspaceScopeError as exc:
        problems.append(f"the reported allowed scope is unusable: {exc}")
        normalized_reported, normalized_pinned = ["?"], ["!"]
    if normalized_reported != normalized_pinned:
        problems.append(
            f"the attempt ran scope {reported_scope}, the pin requires "
            f"{list(pin.allowed_scope)}"
        )
    try:
        workspace = Workspace(
            root=Path(summary["workspace"]["root"]),
            allowed_scope=pin.allowed_scope,
        )
        outside = [path for path in changed if not workspace.covers(path)]
    except WorkspaceScopeError as exc:
        problems.append(f"the attempt workspace cannot be re-checked: {exc}")
        outside = []
    if outside:
        problems.append("changed paths outside the allowed scope: " + ", ".join(outside))
    if summary["publication_authority"] is not False:
        problems.append("the attempt claims publication authority")
    # Declared paths and patch headers are all the supervisor can check without a
    # workspace diff provider; the account must carry that fact forward.
    write_scope = dict(summary.get("write_scope") or {})
    if "verified" not in write_scope:
        problems.append("the attempt account does not state whether the write scope was verified")
    if problems:
        raise Failure(
            description="agent attempt evidence failed verification: " + "; ".join(problems),
            metadata={
                "tactus.agent.attempt_id": summary["attempt_id"],
                "tactus.agent.attempt_dir": summary["process"]["attempt_dir"],
            },
        )
    context.log.info(
        "tactus.agent.verified %s",
        json.dumps(
            {
                "attempt_id": summary["attempt_id"],
                "patch_sha256": digest,
                "patch_bytes": len(raw),
                "changed_paths": changed,
                "publication_authority": False,
                "isolation": summary["isolation"],
                "write_scope_verified": bool(write_scope.get("verified")),
            },
            sort_keys=True,
        ),
    )
    return {
        "capability": CAPABILITY,
        "attempt_id": summary["attempt_id"],
        "runtime_id": summary["runtime"]["runtime_id"],
        "runtime_kind": summary["runtime"]["runtime_kind"],
        "changed_paths": changed,
        "patch_sha256": digest,
        "patch_bytes": len(raw),
        "patch_path": str(patch_path),
        "attempt_dir": summary["process"]["attempt_dir"],
        "publication_authority": False,
        "isolation": summary["isolation"],
        "write_scope": write_scope,
        "verified": True,
    }


@job(
    name="software_agent_job",
    resource_defs={"software_agent_settings": SoftwareAgentSettings()},
    description=(
        "Tactus-owned implementation of the software.implement route: one pinned, "
        "supervised agent attempt with patch evidence. No policy, no publication, "
        "no automatic retry."
    ),
)
def software_agent_job() -> None:
    prepared = prepare_op()
    executed = execute_op(prepared)
    verify_op(executed)


defs = Definitions(jobs=[software_agent_job])
