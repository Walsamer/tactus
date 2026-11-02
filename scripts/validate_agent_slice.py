#!/usr/bin/env python3
"""Validate the Tactus/Ictus/Dagster join for one supervised agent attempt.

One harness, two modes, decided by evidence rather than by a flag:

``durable-submission``
    The full chain: checked WorkOrder -> INITIAL ``StateSnapshot`` -> real
    pinned Ictus binary -> validated ``ExecutionIntent`` -> durable Dagster run
    (queued coordinator + independent daemon) -> Tactus-owned code location
    ``agent_location.py`` -> one supervised agent attempt -> bounded patch
    evidence -> Tactus applies the terminal result.

``in-process``
    The same job, executed by Dagster's own engine in-process, with the same
    Ictus-authorized intent, when the pinned Ictus ``software_agent_job`` still
    records only its placeholder step. Dagster replays the execution plan that
    *Ictus's* job definition recorded, so a real backend cannot receive that run
    until Ictus ships a real step skeleton (blocked dependency B4, see
    ``docs/architecture/AGENT_EXECUTION_SLICE.md``). The harness detects that
    seam before submitting anything, proves everything Tactus owns in-process,
    and records the blocked link as evidence instead of impersonating an op name.

What is proven either way, and what is not
------------------------------------------
Proven: an authorized intent for ``software.implement`` drives a real Dagster
execution of the Tactus-owned capability, which runs one supervised agent
attempt under the operator-owned runtime pin, and the recorded patch applies to
the original workspace.

Not proven, and never claimed: a real coding agent. The pinned runtime is the
deterministic fake agent (``runtime_kind: fake-canary``); every account records
``publication_authority: false`` and ``isolation: none`` because there is no
filesystem/network/credential containment (Issue #22 AC3, gate B2) and nothing
publishes (Issue #23).

Everything is written to a temporary root: the Dagster instance, the SQLite
ledger, the agent workspace and the supervisor evidence. Neither the Tactus nor
the Ictus checkout is mutated.

Usage::

    TACTUS_ICTUS_ROOT=/path/to/agentic-control scripts/run-agent-slice.sh \
        --evidence /tmp/tactus-agent-slice-evidence.json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"
for _import_root in (SRC_ROOT, SCRIPTS_DIR):
    if str(_import_root) not in sys.path:
        sys.path.insert(0, str(_import_root))

# Shared helpers are imported, not copied: the two evidence harnesses must not
# drift in how they verify the pin, decide, wait for a terminal run or reap the
# daemon.
import validate_execution_slice as slice_harness  # noqa: E402

from tactus.application import ExecutionCycle  # noqa: E402
from tactus.domain import (  # noqa: E402
    ClaimToken,
    DagsterRunId,
    InitialAuthorizationContext,
    OpenStatus,
    SourceRevision,
    TransitionAuthority,
    WorkOrder,
    WorkOrderRevision,
)
from tactus.integrations.ictus import (  # noqa: E402
    ICTUS_COMMIT,
    SemanticBudget,
    SnapshotPhase,
    StepRetryDiagnostic,
    build_state_snapshot,
    parse_observation,
)
from tactus.persistence.sqlite import SqliteExecutionAdmission  # noqa: E402

WORK_ORDER_ID = "WO-AGENT-EXECUTION-SLICE"
SOURCE_REVISION = "source:work-order:v1"
CAPABILITY = "software.implement"
CODE_LOCATION = "tactus-agent-runtime"
INTENT_TAG = "ictus.intent_id"
CONFIG_ENV = "TACTUS_AGENT_RUNTIME_CONFIG"
WORKSPACE_ENV = "TACTUS_AGENT_WORKSPACE_ROOT"
STATE_ENV = "TACTUS_AGENT_STATE_DIR"
ORIGINAL = 'def hello():\n    return "hello"\n'
UPDATED = 'def hello():\n    return "hello, world"\n    # fake agent edit\n'


class ValidationError(RuntimeError):
    """The bounded validation could not establish the required evidence."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _shipped_runtime_config() -> dict[str, Any]:
    return json.loads((REPO_ROOT / "config" / "agent-runtime.json").read_text())


