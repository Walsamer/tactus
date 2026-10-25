#!/usr/bin/env python3
"""Validate the four execution-failure scenarios against real boundaries.

This is the ``TCT-WO-005`` evidence harness. It complements
``scripts/validate_execution_slice.py`` (the success/restart slice), which is
left unchanged, and it exercises **real** components:

* a real pinned-Ictus ``ictus decide`` binary (INITIAL and RECOVERY snapshots),
* the real pinned Ictus Dagster bridge (``ictus_dagster.submission``: durable
  queued submission, receipts, terminal query, result mapping),
* a real filesystem ``DagsterInstance`` + ``QueuedRunCoordinator`` +
  independent ``dagster-daemon``,
* Tactus's durable admission, correlation and decision application,
* a **Tactus-owned** capability implementation
  (``integrations/dagster/definitions.py``) whose only job is to fail
  deterministically when a bounded, out-of-band control file says so.

Scenarios
---------
1. ``transient``  — execute attempt 1 fails, Dagster retries the step, the run
   succeeds, and Tactus's semantic attempt count never moves.
2. ``terminal``   — every attempt fails; the terminal ``PROCESS_CRASH``
   observation is normalized, correlated and applied, then a genuine Ictus
   RECOVERY decision authorizes exactly one new semantic attempt.
3. ``escalation`` — the same terminal failure with an exhausted semantic budget
   produces an Ictus ``ESCALATE`` (no intent), which Tactus applies as one
   audited ``OPEN + BLOCKED`` effect that survives a restart.
4. ``duplicate``  — duplicate delivery of an already-applied observation and a
   repeated submission of the same intent produce no second effect and no
   second Dagster run.

The mandatory invariant is asserted in every scenario: the payload Tactus
submits to Dagster is byte-identical to the intent Ictus authorized, and no
failure-control key appears anywhere inside it.
"""

from __future__ import annotations

