"""The Tactus runtime backend that Ictus's seam port reaches by name.

Ictus owns the step skeleton (``prepare -> execute -> verify -> finalize``) and
owns the attempt's bounds. This module owns the runtime: it runs exactly one
supervised agent attempt through :class:`tactus.runtime.RuntimeSupervisor` and
reports what happened as a seam evidence document. Ictus names it in an
operator-owned seam configuration as::

    "backend": "agent_backend:create_backend"

and imports it lazily, with no arguments, only when a seam is configured. This
module therefore imports no Dagster: it is loaded by the worker that Ictus's
job runs in, not by a code location.

What it reads from where
------------------------
* the **runtime pin** (descriptor) path arrives as
  ``WorkspaceRef.descriptor_path`` / ``AttemptRequest.workspace`` — Ictus holds
  the path and never opens it. This module decodes it through
  :func:`agent_attempt.load_agent_runtime_pin`; there is no second loader.
* the attempt's **bounds** — ``write_scope``, ``wall_clock_seconds``,
  ``step_budget`` — arrive on :class:`AttemptRequest` and nowhere else. The
  descriptor carries runtime identity and :attr:`AgentRuntimePin.runtime_caps`
  (the runtime's own process hygiene), never a second set of bounds.
* the **workspace root** is not on the request, so it comes from the operator
  environment: ``TACTUS_AGENT_WORKSPACE_ROOT`` (required) and
  ``TACTUS_AGENT_STATE_DIR`` (optional), the same surfaces the code location
  used before the seam existed.

Why ``control.spawn`` is not used
---------------------------------
:class:`tactus.runtime.RuntimeSupervisor` starts the runtime itself, in its own
process group, and owns termination: SIGTERM, then SIGKILL, then reaping, then
a survivor sweep, and only then a *confirmed* termination before the writer
lock is released (see ``docs/architecture/RUNTIME_SUPERVISION.md``). Routing the
spawn through ``control.spawn`` as well would give the attempt two supervisors
with two opinions about one process group.

The cost is stated rather than hidden, and it is the cost Ictus's own contract
anticipates: a process group started outside ``control`` is invisible to it, so
``control.finish()`` correctly reports no leftover and Ictus's
``SUPERVISION_VIOLATION`` can never fire for us; ``control.cancel`` cannot reach
our children either, which is why :meth:`TactusRuntimeBackend.execute` feeds
``control.cancelled`` into the supervisor and lets the supervisor do the
terminating and the confirming. The ``termination`` this module reports is the
supervisor's own observation, not Ictus's, and it is the stronger of the two.
"""

from __future__ import annotations

import hashlib
import os
import signal
import sys
from pathlib import Path
from typing import Any, Mapping

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
for _import_root in (_HERE, _REPO_ROOT / "src"):  # loaded by name, not as a package
    if str(_import_root) not in sys.path:
        sys.path.insert(0, str(_import_root))

from agent_attempt import (  # noqa: E402  (path bootstrap above)
    AgentAttemptError,
    load_agent_runtime_pin,
    run_agent_attempt,
)
from tactus.runtime import (  # noqa: E402
    RuntimePreflightError,
    RuntimeRecoveryRequired,
    RuntimeSupervisionError,
    preflight,
)

#: Operator configuration, never read from the seam request.
WORKSPACE_ENV = "TACTUS_AGENT_WORKSPACE_ROOT"
STATE_ENV = "TACTUS_AGENT_STATE_DIR"

#: The one capability this backend implements. Ictus's seam names a single
#: backend for both software routes, so the backend itself has to refuse the one
#: it cannot honour rather than run a workspace editor for a promotion.
IMPLEMENTED_CAPABILITY = "software.implement"

#: Version of the evidence document this backend returns. Ictus refuses any
#: other value, which is the point: pin it and fail closed on drift.
EVIDENCE_SCHEMA_VERSION = 1

#: Attempt outcome per supervisor status. The seam's vocabulary is smaller than
#: the supervisor's because Ictus does not distinguish "the contract was
#: violated" from "it failed" — it asks *why* separately, in ``failure_kind``.
OUTCOME_BY_STATUS: dict[str, str] = {
    "success": "succeeded",
    "failed": "failed",
    "timeout": "timed_out",
    "cancelled": "cancelled",
    "invalid": "failed",
    "unterminated": "failed",
}