def _work_order() -> WorkOrder:
    now = _now()
    work_order = WorkOrder.create(WORK_ORDER_ID)
    work_order.admit(
        reason="bounded agent-execution-slice validation",
        authority=TransitionAuthority.OPERATOR,
        at=now,
    )
    work_order.set_readiness(OpenStatus.READY)
    work_order.claim(
        reason="simulated authorized Work Order entering the agent slice",
        at=now,
    )
    return work_order


def _initial_snapshot(work_order: WorkOrder, snapshot_id: str) -> dict[str, Any]:
    """A genuine INITIAL authorization for the agent capability."""

    return build_state_snapshot(
        work_order=work_order,
        phase=SnapshotPhase.INITIAL,
        semantic_budget=SemanticBudget(0, 3),
        step_retry=StepRetryDiagnostic(0),
        snapshot_id=snapshot_id,
        timestamp=datetime.fromisoformat(slice_harness.to_canonical_utc(_now())),
        capability_id=CAPABILITY,
        dependencies_satisfied=True,
        source_revision=SOURCE_REVISION,
        work_order_revision=0,
    )


def _prepare_agent_root(work_root: Path) -> tuple[Path, Path]:
    agent_workspace = work_root / "agent-workspace"
    (agent_workspace / "src").mkdir(parents=True)
    (agent_workspace / "src" / "example.py").write_text(ORIGINAL, encoding="utf-8")
    return agent_workspace, work_root / "agent-state"


def _configure_agent_environment(
    work_root: Path, agent_workspace: Path, agent_state: Path
) -> None:
    os.environ[CONFIG_ENV] = str(REPO_ROOT / "config" / "agent-runtime.json")
    os.environ[WORKSPACE_ENV] = str(agent_workspace)
    os.environ[STATE_ENV] = str(agent_state)
    # Keep Dagster's gRPC unix sockets well below the 103-character limit.
    os.environ["TMPDIR"] = str(work_root)
    os.environ["TMP"] = str(work_root)
    os.environ["TEMP"] = str(work_root)


