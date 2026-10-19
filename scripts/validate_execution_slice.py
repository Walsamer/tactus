#!/usr/bin/env python3
"""Validate one real Tactus/Ictus/Dagster execution slice.

This is a bounded evidence harness, not production Tactus runtime wiring. It
uses the pinned Ictus bridge as the runtime adapter, a real SQLite-backed
``DagsterInstance`` with the queued coordinator and an independent daemon, and
Tactus's durable admission + observation application seams.

The only runtime-adapter-glue code here is the small wrapper that copies the
Ictus-produced ``category`` and evidence from a terminal Dagster status into an
``ExecutionObservation`` v1 envelope. The pinned Ictus Python bridge does not
currently expose its Rust ``ExecutionResult::to_observation`` method to Python;
that upstream gap is documented in
``docs/architecture/END_TO_END_EXECUTION_VALIDATION.md``. The wrapper never
classifies a failure, changes a category, retries a step or makes policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tactus.application import (  # noqa: E402
    ExecutionCycle,
    ObservationCorrelationError,
)
from tactus.domain import (  # noqa: E402
    ClaimToken,
    InitialAuthorizationContext,
    DagsterRunId,
    ExecutionAttempt,
    ExecutionAttemptId,
    ExecutionIntentId,
    OpenStatus,
    SourceRevision,
    TransitionAuthority,
    WorkOrder,
    WorkOrderRevision,
)
from tactus.integrations.ictus import (  # noqa: E402
    ICTUS_COMMIT,
    ObservationContractError,
    SemanticBudget,
    SnapshotPhase,
    StepRetryDiagnostic,
    build_state_snapshot,
    parse_observation,
    to_ictus_legacy_boundary_payload,
)
from tactus.persistence.sqlite import SqliteExecutionAdmission  # noqa: E402


WORK_ORDER_ID = "WO-E2E-EXECUTION-SLICE"
SOURCE_REVISION = "source:work-order:v1"
CAPABILITY = "demo.verify"
CODE_LOCATION = "ictus_dagster"


class ValidationError(RuntimeError):
    """The bounded validation could not establish the required evidence."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _tail(path: Path, limit: int = 80) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(errors="replace").splitlines()
    return "\n".join(lines[-limit:])


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.wait(timeout=15)
    except Exception:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except Exception:
            pass