#: Typed failure kind per supervisor status. ``failure_kind`` is ``None`` exactly
#: when the outcome is ``succeeded``; Ictus refuses the document otherwise.
FAILURE_KIND_BY_STATUS: dict[str, str] = {
    "failed": "PROCESS_EXIT",
    "timeout": "WALL_CLOCK_EXCEEDED",
    "cancelled": "CANCEL_REQUESTED",
    "invalid": "RUNTIME_VERIFICATION_FAILED",
    "unterminated": "TERMINATION_UNCONFIRMED",
}


class TactusRuntimeBackend:
    """Ictus's seam port, answered by one supervised Tactus attempt."""

    # -- describe_workspace -------------------------------------------------

    def describe_workspace(self, workspace: Any) -> dict[str, Any]:
        """Report the runtime's identity and the result of its own probe.

        Ictus never opens the descriptor; this is the report it records as
        evidence instead. A probe that cannot run is reported as
        ``probe.ok = false`` with the reason in ``detail``, which Ictus turns
        into ``WORKSPACE_PROBE_FAILED`` — so the detail has to be the real
        reason, not a generic string.
        """
        descriptor_path = str(workspace.descriptor_path)
        try:
            pin = load_agent_runtime_pin(descriptor_path, repo_root=_REPO_ROOT)
        except AgentAttemptError as exc:
            return _probe_report("", "", ok=False, detail=str(exc))
        executable = str(pin.executable)
        try:
            provenance = preflight(pin.descriptor())
        except RuntimePreflightError as exc:
            return _probe_report(executable, "", ok=False, detail=str(exc))
        return _probe_report(
            executable,
            provenance.reported_version,
            ok=True,
            # The probe mode is the honest qualifier: with a pinned launcher the
            # version was reported from inside the boundary, not outside it.
            detail=f"probe mode: {provenance.probe_mode}",
        )

    # -- execute ------------------------------------------------------------

    def execute(self, request: Any, control: Any) -> dict[str, Any]:
        """Run one bounded attempt and return a validated evidence document.

        Two refusals are reported as evidence rather than raised, so that Ictus
        records the typed kind instead of a generic ``BACKEND_ERROR``:
        an attempt cancelled before anything starts, and an operator
        configuration this backend cannot honour.
        """
        if control.cancelled:
            return _halted_evidence(
                status="cancelled",
                reason="the attempt was already cancelled before it started",
            )
        if str(request.capability) != IMPLEMENTED_CAPABILITY:
            return _halted_evidence(
                status="failed",
                failure_kind="CONFIGURATION",
                reason=(
                    f"this runtime backend implements '{IMPLEMENTED_CAPABILITY}' only; "
                    f"it will not run a '{request.capability}' attempt. A publication "
                    "route needs a backend that publishes, and reporting success "
                    "without publishing is worse than failing closed."
                ),
            )
        workspace_root, root_problem = _workspace_root()
        if workspace_root is None:
            return _halted_evidence(
                status="failed", failure_kind="CONFIGURATION", reason=str(root_problem)
            )
        scope_problem = _scope_problem(request.write_scope)
        if scope_problem is not None:
            return _halted_evidence(
                status="failed", failure_kind="CONFIGURATION", reason=scope_problem
            )
        descriptor_path = str(request.workspace.descriptor_path)
        # One supervised process is one semantic step. Counting it here is what
        # makes Ictus's step budget mean something; a future multi-step runtime
        # counts each step through the same call. A budget overrun raises out of
        # here and Ictus records STEP_BUDGET_EXCEEDED itself.
        control.step()
        try:
            summary = run_agent_attempt(
                intent_id=str(request.capability),
                capability=str(request.capability),
                attempt=1,
                workspace_root=workspace_root,
                write_scope=tuple(str(entry) for entry in request.write_scope),
                wall_clock_seconds=float(request.wall_clock_seconds),
                repo_root=_REPO_ROOT,
                config_path=descriptor_path,
                state_dir=os.environ.get(STATE_ENV, "").strip() or None,
                # A cancellation delivered through a channel the pinned contract
                # does not yet have still enters the supervisor's
                # terminate/confirm/release path, which is the only component that
                # can confirm it. At the pin this is dormant: ``control.cancel`` is
                # only reachable after ``execute`` returns, so nothing can set it
                # mid-attempt yet: ``ictus_dagster/seam/runtime.py::execute`` only calls it
                # from its own handlers, after ``execute`` returns or raises.
                cancelled=lambda: bool(control.cancelled),
            )
        except (AgentAttemptError, RuntimeSupervisionError) as exc:
            return _halted_evidence(
                status="failed",
                failure_kind=_refusal_kind(exc),
                reason=f"the attempt could not be supervised: {exc}",
                # The step was counted before the attempt was refused.
                steps_used=int(getattr(control, "steps_used", 1)),
            )
        return _evidence(summary, descriptor_path=descriptor_path)

    # -- verify -------------------------------------------------------------

    def verify(self, evidence: Mapping[str, Any]) -> dict[str, Any]:
        """Check the runtime's own post-conditions on the returned evidence.

        Ictus has already re-checked the changed paths against the operator's
        write scope and the step/wall-clock budgets before calling this. What is
        left is the runtime's own account: the patch it claims to have produced
        must still be on disk, still match the digest and size it reported, and
        the attempt must not claim publication authority.
        """
        tactus = dict(evidence.get("tactus") or {})
        problems: list[str] = []
        if tactus.get("publication_authority") is not False:
            problems.append("the attempt claims publication authority")
        changed = [str(path) for path in evidence.get("changed_paths") or ()]
        if evidence.get("outcome") == "succeeded" and not changed:
            problems.append("a successful attempt reported no changed paths")
        patch_path = str(tactus.get("patch_path") or "")
        reported_digest = evidence.get("patch_digest")
        reported_bytes = int(evidence.get("patch_bytes") or 0)
        if patch_path:
            limit, unreadable = _patch_limit(tactus)
            problems.extend(
                _patch_problems(
                    Path(patch_path),
                    reported_digest=reported_digest,
                    reported_bytes=reported_bytes,
                    limit=limit,
                    unreadable=unreadable,
                )
            )
        elif reported_digest is not None:
            problems.append(
                f"the attempt reports patch {reported_digest} but names no patch artifact"
            )
        if problems:
            return {"verified": False, "detail": "; ".join(problems)}
        return {
            "verified": True,
            "detail": (
                f"{len(changed)} changed path(s)"
                if patch_path
                else "no patch artifact to re-check"
            ),
        }