def _load_location() -> Any:
    """Import the Tactus code location by path (it is not an installed package)."""

    path = REPO_ROOT / "integrations" / "dagster" / "agent_location.py"
    spec = importlib.util.spec_from_file_location("tactus_agent_location", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ValidationError(f"cannot load the Tactus code location: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:  # pragma: no cover - environment guard
        raise ValidationError(
            f"cannot load {path}: {exc}. Run this harness with the pinned Ictus "
            "virtualenv (dagster installed), which scripts/run-agent-slice.sh does."
        ) from exc
    return module


def _use_pinned_ictus_package(ictus_root: Path) -> str:
    """Bind ``ictus_dagster`` to the pinned checkout, not to any installed copy.

    The seam and the submission must be judged against the revision the pin names.
    A virtualenv that carries an editable install of a *different* Ictus checkout
    would otherwise answer both questions silently.
    """

    pinned_python = (ictus_root / "python").resolve()
    if not (pinned_python / "ictus_dagster").is_dir():
        raise ValidationError(f"not an Ictus checkout at the pin: {ictus_root}")
    entry = str(pinned_python)
    if entry in sys.path:
        sys.path.remove(entry)
    sys.path.insert(0, entry)
    for name in [key for key in sys.modules if key.split(".")[0] == "ictus_dagster"]:
        del sys.modules[name]
    import ictus_dagster

    resolved = Path(ictus_dagster.__file__ or "").resolve()
    if pinned_python not in resolved.parents:
        raise ValidationError(
            f"ictus_dagster resolved to {resolved}, not to the pinned checkout "
            f"{pinned_python}; refusing to measure the seam against another revision"
        )
    return str(resolved)


def _seam(location: Any) -> dict[str, Any]:
    """Compare the pinned bridge's job shape with what Tactus implements.

    Dagster replays the execution plan recorded by *Ictus's* job definition, so a
    Tactus code location can only receive that run when its own job declares the
    same steps. That is a contract, not a detail: the check is explicit so a
    mismatch is named instead of surfacing as an opaque Dagster error.
    """

    from ictus_dagster.adapters.workflow_registry import resolve_workflow

    spec = resolve_workflow(CAPABILITY)
    pinned_steps = [node.name for node in spec.job.graph.node_defs]
    tactus_steps = [node.name for node in location.software_agent_job.graph.node_defs]
    matched = pinned_steps == tactus_steps
    return {
        "status": "matched" if matched else "blocked",
        "blocked_dependency": None
        if matched
        else (
            "B4: the pinned Ictus job software_agent_job records only its "
            "fail-closed placeholder step, so a real backend cannot receive that "
            "run. Ictus has scheduled the prepare/execute/verify skeleton (with "
            "software_publish_job for #23); this harness switches to durable "
            "submission by itself once it lands."
        ),
        "capability": CAPABILITY,
        "job": spec.job.name,
        "resource_key": spec.resource_key,
        "pinned_steps": pinned_steps,
        "tactus_steps": tactus_steps,
    }


def _attempt_summaries(agent_state: Path) -> list[dict[str, Any]]:
    """Read every durable attempt account the code location wrote."""

    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((agent_state / "attempts").glob("*/agent-attempt.json"))
    ]


def _assert_attempt_evidence(
    summary: dict[str, Any], *, intent_id: str, pinned: dict[str, Any]
) -> dict[str, Any]:
    """Verify the attempt account against the pin and the artifacts on disk."""

    if summary.get("status") != "success":
        raise ValidationError(
            "agent attempt did not succeed: " + json.dumps(summary, sort_keys=True)
        )
    if summary.get("intent_id") != intent_id:
        raise ValidationError(
            f"agent attempt ran for {summary.get('intent_id')!r}, "
            f"not the authorized intent {intent_id!r}"
        )
    if summary.get("capability") != CAPABILITY:
        raise ValidationError(f"agent attempt ran capability {summary.get('capability')!r}")
    runtime = summary["runtime"]
    if runtime["runtime_kind"] != "fake-canary":
        raise ValidationError(
            f"this slice must not claim a real runtime (got {runtime['runtime_kind']!r})"
        )
    if runtime["pinned_executable_sha256"] != pinned["executable_sha256"]:
        raise ValidationError("the attempt ran a runtime other than the operator pin")
    if runtime["observed_executable_sha256"] != pinned["executable_sha256"]:
        raise ValidationError("observed executable digest diverged from the pinned digest")
    if pinned["expected_version_token"] not in runtime["reported_version"]:
        raise ValidationError("the runtime did not report the pinned version token")
    if summary["publication_authority"] is not False:
        raise ValidationError("the attempt claims publication authority")
    if not str(summary["isolation"]).startswith("none"):
        raise ValidationError(f"unexpected isolation claim: {summary['isolation']!r}")
    if summary["writer_lock"].get("released") is not True:
        raise ValidationError("the writer lock was not released after confirmed termination")
    if summary["termination"].get("confirmed") is not True:
        raise ValidationError("process termination was not confirmed")

    artifacts = summary["artifacts"]
    if artifacts["changed_paths"] != ["src/example.py"]:
        raise ValidationError(f"unexpected changed paths: {artifacts['changed_paths']}")
    raw = Path(artifacts["patch_path"]).read_bytes()
    if len(raw) != artifacts["patch_bytes"]:
        raise ValidationError("reported patch size does not match the artifact")
    if hashlib.sha256(raw).hexdigest() != artifacts["patch_sha256"]:
        raise ValidationError("reported patch digest does not match the artifact")
    return summary


def _assert_patch_applies(work_root: Path, summary: dict[str, Any]) -> None:
    clean = work_root / "clean-workspace"
    (clean / "src").mkdir(parents=True, exist_ok=True)
    (clean / "src" / "example.py").write_text(ORIGINAL, encoding="utf-8")
    applied = subprocess.run(
        ["git", "apply", "--check", summary["artifacts"]["patch_path"]],
        cwd=clean,
        capture_output=True,
        text=True,
    )
    if applied.returncode != 0:
        raise ValidationError(
            "the recorded patch does not apply to the original workspace: "
            + applied.stderr.strip()
        )


def _write_dagster_home(work_root: Path) -> tuple[Path, Path]:
    dagster_home = work_root / "dagster_home"
    dagster_home.mkdir(parents=True, exist_ok=True)
    (dagster_home / "dagster.yaml").write_text(
        "run_coordinator:\n"
        "  module: dagster.core.run_coordinator\n"
        "  class: QueuedRunCoordinator\n"
        "run_launcher:\n"
        "  module: dagster.core.launcher\n"
        "  class: DefaultRunLauncher\n"
    )
    workspace = work_root / "workspace.yaml"
    # ``python_file.relative_path`` is rebased against the *workspace file*, so
    # the code location is named by absolute path with the repository root as its
    # import working directory.
    workspace.write_text(
        "load_from:\n"
        "  - python_file:\n"
        f"      relative_path: {REPO_ROOT / 'integrations' / 'dagster' / 'agent_location.py'}\n"
        f"      location_name: {CODE_LOCATION}\n"
        f"      working_directory: {REPO_ROOT}\n"
    )
    return dagster_home, workspace


def _configure_dagster(
    dagster_home: Path, ictus_root: Path, work_root: Path
) -> None:
    os.environ["DAGSTER_HOME"] = str(dagster_home)
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [str(ictus_root / "python"), str(SRC_ROOT), str(REPO_ROOT)]
    )
    os.environ["ICTUS_DAGSTER_CODE_LOCATION"] = CODE_LOCATION
    os.environ["ICTUS_DAGSTER_REPOSITORY"] = "__repository__"
    os.environ["TMPDIR"] = str(work_root)
    if str(ictus_root / "python") not in sys.path:
        sys.path.insert(0, str(ictus_root / "python"))