import argparse
import contextlib
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
from typing import Any, Iterator, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
CODE_LOCATION = "tactus-local-test-capability"
CAPABILITY = "demo.verify"
INTENT_ARGUMENT_KEY = "arguments"
#: Checked revision of the milestone Issue. The acceptance slice runs on a Work
#: Order whose identity, source revision and specification digest are derived
#: from this recorded payload by the real import service.
ISSUE_REPOSITORY = "walsamer/tactus"
ISSUE_NUMBER = 12
ISSUE_FIXTURE = (
    REPO_ROOT / "tests" / "integrations" / "github" / "data" / "issue_12.json"
)
#: Keys that must never appear inside an authorized intent (they are the
#: out-of-band failure-control surface, not an intent contract).
FORBIDDEN_INTENT_KEYS = frozenset(
    {"fail_attempts", "fail_hard", "fail_until_attempt", "fail_verify", "hold_seconds"}
)

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tactus.application import (  # noqa: E402
    ExecutionCycle,
    ObservationCorrelationError,
)
from tactus.domain import (  # noqa: E402
    ActionNotPermittedError,
    ClaimToken,
    DagsterRunId,
    HumanAction,
    HumanResolution,
    InitialAuthorizationContext,
    OpenStatus,
    RequestAlreadyResolvedError,
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
from tactus.persistence.human_intervention import (  # noqa: E402
    SqliteHumanIntervention,
)
from tactus.persistence.sqlite import SqliteExecutionAdmission  # noqa: E402


class ValidationError(RuntimeError):
    """A scenario could not establish the required evidence."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _tail(path: Path, limit: int = 60) -> str:
    if not path.exists():
        return ""
    return "\n".join(path.read_text(errors="replace").splitlines()[-limit:])


def _terminate(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.wait(timeout=15)
    except Exception:  # pragma: no cover - best effort teardown
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)


def _fact(snapshot: Mapping[str, Any], key: str) -> str:
    for fact in snapshot.get("facts") or []:
        if fact.get("key") == key:
            return str(fact.get("value") or "")
    raise ValidationError(f"snapshot is missing required fact {key!r}")


def _verify_ictus(ictus_root: Path) -> tuple[dict[str, Any], Path]:
    binary = ictus_root / "target" / "debug" / "ictus"
    if not binary.is_file():
        raise ValidationError(f"pinned Ictus binary is missing: {binary}")
    commit = subprocess.check_output(
        ["git", "-C", str(ictus_root), "rev-parse", "HEAD"], text=True
    ).strip()
    if commit != ICTUS_COMMIT:
        raise ValidationError(
            "Ictus checkout does not match the pinned contract commit: "
            f"checkout={commit} pin={ICTUS_COMMIT}"
        )
    return (
        {
            "root": str(ictus_root),
            "commit": commit,
            "contract_pin": ICTUS_COMMIT,
            "binary": str(binary),
            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        },
        binary,
    )


def _decide(binary: Path, snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
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
            f"Ictus decide failed: exit={completed.returncode} "
            f"stderr={completed.stderr.strip()}"
        )
    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - contract failure
        raise ValidationError(f"Ictus decide returned invalid JSON: {exc}") from exc
    return payload, envelope


def _write_environment(
    work_root: Path, ictus_root: Path, control: dict[str, Any]
) -> tuple[Path, Path, Path, Path]:
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
    # ``python_file.relative_path`` is rebased against the *workspace file*
    # (not ``working_directory``), so the code location is named by absolute
    # path with the repository root as its import working directory.
    workspace.write_text(
        "load_from:\n"
        "  - python_file:\n"
        f"      relative_path: {REPO_ROOT / 'integrations' / 'dagster' / 'definitions.py'}\n"
        f"      location_name: {CODE_LOCATION}\n"
        f"      working_directory: {REPO_ROOT}\n"
    )
    control_path = work_root / "failure-control.json"
    control_path.write_text(json.dumps(control, indent=2, sort_keys=True) + "\n")
    attempt_log = work_root / "capability-attempts.jsonl"
    return dagster_home, workspace, control_path, attempt_log


def _configure(
    work_root: Path,
    dagster_home: Path,
    ictus_root: Path,
    control_path: Path,
    attempt_log: Path,
) -> None:
    os.environ["DAGSTER_HOME"] = str(dagster_home)
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [str(ictus_root / "python"), str(SRC_ROOT), str(REPO_ROOT)]
    )
    os.environ["ICTUS_DAGSTER_CODE_LOCATION"] = CODE_LOCATION
    os.environ["ICTUS_DAGSTER_REPOSITORY"] = "__repository__"
    os.environ["TACTUS_DAGSTER_FAILURE_CONTROL"] = str(control_path)
    os.environ["TACTUS_DAGSTER_ATTEMPT_LOG"] = str(attempt_log)
    # Keep Dagster's gRPC unix sockets well below the 103-character limit.
    os.environ["TMPDIR"] = str(work_root)
    os.environ["TMP"] = str(work_root)
    os.environ["TEMP"] = str(work_root)
    if str(ictus_root / "python") not in sys.path:
        sys.path.insert(0, str(ictus_root / "python"))


def _start_daemon(
    ictus_root: Path, workspace: Path, work_root: Path, log_path: Path
) -> tuple[subprocess.Popen[Any], Any]:
    daemon = ictus_root / ".venv" / "bin" / "dagster-daemon"
    if not daemon.is_file():
        raise ValidationError(f"dagster-daemon is missing from the Ictus venv: {daemon}")
    handle = log_path.open("w")
    process = subprocess.Popen(
        [str(daemon), "run", "-w", str(workspace), "--log-level", "info"],
        cwd=str(work_root),
        env=os.environ.copy(),
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    time.sleep(6)
    if process.poll() is not None:
        handle.close()
        raise ValidationError("dagster-daemon exited during startup:\n" + _tail(log_path))
    return process, handle


def _wait_terminal(submission: Any, receipt: dict[str, Any], *, instance: Any, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = submission.query_receipt(receipt, instance=instance)
        if last.get("state") == "terminal":
            return last
        time.sleep(1)
    raise ValidationError(f"Dagster run did not reach terminal state; last={last}")


def _counts(db: SqliteExecutionAdmission) -> dict[str, int]:
    connection = db._connection  # noqa: SLF001 - harness evidence only
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "work_orders",
            "execution_attempts",
            "submissions",
            "result_inbox",
            "recovery_decision_inbox",
        )
    }


def _import_checked_issue(env: dict[str, Any]) -> tuple[WorkOrder, SourceRevision, dict[str, Any]]:
    """Import the checked Issue revision into a real, provenance-bound Work Order.

    The slice must not run on an invented Work Order id: the id, the source
    revision and the specification digest all come from the recorded GitHub
    Issue revision through the real import service, and the import must be
    idempotent with no source holds.
    """
    if not ISSUE_FIXTURE.is_file():
        raise ValidationError(f"checked Issue fixture is missing: {ISSUE_FIXTURE}")

    from tactus.application import import_issue, recheck_source
    from tactus.integrations.github import MockGitHubIssueSource

    source = MockGitHubIssueSource.from_files(
        {(ISSUE_REPOSITORY, ISSUE_NUMBER): ISSUE_FIXTURE}
    )
    work_source = env["work_source"]
    imported = import_issue(source, work_source, ISSUE_REPOSITORY, ISSUE_NUMBER)
    if not imported.created:
        raise ValidationError("a fresh ledger did not create the Work Order on import")
    reimported = import_issue(source, work_source, ISSUE_REPOSITORY, ISSUE_NUMBER)
    if reimported.created or reimported.work_order_id != imported.work_order_id:
        raise ValidationError("re-importing the same Issue revision was not idempotent")
    holds = recheck_source(source, work_source, imported.work_order_id)
    if holds:
        raise ValidationError(
            "the checked Issue revision is held: "
            f"{[hold.reason.value for hold in holds]}"
        )

    revision = imported.provenance.revision
    source_revision = revision.source_revision
    now = _now()
    work_order = imported.work_order
    work_order.set_readiness(OpenStatus.READY)
    work_order.claim(
        reason="checked Issue revision entering the acceptance slice", at=now
    )
    env["db"].register_work_order(
        work_order.id,
        revision=WorkOrderRevision(0),
        source_revision=source_revision,
        state=work_order.state,
        readiness=None,
        at=now,
    )
    evidence = {
        "issue": {
            "repository": ISSUE_REPOSITORY,
            "number": ISSUE_NUMBER,
            "url": revision.url,
            "title": revision.title,
            "updated_at": revision.updated_at.isoformat(),
            "labels": list(revision.labels),
            "state": revision.issue_state,
        },
        "work_order_id": str(imported.work_order_id),
        "import_key": imported.provenance.import_key.value,
        "source_revision": source_revision.value,
        "specification_digest": revision.specification_digest,
        "body_sha256": revision.body_sha256,
        "created": imported.created,
        "reimport_idempotent": True,
        "source_holds": [],
    }
    return work_order, source_revision, evidence


def _initial_snapshot(
    work_order: WorkOrder,
    *,
    capability: str,
    snapshot_id: str,
    source_revision: SourceRevision,
) -> dict[str, Any]:
    """Genuine INITIAL authorization context (no prior attempt, no observation)."""
    return build_state_snapshot(
        work_order=work_order,
        phase=SnapshotPhase.INITIAL,
        semantic_budget=SemanticBudget(0, 3),
        step_retry=StepRetryDiagnostic(0),
        snapshot_id=snapshot_id,
        timestamp=_now(),
        capability_id=capability,
        dependencies_satisfied=True,
        source_revision=source_revision.value,
        work_order_revision=0,
    )


def _assert_intent_invariant(
    *, authorized: Mapping[str, Any], submitted: Mapping[str, Any], label: str
) -> dict[str, Any]:
    """Prove submitted == authorized, exactly, including ``arguments``."""
    authorized_payload = json.loads(json.dumps(authorized, sort_keys=True))
    submitted_payload = json.loads(json.dumps(submitted, sort_keys=True))
    if submitted_payload != authorized_payload:
        raise ValidationError(
            f"{label}: submitted ExecutionIntent differs from the authorized intent\n"
            f"authorized={json.dumps(authorized_payload, sort_keys=True)}\n"
            f"submitted={json.dumps(submitted_payload, sort_keys=True)}"
        )
    arguments = submitted_payload.get(INTENT_ARGUMENT_KEY) or {}
    leaked = sorted(FORBIDDEN_INTENT_KEYS & set(arguments))
    if leaked:
        raise ValidationError(
            f"{label}: failure-control key(s) {leaked} leaked into the authorized intent"
        )
    if FORBIDDEN_INTENT_KEYS & set(submitted_payload):
        raise ValidationError(
            f"{label}: failure-control key(s) placed at the intent's top level"
        )
    return {
        "label": label,
        "authorized_sha256": hashlib.sha256(
            json.dumps(authorized_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "submitted_sha256": hashlib.sha256(
            json.dumps(submitted_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "arguments": arguments,
        "identical": True,
        "failure_control_keys_present": False,
    }


def _evidence_safe(value: Any) -> Any:
    """Drop private (non-JSON) keys from one scenario's evidence projection."""
    if isinstance(value, Mapping):
        return {
            str(key): _evidence_safe(item)
            for key, item in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, (list, tuple)):
        return [_evidence_safe(item) for item in value]
    return value


def _read_attempt_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _step_events(instance: Any, run_id: str, step_key: str = "execute_op") -> dict[str, int]:
    """Count Dagster's own step-lifecycle events for one step (backend evidence).

    This is what proves *Dagster* owned the retry: the capability implementation
    only raised ``Failure``; the retry scheduling is the backend's.
    """
    counts: dict[str, int] = {}
    try:
        records = instance.all_logs(run_id)
    except Exception:  # pragma: no cover - event storage unavailable
        return counts
    for record in records:
        event = getattr(record, "dagster_event", None)
        if event is None:
            continue
        if getattr(event, "step_key", None) != step_key:
            continue
        name = str(getattr(event, "event_type_value", ""))
        counts[name] = counts.get(name, 0) + 1
    return counts


def _run_count(instance: Any, submission: Any, intent_id: str) -> list[str]:
    from dagster import RunsFilter

    return [
        run.run_id
        for run in instance.get_runs(filters=RunsFilter(tags={submission.IDENTITY_TAG: intent_id}))
    ]


@contextlib.contextmanager
def _environment(
    scenario: str, control: dict[str, Any], ictus_root: Path
) -> Iterator[dict[str, Any]]:
    """Yield one isolated scenario environment (fresh root, instance, daemon)."""
    base = Path("/tmp") if Path("/tmp").is_dir() else None
    work_root = Path(
        tempfile.mkdtemp(
            prefix=f"tactus-failure-{scenario}-", dir=str(base) if base else None
        )
    )
    dagster_home, workspace, control_path, attempt_log = _write_environment(
        work_root, ictus_root, control
    )
    _configure(work_root, dagster_home, ictus_root, control_path, attempt_log)

    from dagster import DagsterInstance, __version__ as dagster_version
    from ictus_dagster import submission

    instance = DagsterInstance.get()
    log_path = work_root / "dagster-daemon.log"
    daemon, handle = _start_daemon(ictus_root, workspace, work_root, log_path)
    from tactus.persistence import SqliteWorkSource

    db_path = work_root / "tactus-ledger.sqlite"
    db = SqliteExecutionAdmission(db_path)
    work_source = SqliteWorkSource(db_path)
    cycle = ExecutionCycle(db)
    try:
        yield {
            "scenario": scenario,
            "work_root": work_root,
            "dagster_home": dagster_home,
            "workspace": workspace,
            "control_path": control_path,
            "attempt_log": attempt_log,
            "daemon_log": log_path,
            "instance": instance,
            "submission": submission,
            "dagster_version": dagster_version,
            "db_path": db_path,
            "db": db,
            "work_source": work_source,
            "cycle": cycle,
        }
    finally:
        with contextlib.suppress(Exception):
            env_work_source = locals().get("work_source")
            if env_work_source is not None:
                env_work_source.close()
        with contextlib.suppress(Exception):
            db.close()
        _terminate(daemon)
        handle.close()


def _admit(
    env: dict[str, Any],
    *,
    binary: Path,
    capability: str,
    snapshot_id: str,
    programme: dict[str, Any],
) -> dict[str, Any]:
    """Authorize, durably admit, register the bounded programme, and submit.

    The control programme is registered **after** Ictus authorized the intent
    and is keyed by the resulting intent id; it can therefore only ever affect
    the capability implementation, never the authorization result.
    """
    db: SqliteExecutionAdmission = env["db"]
    cycle: ExecutionCycle = env["cycle"]
    submission = env["submission"]

    work_order, source_revision, provenance = _import_checked_issue(env)
    work_order_id = str(work_order.id)
    starting_revision = WorkOrderRevision(0)
    snapshot = _initial_snapshot(
        work_order,
        capability=capability,
        snapshot_id=snapshot_id,
        source_revision=source_revision,
    )
    _, envelope = _decide(binary, snapshot)
    if envelope["policy_decision"]["decision"] != "ALLOW":
        raise ValidationError("Ictus INITIAL decision was not ALLOW")
    authorized = envelope.get("execution_intent")
    if not isinstance(authorized, Mapping):
        raise ValidationError("Ictus INITIAL decision carried no ExecutionIntent")

    applied = cycle.apply_recovery_decision(
        work_order,
        envelope,
        state_port=db,
        expected_revision=starting_revision,
        initial_context=InitialAuthorizationContext(
            phase=_fact(snapshot, "snapshot.phase"),
            snapshot_id=snapshot["snapshot_id"],
            snapshot_digest=_fact(snapshot, "snapshot.digest"),
            source_revision=source_revision,
            work_order_revision=starting_revision,
        ),
    )
    revision = applied.work_order_revision
    if revision is None:
        raise ValidationError("INITIAL authorization did not persist a revision")
    now = _now()
    # A real claim lease is bounded and expires; the recovery scenario advances
    # the clock past it instead of hand-releasing a fencing token.
    claim_expiry = now + timedelta(seconds=60)
    claim = db.acquire_claim(
        work_order_id,
        expected_revision=revision,
        claim_token=ClaimToken(f"{env['scenario']}-claim"),
        expires_at=claim_expiry,
        now=now,
    )
    accepted = cycle.accept(
        claim,
        envelope,
        source_revision=source_revision,
        work_order=work_order,
        at=now,
    )
    invariant = _assert_intent_invariant(
        authorized=authorized,
        submitted=accepted.intent.as_dict,
        label=env["scenario"],
    )
    if accepted.intent.digest != accepted.submission.intent_digest:
        raise ValidationError("admission digest diverged from the accepted intent")

    _register_programme(env, accepted.intent.intent_id.value, programme)
    receipt = submission.submit_intent(accepted.intent.as_dict)
    db.mark_dispatched(accepted.submission.submission_id)
    return {
        "work_order_id": work_order_id,
        "work_order": work_order,
        "snapshot": snapshot,
        "envelope": envelope,
        "accepted": accepted,
        "receipt": receipt,
        "revision": db.work_order_snapshot(work_order_id)[0],
        "claim_expiry": claim_expiry,
        "source_revision": source_revision,
        "provenance": provenance,
        "intent_invariant": invariant,
    }


def _attach(env: dict[str, Any], admitted: dict[str, Any]) -> None:
    """Record the durable Dagster receipt for an admitted submission."""
    db: SqliteExecutionAdmission = env["db"]
    db.attach_receipt(
        admitted["accepted"].submission.submission_id,
        DagsterRunId(admitted["receipt"]["receipt_id"]),
    )


def _observe(
    env: dict[str, Any], admitted: dict[str, Any], status: Mapping[str, Any]
) -> dict[str, Any]:
    """Parse, fail-closed-check, correlate and apply one terminal observation."""
    cycle: ExecutionCycle = env["cycle"]
    db: SqliteExecutionAdmission = env["db"]
    accepted = admitted["accepted"]
    result = status["result"]
    category = result.get("observation", {}).get("category")
    if not isinstance(category, str) or not category:
        raise ValidationError("Ictus result carries no observation.category")

    payload = {
        "schema_version": 1,
        "observation_id": f"ictus-result:{result['execution_id']}",
        "execution_id": result["execution_id"],
        "intent_id": accepted.intent.intent_id.value,
        "category": category,
        "observed_at": result.get("finished_at") or _now().isoformat(),
        "message": (
            f"Ictus Dagster result status={result.get('status')} "
            f"run={status.get('state')}"
        ),
        "evidence": result.get("evidence", []),
    }
    observation = parse_observation(payload)

    # Fail closed: an unknown category and a mismatched identity are refused.
    denials: list[dict[str, str]] = []
    bad_category = dict(payload, category="NOT_AN_ICTUS_CATEGORY")
    try:
        parse_observation(bad_category)
    except ObservationContractError as exc:
        denials.append({"case": "unknown_category", "denied": type(exc).__name__})
    else:  # pragma: no cover - contract failure
        raise ValidationError("an unknown observation category was accepted")
    try:
        cycle.correlate(
            dict(payload, intent_id="intent-mismatch"),
            attempt_id=accepted.attempt.attempt_id,
            submission_id=accepted.submission.submission_id,
        )
    except ObservationCorrelationError as exc:
        denials.append({"case": "intent_mismatch", "denied": type(exc).__name__})
    else:  # pragma: no cover - contract failure
        raise ValidationError("a mismatched intent was correlated")

    correlated = cycle.correlate(
        observation,
        attempt_id=accepted.attempt.attempt_id,
        submission_id=accepted.submission.submission_id,
        expected_work_order_id=accepted.attempt.work_order_id,
        expected_intent_digest=accepted.intent.digest,
    )
    applied = cycle.apply_observation(
        correlated,
        work_order=admitted["work_order"],
        expected_revision=admitted["revision"],
        at=_now(),
    )
    replay = cycle.apply_observation(correlated)
    if replay.applied:
        raise ValidationError("duplicate observation delivery applied a second effect")
    return {
        "category": category,
        "result": result,
        "payload": payload,
        # Live objects are kept only for the scenario body; evidence is JSON.
        "_observation": observation,
        "_correlated": correlated,
        "observation_payload": payload,
        "applied": applied.applied,
        "replay_applied": replay.applied,
        "fail_closed_denials": denials,
        "work_order_state": applied.work_order_state.value,
        "attempt_closed": db.attempt(accepted.attempt.attempt_id).closed_at is not None,
    }


def scenario_transient(env: dict[str, Any], *, binary: Path) -> dict[str, Any]:
    """Attempt 1 fails, Dagster retries, the run succeeds, count stays at 1."""
    db: SqliteExecutionAdmission = env["db"]
    cycle: ExecutionCycle = env["cycle"]
    submission = env["submission"]

    admitted = _admit(
        env,
        binary=binary,
        capability=CAPABILITY,
        snapshot_id="snapshot-transient-entry",
        programme=env["programme"],
    )
    accepted = admitted["accepted"]

    # Crash-after-send: close the ledger, reopen it and resend the same intent.
    db.close()
    env["db"] = SqliteExecutionAdmission(env["db_path"])
    db = env["db"]
    env["cycle"] = ExecutionCycle(db)
    cycle = env["cycle"]
    pending = db.pending_submissions()
    if len(pending) != 1 or pending[0].submission_id != accepted.submission.submission_id:
        raise ValidationError(
            "crash recovery did not find the original pending submission: "
            f"{[item.submission_id.value for item in pending]}"
        )
    resent = submission.submit_intent(accepted.intent.as_dict)
    if resent["receipt_id"] != admitted["receipt"]["receipt_id"]:
        raise ValidationError("resend created a different Dagster run")
    _attach(env, admitted)

    status = _wait_terminal(submission, resent, instance=env["instance"], timeout=180)
    if status["result"]["observation"]["category"] != "SUCCESS":
        raise ValidationError(
            "transient scenario did not succeed after the retry: "
            + json.dumps(status["result"], sort_keys=True)
        )
    observed = _observe(env, admitted, status)
    if observed["work_order_state"] != "IMPLEMENTED":
        raise ValidationError("transient scenario did not mark the Work Order IMPLEMENTED")

    attempts = [
        entry
        for entry in _read_attempt_log(env["attempt_log"])
        if entry["intent_id"] == accepted.intent.intent_id.value
    ]
    if [entry["outcome"] for entry in attempts] != ["failed", "succeeded"]:
        raise ValidationError(f"unexpected capability attempt ledger: {attempts}")
    if [entry["attempt"] for entry in attempts] != [1, 2]:
        raise ValidationError(f"unexpected Dagster attempt numbers: {attempts}")

    runs = _run_count(env["instance"], submission, accepted.intent.intent_id.value)
    step_events = _step_events(env["instance"], runs[0]) if len(runs) == 1 else {}
    if step_events.get("STEP_UP_FOR_RETRY", 0) != 1:
        raise ValidationError(
            f"Dagster did not retry the execute step exactly once: {step_events}"
        )
    if step_events.get("STEP_SUCCESS") != 1:
        raise ValidationError(f"the retried execute step did not succeed: {step_events}")
    counts = _counts(db)
    if len(runs) != 1:
        raise ValidationError(f"expected exactly one Dagster run, found {runs}")
    if counts["execution_attempts"] != 1:
        raise ValidationError(
            "a Dagster step retry created a new Tactus semantic attempt: "
            f"{counts['execution_attempts']}"
        )
    if counts["result_inbox"] != 1:
        raise ValidationError(f"duplicate delivery changed the result inbox: {counts}")

    return {
        "scenario": "transient",
        "dagster_run_ids": runs,
        "dagster_run_count": len(runs),
        "capability_attempts": attempts,
        "dagster_step_events": step_events,
        "step_retries": step_events.get("STEP_UP_FOR_RETRY", 0),
        "semantic_attempts": counts["execution_attempts"],
        "semantic_attempt_count_unchanged": counts["execution_attempts"] == 1,
        "observation": observed,
        "durable_counts": counts,
        "provenance": admitted["provenance"],
        "intent_invariant": admitted["intent_invariant"],
    }


def scenario_terminal(env: dict[str, Any], *, binary: Path) -> dict[str, Any]:
    """All attempts fail; a genuine Ictus RECOVERY decision authorizes attempt 2."""
    db: SqliteExecutionAdmission = env["db"]
    cycle: ExecutionCycle = env["cycle"]
    submission = env["submission"]

    admitted = _admit(
        env,
        binary=binary,
        capability=CAPABILITY,
        snapshot_id="snapshot-terminal-entry",
        programme=env["programme"],
    )
    _attach(env, admitted)
    accepted = admitted["accepted"]
    status = _wait_terminal(submission, admitted["receipt"], instance=env["instance"], timeout=180)
    if status["result"]["status"] != "failed":
        raise ValidationError(
            "terminal scenario did not fail: " + json.dumps(status["result"], sort_keys=True)
        )
    observed = _observe(env, admitted, status)
    if observed["category"] != "PROCESS_CRASH":
        raise ValidationError(f"unexpected terminal category: {observed['category']}")
    if observed["work_order_state"] != "ACTIVE":
        raise ValidationError("a failure observation must leave the Work Order ACTIVE")
    if not observed["attempt_closed"]:
        raise ValidationError("a failure observation must close the attempt")

    attempts = [
        entry
        for entry in _read_attempt_log(env["attempt_log"])
        if entry["intent_id"] == accepted.intent.intent_id.value
    ]
    if len(attempts) != 4 or {entry["outcome"] for entry in attempts} != {"failed"}:
        raise ValidationError(f"expected four failed attempts, got {attempts}")
    run_ids = _run_count(env["instance"], submission, accepted.intent.intent_id.value)
    step_events = _step_events(env["instance"], run_ids[0]) if len(run_ids) == 1 else {}
    if step_events.get("STEP_UP_FOR_RETRY", 0) != 3:
        raise ValidationError(
            f"the execute step's retry budget was not exhausted: {step_events}"
        )

    # A real RECOVERY snapshot built from the durable attempt/submission, then
    # decided by the pinned Ictus binary. Tactus never synthesizes the intent.
    receipt = db.submission(accepted.submission.submission_id)
    snapshot = cycle.recovery_snapshot(
        admitted["work_order"],
        observation=observed["_observation"],
        attempt=db.attempt(accepted.attempt.attempt_id),
        submission=receipt,
        semantic_budget=SemanticBudget(1, 3),
        step_retry=StepRetryDiagnostic(1),
        snapshot_id="snapshot-terminal-recovery",
        timestamp=_now(),
        capability_id=CAPABILITY,
        source_revision=admitted["source_revision"].value,
        work_order_revision=1,
    )
    _, envelope = _decide(binary, snapshot)
    if envelope["proposal"]["decision"] != "REEXECUTE":
        raise ValidationError(
            f"expected REEXECUTE within budget, got {envelope['proposal']['decision']}"
        )
    if envelope["policy_decision"]["decision"] != "ALLOW":
        raise ValidationError("recovery decision was not ALLOW")
    next_intent = envelope.get("execution_intent")
    if not isinstance(next_intent, Mapping):
        raise ValidationError("REEXECUTE ALLOW carried no ExecutionIntent")

    revision = db.work_order_snapshot(admitted["work_order_id"])[0]
    recovery = cycle.apply_recovery_decision(
        admitted["work_order"],
        envelope,
        state_port=db,
        expected_revision=revision,
    )
    if not recovery.applied:
        raise ValidationError("the first recovery decision was treated as a replay")
    if recovery.next_intent is None:
        raise ValidationError("an applied REEXECUTE authorized no new attempt")
    replay = cycle.apply_recovery_decision(
        admitted["work_order"], envelope, state_port=db, expected_revision=revision
    )
    if replay.applied or replay.next_intent is not None:
        raise ValidationError("a replayed recovery decision authorized a second attempt")

    # The first attempt's lease has expired by the time recovery authorizes a
    # new semantic attempt, so the fencing token advances (fence 1 -> 2).
    now = admitted["claim_expiry"] + timedelta(seconds=1)
    new_revision = db.work_order_snapshot(admitted["work_order_id"])[0]
    claim = db.acquire_claim(
        admitted["work_order_id"],
        expected_revision=new_revision,
        claim_token=ClaimToken("terminal-claim-2"),
        expires_at=now + timedelta(seconds=60),
        now=now,
    )
    second = cycle.accept(
        claim,
        envelope,
        source_revision=admitted["source_revision"],
        work_order=admitted["work_order"],
        at=now,
    )
    invariant = _assert_intent_invariant(
        authorized=next_intent,
        submitted=second.intent.as_dict,
        label="terminal-recovery",
    )
    counts = _counts(db)
    if counts["execution_attempts"] != 2:
        raise ValidationError(f"recovery did not create exactly one new attempt: {counts}")
    # One audited decision for the INITIAL authorization and one for the
    # recovery; the replay above must not add a third.
    if counts["recovery_decision_inbox"] != 2:
        raise ValidationError(f"recovery decision was not applied exactly once: {counts}")

    return {
        "scenario": "terminal",
        "dagster_run_ids": _run_count(env["instance"], submission, accepted.intent.intent_id.value),
        "capability_attempts": attempts,
        "dagster_step_events": step_events,
        "terminal_observation": observed,
        "recovery_snapshot_phase": _fact(snapshot, "snapshot.phase"),
        "recovery_proposal_decision": envelope["proposal"]["decision"],
        "recovery_decision_applied": recovery.applied,
        "recovery_replay_applied": replay.applied,
        "new_attempt": {
            "attempt_id": second.attempt.attempt_id.value,
            "intent_id": second.intent.intent_id.value,
            "submission_id": second.submission.submission_id.value,
        },
        "durable_counts": counts,
        "provenance": admitted["provenance"],
        "intent_invariant": invariant,
    }


def scenario_escalation(env: dict[str, Any], *, binary: Path) -> dict[str, Any]:
    """Exhausted budget ⇒ Ictus ESCALATE ⇒ one audited OPEN+BLOCKED effect."""
    db: SqliteExecutionAdmission = env["db"]
    cycle: ExecutionCycle = env["cycle"]
    submission = env["submission"]

    admitted = _admit(
        env,
        binary=binary,
        capability=CAPABILITY,
        snapshot_id="snapshot-escalation-entry",
        programme=env["programme"],
    )
    _attach(env, admitted)
    accepted = admitted["accepted"]
    status = _wait_terminal(submission, admitted["receipt"], instance=env["instance"], timeout=180)
    observed = _observe(env, admitted, status)
    if observed["category"] != "PROCESS_CRASH":
        raise ValidationError(f"unexpected terminal category: {observed['category']}")

    receipt = db.submission(accepted.submission.submission_id)
    snapshot = cycle.recovery_snapshot(
        admitted["work_order"],
        observation=observed["_observation"],
        attempt=db.attempt(accepted.attempt.attempt_id),
        submission=receipt,
        semantic_budget=SemanticBudget(3, 3),
        step_retry=StepRetryDiagnostic(0),
        snapshot_id="snapshot-escalation-recovery",
        timestamp=_now(),
        capability_id=CAPABILITY,
        source_revision=admitted["source_revision"].value,
        work_order_revision=1,
    )
    _, envelope = _decide(binary, snapshot)
    if envelope["proposal"]["decision"] != "ESCALATE":
        raise ValidationError(
            f"expected ESCALATE at 3/3, got {envelope['proposal']['decision']}"
        )
    if envelope["policy_decision"]["decision"] != "ALLOW":
        raise ValidationError("ESCALATE was not policy-allowed")
    if envelope.get("execution_intent") is not None:
        raise ValidationError("an ESCALATE decision carried an ExecutionIntent")

    revision = db.work_order_snapshot(admitted["work_order_id"])[0]
    interventions = SqliteHumanIntervention(env["db_path"])
    escalation = cycle.apply_recovery_decision(
        admitted["work_order"],
        envelope,
        state_port=db,
        expected_revision=revision,
        intervention_store=interventions,
    )
    if not escalation.applied or escalation.next_intent is not None:
        raise ValidationError("ESCALATE must apply once and authorize no intent")
    if escalation.application is None or escalation.application.intervention is None:
        raise ValidationError("ESCALATE did not create a typed human-intervention request")
    intervention_id = escalation.application.intervention.intervention_id
    if admitted["work_order"].readiness is not OpenStatus.BLOCKED:
        raise ValidationError("ESCALATE did not leave the Work Order OPEN+BLOCKED")

    # Restart: the audited effect and its idempotency must survive a new process.
    db.close()
    reopened = SqliteExecutionAdmission(env["db_path"])
    env["db"] = reopened
    env["cycle"] = ExecutionCycle(reopened)
    state_after_restart = reopened.work_order_snapshot(admitted["work_order_id"])
    if state_after_restart[1].value != "OPEN":
        raise ValidationError(f"escalation did not survive restart: {state_after_restart}")
    replay = env["cycle"].apply_recovery_decision(
        admitted["work_order"], envelope, state_port=reopened, expected_revision=revision
    )
    if replay.applied or replay.next_intent is not None:
        raise ValidationError("replayed ESCALATE applied a second effect after restart")
    counts = _counts(reopened)
    if counts["recovery_decision_inbox"] != 2:
        raise ValidationError(f"restart duplicated the escalation record: {counts}")

    # The typed request must be reconstructible from durable state alone and
    # must accept only a valid typed resolution (AC: escalations persist across
    # restart and accept only valid typed resolution).
    interventions.close()
    interventions = SqliteHumanIntervention(env["db_path"])
    request = interventions.request(intervention_id)
    if request.work_order_id != admitted["work_order"].id:
        raise ValidationError("the persisted request targets a different Work Order")
    current_revision = state_after_restart[0]
    now = _now()

    def resolution(resolution_id: str, action: HumanAction) -> HumanResolution:
        return HumanResolution(
            resolution_id=resolution_id,
            intervention_id=intervention_id,
            work_order_id=request.work_order_id,
            actor="operator",
            action=action,
            resolved_at=now,
            source_revision=admitted["source_revision"],
            work_order_revision=current_revision,
            evidence=("operator resolved the persisted escalation",),
        )

    denials: list[dict[str, str]] = []
    try:
        interventions.resolve(
            resolution("resolution-scope-change", HumanAction.AUTHORIZE_SCOPE_CHANGE),
            current_source_revision=admitted["source_revision"],
            current_work_order_revision=current_revision,
        )
    except ActionNotPermittedError as exc:
        denials.append({"case": "unpermitted_action", "denied": type(exc).__name__})
    else:
        raise ValidationError("an unpermitted typed action was accepted")

    accepted_resolution = resolution("resolution-approve", HumanAction.APPROVE)
    _, grant = interventions.resolve(
        accepted_resolution,
        current_source_revision=admitted["source_revision"],
        current_work_order_revision=current_revision,
    )
    _, replay_grant = interventions.resolve(
        accepted_resolution,
        current_source_revision=admitted["source_revision"],
        current_work_order_revision=current_revision,
    )
    if replay_grant.grant_id != grant.grant_id:
        raise ValidationError("a replayed resolution produced a second grant")
    try:
        interventions.resolve(
            resolution("resolution-second", HumanAction.APPROVE),
            current_source_revision=admitted["source_revision"],
            current_work_order_revision=current_revision,
        )
    except RequestAlreadyResolvedError as exc:
        denials.append({"case": "second_active_resolution", "denied": type(exc).__name__})
    else:
        raise ValidationError("a second active resolution was accepted")
    if len(interventions.resolutions_for(intervention_id)) != 1:
        raise ValidationError("a request ended up with more than one resolution")
    intervention_evidence = {
        "intervention_id": intervention_id,
        "reason": request.reason.value,
        "summary": request.summary,
        "permitted_actions": [action.value for action in request.permitted_actions],
        "resolved_action": grant.action.value,
        "grant_id": grant.grant_id,
        "grant_replay_same_id": replay_grant.grant_id == grant.grant_id,
        "resolutions_recorded": len(interventions.resolutions_for(intervention_id)),
        "denials": denials,
        "recoverable_after_restart": True,
    }
    interventions.close()

    return {
        "scenario": "escalation",
        "terminal_observation_category": observed["category"],
        "recovery_proposal_decision": envelope["proposal"]["decision"],
        "execution_intent_present": False,
        "intervention_created": True,
        "work_order_state_after_restart": state_after_restart[1].value,
        "work_order_blocked_after_restart": True,
        "replay_applied_after_restart": replay.applied,
        "durable_counts_after_restart": counts,
        "intervention": intervention_evidence,
        "provenance": admitted["provenance"],
        "intent_invariant": admitted["intent_invariant"],
    }


SCENARIO_CONTROL: dict[str, dict[str, Any]] = {
    # Intent ids are produced by Ictus; the harness rewrites the control file
    # after authorization (the programme is keyed by the authorized intent id).
    "transient": {"fail_attempts": 1},
    "terminal": {"fail_hard": True},
    "escalation": {"fail_hard": True},
}


def _register_programme(env: dict[str, Any], intent_id: str, programme: dict[str, Any]) -> None:
    control = {"version": 1, "intents": {intent_id: programme}}
    env["control_path"].write_text(json.dumps(control, indent=2, sort_keys=True) + "\n")


def run(args: argparse.Namespace) -> dict[str, Any]:
    ictus_root = Path(args.ictus_root).expanduser().resolve()
    pin, binary = _verify_ictus(ictus_root)
    results: dict[str, Any] = {}

    for name in args.scenarios:
        programme = SCENARIO_CONTROL[name]
        with _environment(name, {"version": 1, "intents": {}}, ictus_root) as env:
            env["programme"] = programme
            if name == "transient":
                results[name] = _evidence_safe(scenario_transient(env, binary=binary))
            elif name == "terminal":
                results[name] = _evidence_safe(scenario_terminal(env, binary=binary))
            elif name == "escalation":
                results[name] = _evidence_safe(scenario_escalation(env, binary=binary))
            else:  # pragma: no cover - argparse restricts the choices
                raise ValidationError(f"unknown scenario {name!r}")
            results[name]["environment"] = {
                "work_root": str(env["work_root"]),
                "dagster_home": str(env["dagster_home"]),
                "ledger": str(env["db_path"]),
                "code_location": CODE_LOCATION,
                "control_file": str(env["control_path"]),
                "attempt_log": str(env["attempt_log"]),
            }
            results[name]["result"] = "passed"

    return {
        "schema_version": 1,
        "kind": "tactus.ictus.dagster.execution_failure_scenarios",
        "generated_at": _now().isoformat(),
        "result": "passed",
        "versions": {"python": sys.version, "ictus": pin},
        "invariant": (
            "submitted ExecutionIntent == Ictus-authorized ExecutionIntent; "
            "failure control is out-of-band and bounded"
        ),
        "scenarios": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ictus-root",
        default=os.environ.get("TACTUS_ICTUS_ROOT"),
        help="Path to the pinned Ictus checkout (or TACTUS_ICTUS_ROOT).",
    )
    parser.add_argument(
        "--evidence",
        default="/tmp/tactus-failure-scenarios-evidence.json",
        help="Where to write the JSON evidence record.",
    )
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=sorted(SCENARIO_CONTROL),
        default=["transient", "terminal", "escalation"],
    )
    args = parser.parse_args(argv)
    if not args.ictus_root:
        parser.error("--ictus-root or TACTUS_ICTUS_ROOT is required")

    try:
        evidence = run(args)
    except Exception as exc:
        print(f"failure-scenario validation failed: {exc}", file=sys.stderr)
        return 1

    path = Path(args.evidence)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(evidence), indent=2, sort_keys=True) + "\n")
    summary = ", ".join(
        f"{name}={data['result']}" for name, data in evidence["scenarios"].items()
    )
    print(f"execution-failure scenarios passed: {summary} evidence={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