def create_backend() -> TactusRuntimeBackend:
    """Construct the backend. Ictus calls this attribute with no arguments."""

    return TactusRuntimeBackend()


# -- evidence translation ---------------------------------------------------


def _evidence(summary: Mapping[str, Any], *, descriptor_path: str) -> dict[str, Any]:
    """Translate the supervisor's account into a seam evidence document.

    Every field here either exists on the account under another name or is a
    derived fact whose derivation is stated. Nothing is invented to make the
    document validate.
    """
    status = str(summary["status"])
    process = dict(summary["process"])
    termination = dict(summary["termination"])
    artifacts = dict(summary["artifacts"])
    runtime = dict(summary["runtime"])
    outcome = OUTCOME_BY_STATUS.get(status, "failed")
    digest = str(artifacts.get("patch_sha256") or "")
    signals = [str(name) for name in termination.get("signals") or ()]
    cancel_reason = str(process.get("cancel_reason") or "")
    # ``Popen.returncode`` is negative exactly when the child died by a signal.
    # Reporting that as an exit code would be false in both halves of the pair
    # Ictus defines, so it is split back into the two facts it came from.
    exit_code = process.get("exit_code")
    exit_signal: str | None = None
    if isinstance(exit_code, int) and exit_code < 0:
        exit_signal = _signal_name(-exit_code)
        exit_code = None
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "outcome": outcome,
        "failure_kind": _failure_kind(status, outcome, process),
        "changed_paths": [str(path) for path in artifacts.get("changed_paths") or ()],
        # The seam wants ``sha256:<hex>``; the supervisor records bare hex and
        # no digest at all when the attempt produced no patch.
        "patch_digest": f"sha256:{digest}" if digest else None,
        "patch_bytes": int(artifacts.get("patch_bytes") or 0),
        # One supervised process is one semantic step (the control counted it).
        "steps_used": 1,
        # The supervisor records the exit code, and a negative one is a signal
        # death rather than an exit; both halves are reported as what they are.
        "exit_status": {"code": exit_code, "signal": exit_signal},
        "termination": {
            # A termination was requested when we signalled the group, when the
            # deadline fired, or when a cancellation named a reason. A clean
            # exit requests nothing and sends nothing.
            "requested": bool(signals) or bool(cancel_reason) or bool(process.get("timed_out")),
            "signals": signals,
            "confirmed": bool(termination.get("confirmed")),
        },
        "duration_seconds": max(0.0, float(process.get("duration_ms") or 0) / 1000.0),
        # Unknown fields are preserved by Ictus for the runtime's own
        # diagnostics. This is where the identity a reader needs lives, and it
        # is what ``verify`` reads back.
        "tactus": {
            "attempt_id": summary.get("attempt_id"),
            "attempt_dir": process.get("attempt_dir"),
            "descriptor_path": descriptor_path,
            "runtime_id": runtime.get("runtime_id"),
            "runtime_kind": runtime.get("runtime_kind"),
            "reported_version": runtime.get("reported_version"),
            "workspace_root": (summary.get("workspace") or {}).get("root"),
            "patch_path": artifacts.get("patch_path"),
            "publication_authority": summary.get("publication_authority"),
            "isolation": summary.get("isolation"),
            "write_scope": dict(summary.get("write_scope") or {}),
            "errors": list(summary.get("errors") or ()),
        },
    }