def _run_in_process(location: Any, intent_id: str) -> dict[str, Any]:
    """Execute the real Tactus job with Dagster's engine, no daemon or bridge."""

    result = location.software_agent_job.execute_in_process(
        run_config={
            "resources": {
                "software_agent_settings": {
                    "config": {"capability": CAPABILITY, "placeholder": True}
                }
            }
        },
        tags={INTENT_TAG: intent_id, "tactus.slice": "agent-attempt-canary"},
        raise_on_error=False,
    )
    if not result.success:
        raise ValidationError(
            "the in-process Dagster run of the Tactus agent job failed: "
            + json.dumps(_step_failures(result), sort_keys=True)
        )
    return {
        "dagster_run_id": result.run_id,
        "success": True,
        "defined_steps": [node.name for node in location.software_agent_job.graph.node_defs],
        "succeeded_steps": sorted(
            {event.step_key for event in result.get_step_success_events()}
        ),
    }


def _step_failures(result: Any) -> list[dict[str, str]]:
    """Bounded failure facts for the evidence record (never a classification)."""

    failures: list[dict[str, str]] = []
    try:
        for event in result.get_step_failure_events():
            data = event.event_specific_data
            failures.append({"step": event.step_key, "message": data.error.message})
    except Exception:  # pragma: no cover - evidence convenience only
        pass
    return failures