def _verify_ictus_pin(ictus_root: Path, binary: Path) -> dict[str, Any]:
    if not binary.is_file():
        raise ValidationError(f"pinned Ictus binary is missing: {binary}")
    try:
        commit = subprocess.check_output(
            ["git", "-C", str(ictus_root), "rev-parse", "HEAD"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValidationError(f"cannot determine Ictus commit: {exc}") from exc
    if commit != ICTUS_COMMIT:
        raise ValidationError(
            "Ictus checkout does not match the pinned contract commit: "
            f"checkout={commit} pin={ICTUS_COMMIT}"
        )
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    return {
        "root": str(ictus_root),
        "commit": commit,
        "contract_pin": ICTUS_COMMIT,
        "binary": str(binary),
        "binary_sha256": digest,
    }


def _decide(
    binary: Path,
    snapshot: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Invoke the real pinned Ictus CLI with a versioned snapshot on stdin."""

    payload = to_ictus_legacy_boundary_payload(snapshot)
    completed = subprocess.run(
        [str(binary), "decide", "--approve"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
    )
    if completed.returncode not in {0, 3}:
        raise ValidationError(
            "Ictus decide failed: "
            f"exit={completed.returncode} stderr={completed.stderr.strip()}"
        )
    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Ictus decide returned invalid JSON: {exc}") from exc
    return payload, envelope


def _write_dagster_home(work_root: Path, ictus_root: Path) -> tuple[Path, Path]:
    dagster_home = work_root / "dagster_home"
    dagster_home.mkdir(parents=True, exist_ok=True)
    (dagster_home / "dagster.yaml").write_text(
        """run_coordinator:
  module: dagster.core.run_coordinator
  class: QueuedRunCoordinator
run_launcher:
  module: dagster.core.launcher
  class: DefaultRunLauncher
"""
    )
    workspace = work_root / "workspace.yaml"
    workspace.write_text(
        f"""load_from:
  - python_module:
      module_name: ictus_dagster.definitions
      location_name: {CODE_LOCATION}
      working_directory: {ictus_root / 'python'}
"""
    )
    return dagster_home, workspace


def _configure_environment(work_root: Path, dagster_home: Path, ictus_root: Path) -> None:
    os.environ["DAGSTER_HOME"] = str(dagster_home)
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [str(ictus_root / "python"), str(SRC_ROOT)]
    )
    os.environ["ICTUS_DAGSTER_CODE_LOCATION"] = CODE_LOCATION
    os.environ["ICTUS_DAGSTER_REPOSITORY"] = "__repository__"
    # Keep Dagster's gRPC unix sockets well below the 103-character limit.
    os.environ["TMPDIR"] = str(work_root)
    os.environ["TMP"] = str(work_root)
    os.environ["TEMP"] = str(work_root)
    sys.path.insert(0, str(ictus_root / "python"))


def _start_daemon(
    ictus_root: Path, workspace: Path, work_root: Path, log_path: Path
) -> tuple[subprocess.Popen[Any], Any]:
    daemon = ictus_root / ".venv" / "bin" / "dagster-daemon"
    if not daemon.is_file():
        raise ValidationError(f"dagster-daemon is missing from the Ictus venv: {daemon}")
    log_file = log_path.open("w")
    process = subprocess.Popen(
        [str(daemon), "run", "-w", str(workspace), "--log-level", "info"],
        cwd=str(work_root),
        env=os.environ.copy(),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    time.sleep(6)
    if process.poll() is not None:
        log_file.close()
        raise ValidationError(
            "dagster-daemon exited during startup:\n" + _tail(log_path)
        )
    return process, log_file


def _bootstrap_work_order(
    db: SqliteExecutionAdmission,
) -> tuple[WorkOrder, WorkOrderRevision]:
    now = _now()
    work_order = WorkOrder.create(WORK_ORDER_ID)
    work_order.admit(
        reason="bounded execution-slice validation",
        authority=TransitionAuthority.OPERATOR,
        at=now,
    )
    work_order.set_readiness(OpenStatus.READY)
    work_order.claim(
        reason="simulated recovered Work Order entering the validation slice",
        at=now,
    )
    db.register_work_order(
        WORK_ORDER_ID,
        revision=WorkOrderRevision(0),
        source_revision=SourceRevision(SOURCE_REVISION),
        state=work_order.state,
        readiness=None,
        at=now,
    )
    return work_order, WorkOrderRevision(0)


def _bootstrap_snapshot(
    work_order: WorkOrder,
    *,
    semantic_budget: SemanticBudget,
    step_retry: StepRetryDiagnostic,
    snapshot_id: str,
) -> dict[str, Any]:
    now = _now()
    observation = parse_observation(
        {
            "schema_version": 1,
            "observation_id": f"bootstrap-observation:{snapshot_id}",
            "execution_id": "bootstrap-run",
            "intent_id": "bootstrap-intent",
            "category": "WORKER_TIMEOUT",
            "observed_at": now.isoformat(),
            "message": "bounded validation bootstrap context",
            "evidence": [
                {
                    "kind": "fixture",
                    "uri": "tactus://validation/bootstrap-context",
                    "note": "not a real execution observation",
                }
            ],
        }
    )
    attempt = ExecutionAttempt(
        attempt_id=ExecutionAttemptId("bootstrap-attempt"),
        work_order_id=work_order.id,
        intent_id=ExecutionIntentId("bootstrap-intent"),
        dagster_run_id=DagsterRunId("bootstrap-run"),
        accepted_at=now,
    )
    return build_state_snapshot(
        work_order=work_order,
        phase=SnapshotPhase.INITIAL,
        semantic_budget=semantic_budget,
        step_retry=step_retry,
        snapshot_id=snapshot_id,
        timestamp=now,
        capability_id=CAPABILITY,
        dependencies_satisfied=True,
        source_revision=SOURCE_REVISION,
        work_order_revision=0,
    )


def _unused_bootstrap_snapshot(
    work_order: WorkOrder,
    *,
    semantic_budget: SemanticBudget,
    step_retry: StepRetryDiagnostic,
    snapshot_id: str,
) -> dict[str, Any]:
    now = _now()
    observation = parse_observation(
        {
            "schema_version": 1,
            "observation_id": "obs-bootstrap",
            "execution_id": "bootstrap-run",
            "intent_id": "bootstrap-intent",
            "category": "PROCESS_CRASH",
            "observed_at": now.isoformat(),
            "message": "bootstrap",
            "evidence": [{"kind": "dagster_run", "uri": "dagster://runs/bootstrap"}],
        }
    )
    attempt = ExecutionAttempt(
        attempt_id=ExecutionAttemptId("bootstrap-attempt"),
        work_order_id=work_order.id,
        intent_id=ExecutionIntentId("bootstrap-intent"),
        dagster_run_id=DagsterRunId("bootstrap-run"),
        accepted_at=now,
    )
    return build_state_snapshot(
        work_order=work_order,
        phase=SnapshotPhase.RECOVERY,
        observation=observation,
        attempt=attempt,
        semantic_budget=semantic_budget,
        step_retry=step_retry,
        snapshot_id=snapshot_id,
        timestamp=now,
        capability_id=CAPABILITY,
        dependencies_satisfied=True,
        source_revision=SOURCE_REVISION,
        work_order_revision=0,
    )


def _observation_from_result(
    *,
    result: dict[str, Any],
    intent_id: str,
    terminal_status: str,
) -> dict[str, Any]:
    """Wrap the Ictus-produced result in the strict v1 observation envelope.

    This is the documented upstream-gap shim: category and evidence are copied
    verbatim from the pinned Ictus result; no classification or policy happens.
    """

    category = result.get("observation", {}).get("category")
    if not isinstance(category, str):
        raise ValidationError("Ictus result carries no observation.category")
    execution_id = result.get("execution_id")
    if not isinstance(execution_id, str) or not execution_id:
        raise ValidationError("Ictus result carries no execution_id")
    return {
        "schema_version": 1,
        "observation_id": f"ictus-result:{execution_id}",
        "execution_id": execution_id,
        "intent_id": intent_id,
        "category": category,
        "observed_at": (result.get("finished_at") or _now().isoformat()),
        "message": f"Ictus Dagster result status={result.get('status')} run={terminal_status}",
        "evidence": result.get("evidence", []),
    }


def _wait_terminal(
    submission_module: Any,
    receipt: dict[str, Any],
    *,
    instance: Any,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = submission_module.query_receipt(receipt, instance=instance)
        if last.get("state") == "terminal":
            return last
        time.sleep(1)
    raise ValidationError(f"Dagster run did not reach terminal state; last={last}")


def _assert_fail_closed(
    cycle: ExecutionCycle,
    accepted: Any,
    observation_payload: dict[str, Any],
) -> list[dict[str, str]]:
    """Prove the production boundary rejects bad receipts and mismatches."""

    checks: list[dict[str, str]] = []

    unknown_category = dict(observation_payload)
    unknown_category["category"] = "NOT_AN_ICTUS_CATEGORY"
    try:
        parse_observation(unknown_category)
    except ObservationContractError as exc:
        checks.append({
            "case": "unknown_receipt_category",
            "denied": type(exc).__name__,
        })
    else:
        raise ValidationError("an unknown receipt category was accepted")

    malformed = dict(observation_payload)
    malformed.pop("execution_id", None)
    try:
        parse_observation(malformed)
    except ObservationContractError as exc:
        checks.append({"case": "malformed_receipt", "denied": type(exc).__name__})
    else:
        raise ValidationError("a malformed receipt was accepted")

    substitutions: tuple[tuple[str, dict[str, str], dict[str, Any]], ...] = (
        ("execution_id", {"execution_id": "dagster-run-mismatch"}, {}),
        ("intent_id", {"intent_id": "intent-mismatch"}, {}),
        (
            "expected_work_order_id",
            {},
            {"expected_work_order_id": "WO-MISMATCH"},
        ),
        ("expected_intent_digest", {}, {"expected_intent_digest": "0" * 64}),
    )
    for label, payload_overrides, keyword_overrides in substitutions:
        payload = {**observation_payload, **payload_overrides}
        try:
            cycle.correlate(
                payload,
                attempt_id=accepted.attempt.attempt_id,
                submission_id=accepted.submission.submission_id,
                **keyword_overrides,
            )
        except ObservationCorrelationError as exc:
            checks.append({
                "case": f"correlation_{label}",
                "denied": type(exc).__name__,
            })
        else:
            raise ValidationError(f"a mismatched {label} was correlated")
    return checks


def _counts(db: SqliteExecutionAdmission) -> dict[str, int]:
    connection = db._connection  # noqa: SLF001 - harness evidence only
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("work_orders", "execution_attempts", "submissions", "result_inbox")
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    ictus_root = Path(args.ictus_root).expanduser().resolve()
    binary = ictus_root / "target" / "debug" / "ictus"
    pin = _verify_ictus_pin(ictus_root, binary)

    base_tmp = Path("/tmp") if Path("/tmp").is_dir() else None
    work_root = Path(
        tempfile.mkdtemp(
            prefix="tactus-e2e-", dir=str(base_tmp) if base_tmp is not None else None
        )
    )
    dagster_home, workspace = _write_dagster_home(work_root, ictus_root)
    _configure_environment(work_root, dagster_home, ictus_root)

    # Import only after DAGSTER_HOME / PYTHONPATH / code-location env are set.
    from dagster import DagsterInstance, RunsFilter, __version__ as dagster_version
    from ictus_dagster import submission

    # Materialize and migrate the persistent storage before the daemon starts.
    # Letting the daemon and the caller initialize a brand-new SQLite instance
    # concurrently can record more than one Alembic head and wedge both
    # processes; the daemon then opens the already-initialized instance.
    instance = DagsterInstance.get()

    daemon_log = work_root / "dagster-daemon.log"
    daemon, daemon_log_file = _start_daemon(ictus_root, workspace, work_root, daemon_log)
    db_path = work_root / "tactus-ledger.sqlite"
    db = SqliteExecutionAdmission(db_path)
    cycle = ExecutionCycle(db)

    try:
        work_order, starting_revision = _bootstrap_work_order(db)

        # 1. A real pinned-Ictus decision (ALLOW + ExecutionIntent). The
        #    bootstrap snapshot describes pre-slice recovery context; the
        #    decision itself is genuinely computed by Ictus.
        bootstrap = _bootstrap_snapshot(
            work_order,
            semantic_budget=SemanticBudget(1, 3),
            step_retry=StepRetryDiagnostic(2),
            snapshot_id="snapshot-entry",
        )
        bootstrap_payload, bootstrap_decision = _decide(binary, bootstrap)
        if bootstrap_decision["policy_decision"]["decision"] != "ALLOW":
            raise ValidationError("Ictus entry decision was not ALLOW")
        if bootstrap_decision.get("execution_intent") is None:
            raise ValidationError("Ictus entry decision carried no ExecutionIntent")

        # 2. Apply the real recovery decision, persist the projection, then
        #    durably accept the exact Ictus intent.
        recovery = cycle.apply_recovery_decision(
            work_order,
            bootstrap_decision,
            state_port=db,
            expected_revision=starting_revision,
            initial_context=InitialAuthorizationContext(
                phase=bootstrap["phase"],
                snapshot_id=bootstrap["snapshot_id"],
                snapshot_digest=bootstrap["digest"],
                source_revision=SourceRevision(SOURCE_REVISION),
                work_order_revision=starting_revision,
            ),
        )
        accepted_revision = recovery.work_order_revision
        if accepted_revision is None:
            raise ValidationError("recovery decision did not persist a revision")
        now = _now()
        claim = db.acquire_claim(
            WORK_ORDER_ID,
            expected_revision=accepted_revision,
            claim_token=ClaimToken("e2e-claim"),
            expires_at=now + timedelta(minutes=10),
            now=now,
        )
        accepted = cycle.accept(
            claim,
            bootstrap_decision,
            source_revision=SOURCE_REVISION,
            work_order=work_order,
            at=now,
        )
        if (
            accepted.intent.intent_id.value
            != bootstrap_decision["execution_intent"]["intent_id"]
        ):
            raise ValidationError("durable intent identity diverged from the Ictus intent")
        accepted_db_revision = db.work_order_snapshot(WORK_ORDER_ID)[0]

        # 3. Submit via the pinned Ictus Dagster bridge. Then simulate a Tactus
        #    crash after send/before receipt: close the ledger, reopen it and
        #    reconcile the pending submission by resending the same intent.
        first_receipt = submission.submit_intent(accepted.intent.as_dict)
        db.mark_dispatched(accepted.submission.submission_id)
        db.close()

        db = SqliteExecutionAdmission(db_path)
        cycle = ExecutionCycle(db)
        pending = db.pending_submissions()
        if (
            len(pending) != 1
            or pending[0].submission_id != accepted.submission.submission_id
        ):
            raise ValidationError(
                "crash recovery did not find exactly the original pending submission"
            )
        reconciled_receipt = submission.submit_intent(accepted.intent.as_dict)
        if reconciled_receipt["receipt_id"] != first_receipt["receipt_id"]:
            raise ValidationError("resend created a different Dagster run")
        db.attach_receipt(
            accepted.submission.submission_id,
            DagsterRunId(reconciled_receipt["receipt_id"]),
        )

        status = _wait_terminal(
            submission,
            reconciled_receipt,
            instance=instance,
            timeout=args.timeout,
        )
        result = status["result"]
        if result["observation"]["category"] != "SUCCESS":
            raise ValidationError(
                f"validation run did not succeed: {json.dumps(result, sort_keys=True)}"
            )

        # 4. Strictly parse the runtime receipt as an ExecutionObservation,
        #    correlate subject/intent/attempt/run, then apply one outcome.
        observation_payload = _observation_from_result(
            result=result,
            intent_id=accepted.intent.intent_id.value,
            terminal_status=status["state"],
        )
        observation = parse_observation(observation_payload)
        fail_closed_checks = _assert_fail_closed(
            cycle, accepted, observation_payload
        )
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
            raise ValidationError("success observation was not applied")
        replay = cycle.apply_observation(correlated)
        if replay.applied:
            raise ValidationError("identical observation replay applied a second effect")
        if applied.work_order_state.value != "IMPLEMENTED":
            raise ValidationError("success did not mark the Work Order IMPLEMENTED")
        if db.attempt(accepted.attempt.attempt_id).closed_at is None:
            raise ValidationError("success did not close the execution attempt")

        # 5. Evidence that exactly one semantic attempt/run/result exists.
        runs = instance.get_runs(
            RunsFilter(tags={submission.IDENTITY_TAG: accepted.intent.intent_id.value})
        )
        if len(runs) != 1:
            raise ValidationError(f"expected one Dagster run, found {len(runs)}")
        counts = _counts(db)
        if counts["execution_attempts"] != 1 or counts["submissions"] != 1:
            raise ValidationError(f"unexpected durable identity counts: {counts}")
        if counts["result_inbox"] != 1:
            raise ValidationError(f"unexpected result count: {counts}")

        # 6. Real Ictus policy-boundary checks using the canonical semantic
        #    count/limit and the diagnostic-only step retry index.
        boundary_checks: list[dict[str, Any]] = []
        for label, budget, step_retry in (
            (
                "within_budget_step_retry_99",
                SemanticBudget(1, 3),
                StepRetryDiagnostic(99),
            ),
            ("exhausted_budget", SemanticBudget(3, 3), StepRetryDiagnostic(0)),
        ):
            snapshot = _bootstrap_snapshot(
                work_order,
                semantic_budget=budget,
                step_retry=step_retry,
                snapshot_id=f"snapshot-{label}",
            )
            _, envelope = _decide(binary, snapshot)
            boundary_checks.append(
                {
                    "label": label,
                    "semantic_attempts": budget.semantic_attempts,
                    "max_semantic_attempts": budget.max_semantic_attempts,
                    "step_retry_index": step_retry.index,
                    "proposal_decision": envelope["proposal"]["decision"],
                    "policy_decision": envelope["policy_decision"]["decision"],
                    "execution_intent_present": envelope.get("execution_intent")
                    is not None,
                }
            )
        if boundary_checks[0]["proposal_decision"] != "REEXECUTE":
            raise ValidationError("large step-retry index changed the semantic decision")
        if boundary_checks[1]["proposal_decision"] != "ESCALATE":
            raise ValidationError("exhausted semantic budget was not escalated")

        evidence = {
            "schema_version": 1,
            "kind": "tactus.ictus.dagster.execution_slice",
            "generated_at": _now().isoformat(),
            "result": "passed",
            "versions": {
                "python": sys.version,
                "tactus_contract_pin": ICTUS_COMMIT,
                "dagster": dagster_version,
                "ictus": pin,
            },
            "environment": {
                "work_root": str(work_root),
                "dagster_home": str(dagster_home),
                "workspace": str(workspace),
                "ledger": str(db_path),
                "daemon_log": str(daemon_log),
                "run_launcher": "DefaultRunLauncher",
                "run_coordinator": "QueuedRunCoordinator",
                "code_location": CODE_LOCATION,
                "code_location_env": os.environ["ICTUS_DAGSTER_CODE_LOCATION"],
            },
            "work_order": {
                "id": WORK_ORDER_ID,
                "source_revision": SOURCE_REVISION,
                "state_after_decision": "OPEN+READY",
                "state_after_outcome": applied.work_order_state.value,
                "revision_after_decision": accepted_revision.value,
                "revision_after_accept": accepted_db_revision.value,
                "revision_after_outcome": applied.work_order_revision.value,
            },
            "decision": {
                "snapshot": bootstrap,
                "legacy_boundary_payload": bootstrap_payload,
                "envelope": bootstrap_decision,
            },
            "admission": {
                "intent_id": accepted.intent.intent_id.value,
                "intent_digest": accepted.intent.digest,
                "decision_id": accepted.intent.decision_id,
                "attempt_id": accepted.attempt.attempt_id.value,
                "submission_id": accepted.submission.submission_id.value,
                "source_revision": accepted.attempt.source_revision.value,
                "fence": accepted.attempt.fence,
            },
            "mid_flight_recovery": {
                "first_receipt": first_receipt,
                "pending_after_reopen": len(pending),
                "pending_submission_id": pending[0].submission_id.value,
                "reconciled_receipt": reconciled_receipt,
                "same_run": first_receipt["receipt_id"]
                == reconciled_receipt["receipt_id"],
                "dagster_run_count": len(runs),
                "dagster_run_ids": [run.run_id for run in runs],
            },
            "observation": observation_payload,
            "fail_closed_checks": fail_closed_checks,
            "correlation": {
                "execution_id": correlated.observation.execution_id,
                "intent_id": correlated.observation.intent_id,
                "attempt_id": correlated.attempt.attempt_id.value,
                "submission_id": correlated.submission.submission_id.value,
                "intent_digest": correlated.intent_digest,
            },
            "outcome": {
                "terminal_status": status["state"],
                "ictus_result": result,
                "applied": applied.applied,
                "replay_applied": replay.applied,
                "attempt_closed": db.attempt(
                    accepted.attempt.attempt_id
                ).closed_at
                is not None,
                "work_order_state": applied.work_order_state.value,
                "work_order_revision": applied.work_order_revision.value,
                "durable_counts": counts,
            },
            "semantic_boundary_checks": boundary_checks,
        }
        return evidence
    finally:
        try:
            db.close()
        except Exception:
            pass
        _terminate_process_tree(daemon)
        daemon_log_file.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ictus-root",
        default=os.environ.get("TACTUS_ICTUS_ROOT"),
        help="Path to the pinned Ictus checkout (or TACTUS_ICTUS_ROOT).",
    )
    parser.add_argument(
        "--evidence",
        default="/tmp/tactus-execution-slice-evidence.json",
        help="Where to write the JSON evidence record.",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    if not args.ictus_root:
        parser.error("--ictus-root or TACTUS_ICTUS_ROOT is required")

    try:
        evidence = run(args)
    except Exception as exc:
        print(f"execution-slice validation failed: {exc}", file=sys.stderr)
        return 1

    evidence_path = Path(args.evidence)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps(_jsonable(evidence), indent=2, sort_keys=True) + "\n"
    )
    print(
        "execution-slice validation passed: "
        f"run={evidence['mid_flight_recovery']['dagster_run_ids'][0]} "
        f"attempt={evidence['admission']['attempt_id']} "
        f"evidence={evidence_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