def _failure_kind(status: str, outcome: str, process: Mapping[str, Any]) -> str | None:
    """The typed kind for this outcome, or ``None`` when it succeeded.

    ``failed`` is two different facts and they are not merged: a non-zero exit
    is the process's own failure (``PROCESS_EXIT``), while an agent that
    reports failure without a failing process is a backend failure.
    """
    if outcome == "succeeded":
        return None
    if status == "failed" and process.get("exit_code") in (0, None):
        return "BACKEND_ERROR"
    return FAILURE_KIND_BY_STATUS.get(status, "BACKEND_ERROR")


def _halted_evidence(
    *, status: str, reason: str, failure_kind: str | None = None, steps_used: int = 0
) -> dict[str, Any]:
    """Evidence for an attempt that never started, with nothing to confirm.

    ``termination.confirmed`` is true because there was no process group to
    outlive us — not because a kill was observed. Ictus refuses only
    ``requested and not confirmed``, so this claims nothing it did not do.
    """

    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "outcome": status,
        "failure_kind": failure_kind or FAILURE_KIND_BY_STATUS.get(status, "BACKEND_ERROR"),
        "changed_paths": [],
        "patch_digest": None,
        "patch_bytes": 0,
        "steps_used": steps_used,
        # No process group existed, so nothing was requested and nothing needs
        # confirming — which is not the same claim as a confirmed termination.
        "exit_status": {"code": None, "signal": None},
        "termination": {"requested": False, "signals": [], "confirmed": True},
        "duration_seconds": 0.0,
        "tactus": {
            "attempt_id": None,
            "attempt_dir": None,
            "descriptor_path": None,
            "runtime_id": None,
            "runtime_kind": None,
            "patch_path": None,
            "publication_authority": False,
            "isolation": None,
            "write_scope": {},
            "errors": [reason],
        },
    }


def _refusal_kind(exc: Exception) -> str:
    """The typed kind for a supervision refusal that happened before a child ran."""

    if isinstance(exc, RuntimeRecoveryRequired):
        # An unresolved previous attempt is an operator decision, not a crash:
        # nothing may start until it is settled, and the category says so.
        return "SUPERVISION_VIOLATION"
    return "CONFIGURATION"


# -- helpers ----------------------------------------------------------------


def _probe_report(
    executable: str, version_token: str, *, ok: bool, detail: str
) -> dict[str, Any]:
    return {
        "executable": executable,
        "version_token": version_token,
        "probe": {"ok": ok, "detail": detail},
    }