def _durable_chain(
    *,
    args: argparse.Namespace,
    ictus_root: Path,
    work_root: Path,
    agent_workspace: Path,
    agent_state: Path,
    decision: dict[str, Any],
    snapshot: dict[str, Any],
    pinned_runtime: dict[str, Any],
) -> dict[str, Any]:
    """The full chain through the pinned Ictus bridge, once the seam matches."""

    from dagster import DagsterInstance, RunsFilter, __version__ as dagster_version
    from ictus_dagster import submission

    dagster_home, workspace = _write_dagster_home(work_root)
    _configure_dagster(dagster_home, ictus_root, work_root)
    instance = DagsterInstance.get()
    daemon_log = work_root / "dagster-daemon.log"
    daemon, daemon_log_file = slice_harness._start_daemon(  # noqa: SLF001
        ictus_root, workspace, work_root, daemon_log
    )
    ledger_path = work_root / "tactus-ledger.sqlite"
    db = SqliteExecutionAdmission(ledger_path)
    cycle = ExecutionCycle(db)
    try:
        work_order = _work_order()
        db.register_work_order(
            WORK_ORDER_ID,
            revision=WorkOrderRevision(0),
            source_revision=SourceRevision(SOURCE_REVISION),
            state=work_order.state,
            readiness=None,
            at=_now(),
        )
        recovery = cycle.apply_recovery_decision(
            work_order,
            decision,
            state_port=db,
            expected_revision=WorkOrderRevision(0),
            initial_context=InitialAuthorizationContext(
                phase=slice_harness._fact(snapshot, "snapshot.phase"),  # noqa: SLF001
                snapshot_id=snapshot["snapshot_id"],
                snapshot_digest=slice_harness._fact(snapshot, "snapshot.digest"),  # noqa: SLF001
                source_revision=SourceRevision(SOURCE_REVISION),
                work_order_revision=WorkOrderRevision(0),
            ),
        )
        accepted_revision = recovery.work_order_revision
        if accepted_revision is None:
            raise ValidationError("the decision did not persist a revision")
        now = _now()
        claim = db.acquire_claim(
            WORK_ORDER_ID,
            expected_revision=accepted_revision,
            claim_token=ClaimToken("agent-slice-claim"),
            expires_at=now + timedelta(minutes=10),
            now=now,
        )
        accepted = cycle.accept(
            claim,
            decision,
            source_revision=SOURCE_REVISION,
            work_order=work_order,
            at=now,
        )
        accepted_db_revision = db.work_order_snapshot(WORK_ORDER_ID)[0]
        receipt = submission.submit_intent(accepted.intent.as_dict)
        db.mark_dispatched(accepted.submission.submission_id)
        db.attach_receipt(
            accepted.submission.submission_id, DagsterRunId(receipt["receipt_id"])
        )
        status = slice_harness._wait_terminal(  # noqa: SLF001
            submission, receipt, instance=instance, timeout=args.timeout
        )
        result = status["result"]
        if result["observation"]["category"] != "SUCCESS":
            raise ValidationError(
                "the durable agent run did not succeed: "
                + json.dumps(result, sort_keys=True)
            )

        summaries = _attempt_summaries(agent_state)
        if len(summaries) != 1:
            raise ValidationError(
                f"expected exactly one agent attempt, found {len(summaries)}"
            )
        attempt = _assert_attempt_evidence(
            summaries[0],
            intent_id=accepted.intent.intent_id.value,
            pinned=pinned_runtime,
        )
        _assert_patch_applies(work_root, attempt)
        if (agent_workspace / "src" / "example.py").read_text(encoding="utf-8") != UPDATED:
            raise ValidationError("the agent did not change the workspace file")

        observation_payload = slice_harness._observation_from_result(  # noqa: SLF001
            result=result,
            intent_id=accepted.intent.intent_id.value,
            terminal_status=status["state"],
        )
        observation = parse_observation(observation_payload)
        correlated = cycle.correlate(
            observation,
            attempt_id=accepted.attempt.attempt_id,
            submission_id=accepted.submission.submission_id,
            expected_work_order_id=WORK_ORDER_ID,
            expected_intent_digest=accepted.intent.digest,
        )
        applied = cycle.apply_observation(
            correlated,
            work_order=work_order,
            expected_revision=accepted_db_revision,
            at=_now(),
        )
        if not applied.applied:
            raise ValidationError("the success observation was not applied")
        if applied.work_order_state.value != "IMPLEMENTED":
            raise ValidationError("success did not mark the Work Order IMPLEMENTED")
        replay = cycle.apply_observation(correlated)
        if replay.applied:
            raise ValidationError("an identical observation replay applied a second effect")

        runs = instance.get_runs(
            RunsFilter(tags={submission.IDENTITY_TAG: accepted.intent.intent_id.value})
        )
        if len(runs) != 1:
            raise ValidationError(f"expected one Dagster run, found {len(runs)}")

        return {
            "mode": "durable-submission",
            "dagster": {"version": dagster_version, "instance": "persistent (SQLite)"},
            "environment": {
                "dagster_home": str(dagster_home),
                "workspace_yaml": str(workspace),
                "daemon_log": str(daemon_log),
                "ledger": str(ledger_path),
                "code_location": CODE_LOCATION,
                "run_launcher": "DefaultRunLauncher",
                "run_coordinator": "QueuedRunCoordinator",
            },
            "admission": {
                "intent_id": accepted.intent.intent_id.value,
                "intent_digest": accepted.intent.digest,
                "decision_id": accepted.intent.decision_id,
                "attempt_id": accepted.attempt.attempt_id.value,
                "submission_id": accepted.submission.submission_id.value,
                "fence": accepted.attempt.fence,
            },
            "run": {
                "receipt": receipt,
                "terminal_state": status["state"],
                "dagster_run_count": len(runs),
                "dagster_run_ids": [run.run_id for run in runs],
                "ictus_result": result,
            },
            "agent_attempt": attempt,
            "outcome": {
                "applied": applied.applied,
                "replay_applied": replay.applied,
                "work_order_state": applied.work_order_state.value,
            },
        }
    finally:
        try:
            db.close()
        except Exception:
            pass
        slice_harness._terminate_process_tree(daemon)  # noqa: SLF001
        daemon_log_file.close()


#: Untracked entries a pinned checkout may carry without being "dirty": the
#: prebuilt binary and the virtualenv the harness needs. Anything else untracked is
#: a local change to the revision being measured.
UNTRACKED_ALLOWLIST = ("target", ".venv")


def _verify_pinned_checkout(ictus_root: Path, *, allow_dirty: bool) -> dict[str, Any]:
    """Refuse a pinned checkout that carries local changes or extra files.

    ``HEAD`` alone cannot tell the pinned revision from a working tree that was
    edited at that commit, and an *untracked* file matters too (``sitecustomize.py``
    on ``PYTHONPATH`` is imported by the harness itself). Durable-link evidence
    produced from such a checkout would otherwise read as if the pinned revision
    supported it.
    """

    completed = subprocess.run(
        ["git", "-C", str(ictus_root), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        timeout=60.0,
    )
    if completed.returncode != 0:
        raise ValidationError(
            f"cannot inspect the Ictus checkout for local changes: {completed.stderr.strip()}"
        )
    modified: list[str] = []
    untracked: list[str] = []
    unexpected_untracked: list[str] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        if line.startswith("?? "):
            entry = line[3:].strip().rstrip("/")
            untracked.append(entry)
            if entry.split("/", 1)[0] not in UNTRACKED_ALLOWLIST:
                unexpected_untracked.append(entry)
        else:
            modified.append(line)
    changes = modified + unexpected_untracked
    if changes and not allow_dirty:
        raise ValidationError(
            f"the Ictus checkout {ictus_root} has local changes at the pinned commit: "
            + "; ".join(changes[:5])
            + ". Evidence must describe the pinned revision; pass --allow-dirty-ictus "
            "only for a throwaway experiment and expect the record to say so."
        )
    return {
        "dirty": bool(changes),
        "modified_files": modified,
        "unexpected_untracked": unexpected_untracked,
        "untracked_allowed": [
            entry for entry in untracked if entry not in unexpected_untracked
        ],
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    ictus_root = Path(args.ictus_root).expanduser().resolve()
    binary = slice_harness.load_boundary(ictus_root).binary
    pin = slice_harness._verify_ictus_pin(ictus_root, binary)  # noqa: SLF001
    checkout = _verify_pinned_checkout(
        ictus_root, allow_dirty=bool(getattr(args, "allow_dirty_ictus", False))
    )
    pinned_runtime = _shipped_runtime_config()

    base_tmp = Path("/tmp") if Path("/tmp").is_dir() else None
    work_root = Path(
        tempfile.mkdtemp(
            prefix="tactus-agent-", dir=str(base_tmp) if base_tmp is not None else None
        )
    )
    agent_workspace, agent_state = _prepare_agent_root(work_root)
    _configure_agent_environment(work_root, agent_workspace, agent_state)

    ictus_package = _use_pinned_ictus_package(ictus_root)

    # 1. A real pinned-Ictus decision for the agent capability.
    snapshot = _initial_snapshot(_work_order(), "snapshot-agent-entry")
    payload, decision = slice_harness._decide(binary, snapshot)  # noqa: SLF001
    if decision["outcome"] != "EXECUTABLE":
        raise ValidationError("Ictus agent outcome was not EXECUTABLE")
    if decision["policy"]["verdict"] != "ALLOW":
        raise ValidationError(
            "Ictus did not authorize the agent capability: "
            + json.dumps(decision, sort_keys=True)
        )
    intent = decision.get("intent")
    if intent is None:
        raise ValidationError(f"Ictus returned no ExecutionIntent for {CAPABILITY}")
    if intent["capability"] != CAPABILITY:
        raise ValidationError(
            f"Ictus routed capability {intent['capability']!r}, not {CAPABILITY!r}"
        )

    # 2. Probe the seam before anything is submitted.
    location = _load_location()
    seam = _seam(location)

    # 3. Prove what the current pin allows.
    if seam["status"] == "matched":
        chain = _durable_chain(
            args=args,
            ictus_root=ictus_root,
            work_root=work_root,
            agent_workspace=agent_workspace,
            agent_state=agent_state,
            decision=decision,
            snapshot=snapshot,
            pinned_runtime=pinned_runtime,
        )
    else:
        ran = _run_in_process(location, intent["intent_id"])
        summaries = _attempt_summaries(agent_state)
        if len(summaries) != 1:
            raise ValidationError(
                f"expected exactly one agent attempt, found {len(summaries)}"
            )
        attempt = _assert_attempt_evidence(
            summaries[0], intent_id=intent["intent_id"], pinned=pinned_runtime
        )
        _assert_patch_applies(work_root, attempt)
        if (agent_workspace / "src" / "example.py").read_text(encoding="utf-8") != UPDATED:
            raise ValidationError("the agent did not change the workspace file")
        chain = {
            "mode": "in-process",
            "dagster": {"executor": "in-process", "instance": "ephemeral"},
            "environment": {
                "code_location": CODE_LOCATION,
                "code_location_loaded_by": "python_file",
            },
            "admission": {"intent_id": intent["intent_id"], "intent_digest": None},
            "run": ran,
            "agent_attempt": attempt,
            "outcome": {
                "applied": False,
                "note": (
                    "Tactus applies the terminal ExecutionResult through the pinned "
                    "bridge, which is exactly the blocked link; the apply path itself "
                    "is proven by scripts/run-execution-slice.sh"
                ),
            },
        }

    evidence: dict[str, Any] = {
        "schema_version": 1,
        "kind": "tactus.ictus.dagster.agent_slice",
        "generated_at": _now().isoformat(),
        "result": "passed",
        "mode": chain["mode"],
        "versions": {
            "python": sys.version,
            "tactus_contract_pin": ICTUS_COMMIT,
            "ictus": pin,
            "ictus_checkout": checkout,
            "ictus_dagster_package": ictus_package,
            "agent_runtime_pin": pinned_runtime,
        },
        "environment": {
            "work_root": str(work_root),
            "agent_workspace": str(agent_workspace),
            "agent_state": str(agent_state),
            **chain["environment"],
        },
        "work_order": {
            "id": WORK_ORDER_ID,
            "source_revision": SOURCE_REVISION,
            "state_after_decision": "OPEN+READY",
        },
        "decision": {
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_digest": slice_harness._fact(snapshot, "snapshot.digest"),  # noqa: SLF001
            "context": payload,
            "outcome": decision["outcome"],
            "policy": decision["policy"],
            "execution_intent": intent,
        },
        "seam": seam,
        "blocked_dependencies": []
        if seam["status"] == "matched"
        else [seam["blocked_dependency"]],
        "dagster": chain["dagster"],
        "admission": chain["admission"],
        "run": chain["run"],
        "agent_attempt": chain["agent_attempt"],
        "patch": {
            "applies_to_original_workspace": True,
            "sha256": chain["agent_attempt"]["artifacts"]["patch_sha256"],
            "bytes": chain["agent_attempt"]["artifacts"]["patch_bytes"],
            "changed_paths": chain["agent_attempt"]["artifacts"]["changed_paths"],
        },
        "outcome": chain["outcome"],
        "claims": {
            "real_coding_agent": False,
            "runtime_kind": pinned_runtime["runtime_kind"],
            "publication_authority": False,
            "os_isolation": "none",
            "remaining_gates": [
                "Issue #22 AC3: filesystem/network/credential containment (gate B2)",
                "Issue #22 AC1: a real provider/model pin and an approved canary",
                "Issue #23: authorized branch/PR publication with a live transport",
            ],
        },
    }
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ictus-root",
        default=os.environ.get("TACTUS_ICTUS_ROOT"),
        help="Path to the pinned Ictus checkout (or TACTUS_ICTUS_ROOT).",
    )
    parser.add_argument(
        "--evidence",
        default="/tmp/tactus-agent-slice-evidence.json",
        help="Where to write the JSON evidence record.",
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--allow-dirty-ictus",
        action="store_true",
        help=(
            "Permit evidence from an Ictus checkout with local changes at the pinned "
            "commit (throwaway B4 experiments only; the record then says dirty: true)."
        ),
    )
    args = parser.parse_args(argv)
    if not args.ictus_root:
        parser.error("--ictus-root or TACTUS_ICTUS_ROOT is required")

    started = time.monotonic()
    try:
        evidence = run(args)
    except Exception as exc:
        print(f"agent-slice validation failed: {exc}", file=sys.stderr)
        return 1

    evidence["duration_seconds"] = round(time.monotonic() - started, 3)
    evidence_path = Path(args.evidence)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps(slice_harness._jsonable(evidence), indent=2, sort_keys=True) + "\n"  # noqa: SLF001
    )
    attempt = evidence["agent_attempt"]
    seam = evidence["seam"]
    print(
        "agent-slice validation passed: "
        f"mode={evidence['mode']} "
        f"outcome={evidence['decision']['outcome']} "
        f"verdict={evidence['decision']['policy']['verdict']} "
        f"intent={evidence['admission']['intent_id']} "
        f"attempt={attempt['attempt_id']} "
        f"runtime={attempt['runtime']['runtime_id']} ({attempt['runtime']['runtime_kind']}) "
        f"patch={attempt['artifacts']['patch_sha256'][:12]} "
        f"seam={seam['status']} "
        f"evidence={evidence_path}"
    )
    if seam["status"] != "matched":
        print("blocked link: " + str(seam["blocked_dependency"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