def _signal_name(number: int) -> str | None:
    """The signal's name, or ``None`` when the number names none on this platform."""

    try:
        return signal.Signals(number).name
    except ValueError:
        return None


def _workspace_root() -> tuple[str | None, str | None]:
    """The operator's workspace root, or the reason it is unusable.

    A root that is set but not an absolute existing directory is a
    configuration failure, not a backend crash: the supervisor would raise a
    ``WorkspaceScopeError`` and Ictus would record BACKEND_ERROR, which hides an
    operator mistake behind a process incident.
    """

    raw = os.environ.get(WORKSPACE_ENV, "").strip()
    if not raw:
        return None, (
            f"{WORKSPACE_ENV} is not set: a bounded agent workspace must be "
            "named explicitly; this backend has no default workspace"
        )
    root = Path(raw).expanduser()
    if not root.is_absolute():
        return None, f"{WORKSPACE_ENV} must be an absolute path (got {raw!r})"
    resolved = root.resolve()
    if not resolved.is_dir():
        return None, f"{WORKSPACE_ENV} is not a directory: {root}"
    if resolved == _REPO_ROOT or _REPO_ROOT in resolved.parents:
        return None, (
            f"{WORKSPACE_ENV} points inside this repository ({resolved}); an allowed "
            "path there is not a bounded agent workspace"
        )
    return str(root), None


def _scope_problem(write_scope: Any) -> str | None:
    """The reason this backend cannot honour a write scope, or ``None``.

    Ictus permits absolute scope entries because it has no workspace root and
    compares paths syntactically (``seam/config.py``). This backend does have a
    root, and its scope entries are repository-relative, so an absolute entry is
    refused here as a typed ``CONFIGURATION`` failure instead of escaping as a
    ``WorkspaceScopeError`` that Ictus would record as BACKEND_ERROR.
    """

    entries = [str(entry) for entry in write_scope]
    if not entries:
        return "the attempt request carries no write scope; a bounded attempt needs one"
    for entry in entries:
        if entry.startswith("/"):
            return (
                f"write scope entry {entry!r} is absolute. Ictus permits an absolute "
                "entry because it has no workspace root; this backend has one, so it "
                "supports repository-relative scope entries only."
            )
        if entry.strip() in ("", ".", "./"):
            return f"write scope entry {entry!r} names no path"
    return None


def _patch_limit(tactus: Mapping[str, Any]) -> tuple[int | None, str]:
    """The runtime's own patch-byte cap and why it is unavailable, if it is.

    ``None`` means "cannot check", which fails the verification rather than
    silently substituting a number the runtime never agreed to. The reason is
    carried through because the re-read also re-verifies the executable digest,
    so "replaced mid-run" and "pin removed" are different facts.
    """

    descriptor_path = str(tactus.get("descriptor_path") or "")
    if not descriptor_path:
        return None, "the evidence names no descriptor"
    try:
        pin = load_agent_runtime_pin(descriptor_path, repo_root=_REPO_ROOT)
    except AgentAttemptError as exc:
        return None, str(exc)
    return int(pin.runtime_caps.max_patch_bytes), ""


def _patch_problems(
    patch_path: Path,
    *,
    reported_digest: Any,
    reported_bytes: int,
    limit: int | None,
    unreadable: str = "",
) -> list[str]:
    """Re-check the patch artifact on disk against what the attempt reported."""

    if limit is None:
        return [
            "the runtime pin could not be re-read, so the patch byte cap cannot be "
            f"checked ({unreadable}); refusing to verify against a limit the runtime "
            "never agreed to"
        ]
    try:
        with patch_path.open("rb") as handle:
            raw = handle.read(limit + 1)
    except OSError as exc:
        return [f"patch artifact is unreadable: {patch_path}: {exc}"]
    problems: list[str] = []
    if len(raw) > limit:
        problems.append(f"patch artifact exceeds the {limit}-byte cap: {patch_path}")
    digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    if digest != reported_digest:
        problems.append(
            f"patch digest {digest} does not match the reported {reported_digest}"
        )
    if len(raw) != reported_bytes:
        problems.append(f"patch is {len(raw)} bytes, reported {reported_bytes}")
    return problems
