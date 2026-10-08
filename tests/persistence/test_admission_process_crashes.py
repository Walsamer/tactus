"""Exercise independent processes and abrupt exits at durable boundaries."""
from __future__ import annotations

import multiprocessing
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from tactus.domain import (
    ClaimToken, ClaimUnavailableError, DagsterRunId, DurableAdmissionRequest,
    ExecutionIntentId, FencingError, OpenStatus, ResultRecord,
    SourceRevision, StaleWorkOrderRevisionError, WorkOrderId, WorkOrderRevision,
    WorkOrderState,
)
from tactus.persistence import SqliteExecutionAdmission

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
CRASH_EXIT = 73


def _request(token="owner"):
    return DurableAdmissionRequest(
        WorkOrderId("WO"), WorkOrderRevision(0), SourceRevision("source"),
        ClaimToken(token), ExecutionIntentId("intent"), "intent-digest",
    )


def _claim(adapter, token="owner", revision=0, now=NOW):
    return adapter.acquire_claim(
        "WO", expected_revision=WorkOrderRevision(revision), claim_token=ClaimToken(token),
        expires_at=now + timedelta(minutes=5), now=now,
    )


def _seed(path):
    adapter = SqliteExecutionAdmission(path)
    adapter.register_work_order(
        "WO", revision=WorkOrderRevision(0), source_revision=SourceRevision("source"), at=NOW,
    )
    adapter.close()


def _compete(path, token, barrier, results):
    adapter = SqliteExecutionAdmission(path)
    try:
        barrier.wait(timeout=15)
        held = _claim(adapter, token)
        attempt, _ = adapter.accept_durable(held, _request(token), at=NOW)
        results.put((os.getpid(), "accepted", str(attempt.attempt_id)))
    except (ClaimUnavailableError, StaleWorkOrderRevisionError):
        results.put((os.getpid(), "rejected", None))
    finally:
        adapter.close()


def _finish(process, expected_exit=0):
    process.join(timeout=20)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("child process exceeded timeout")
    assert process.exitcode == expected_exit


def test_two_processes_cannot_accept_the_same_revision(tmp_path):
    path = tmp_path / "domain.sqlite"
    _seed(path)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [context.Process(target=_compete, args=(path, token, barrier, results))
                 for token in ("one", "two")]
    try:
        for process in processes:
            process.start()
        for process in processes:
            _finish(process)
        outcomes = [results.get(timeout=5) for _ in processes]
        assert len({item[0] for item in outcomes}) == 2
        assert sorted(item[1] for item in outcomes) == ["accepted", "rejected"]
        adapter = SqliteExecutionAdmission(path)
        assert len(adapter.pending_submissions()) == 1
        assert adapter.work_order_snapshot("WO") == (
            WorkOrderRevision(1), WorkOrderState.ACTIVE, None,
        )
        adapter.close()
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()
        results.join_thread()


def _crash_at_boundary(path, bridge_path, boundary):
    adapter = SqliteExecutionAdmission(path)
    held = _claim(adapter)
    if boundary == "precommit":
        def crash_before_commit(statement):
            if statement == "COMMIT":
                os._exit(CRASH_EXIT)
        adapter._connection.set_trace_callback(crash_before_commit)
    attempt, submission = adapter.accept_durable(held, _request(), at=NOW)
    if boundary == "postsend":
        # A durable stand-in for an external bridge. Its commit is independent
        # of Tactus; crash before persisting any local send mark or receipt.
        with sqlite3.connect(bridge_path) as bridge:
            bridge.execute("CREATE TABLE receipts (submission_id TEXT PRIMARY KEY, run_id TEXT UNIQUE)")
            bridge.execute("INSERT INTO receipts VALUES (?, 'run-1')", (str(submission.submission_id),))
        os._exit(CRASH_EXIT)
    if boundary == "postapplication":
        adapter._connection.execute("CREATE TABLE effects (name TEXT PRIMARY KEY)")
        def complete(connection):
            connection.execute("INSERT INTO effects VALUES ('verified-success')")
            connection.execute(
                "UPDATE work_orders SET state='IMPLEMENTED', revision=revision+1 WHERE work_order_id='WO'"
            )
        adapter.apply_result(
            ResultRecord("result", attempt.attempt_id, "result-digest"),
            apply_domain_effect=complete, at=NOW,
        )
        os._exit(CRASH_EXIT)
    raise AssertionError("crash boundary was not reached")


def _run_crash(path, bridge_path, boundary):
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_at_boundary, args=(path, bridge_path, boundary),
    )
    process.start()
    _finish(process, CRASH_EXIT)


def test_process_crash_after_admission_writes_before_commit_rolls_back(tmp_path):
    path = tmp_path / "domain.sqlite"
    _seed(path)
    _run_crash(path, tmp_path / "bridge.sqlite", "precommit")
    adapter = SqliteExecutionAdmission(path)
    assert adapter.work_order_snapshot("WO") == (
        WorkOrderRevision(0), WorkOrderState.OPEN, OpenStatus.READY,
    )
    for table in ("execution_attempts", "submissions", "submission_outbox"):
        assert adapter._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    adapter.accept_durable(_claim(adapter), _request(), at=NOW)
    assert len(adapter.pending_submissions()) == 1
    adapter.close()


def test_process_crash_after_send_reconciles_same_identity_after_lease_expiry(tmp_path):
    path = tmp_path / "domain.sqlite"
    bridge_path = tmp_path / "bridge.sqlite"
    _seed(path)
    original = SqliteExecutionAdmission(path)
    old_claim = _claim(original)
    original.close()
    _run_crash(path, bridge_path, "postsend")
    adapter = SqliteExecutionAdmission(path)
    (submission,) = adapter.pending_submissions()
    later = NOW + timedelta(minutes=10)
    with pytest.raises(FencingError):
        adapter.accept_durable(old_claim, _request(), at=later)
    with sqlite3.connect(bridge_path) as bridge:
        receipt = bridge.execute(
            "SELECT run_id FROM receipts WHERE submission_id=?", (str(submission.submission_id),)
        ).fetchone()
        assert receipt == ("run-1",)
        assert bridge.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
    assert adapter.attach_receipt(submission.submission_id, receipt[0]).dagster_run_id == DagsterRunId("run-1")
    assert adapter.pending_submissions() == ()
    assert adapter._connection.execute("SELECT count(*) FROM execution_attempts").fetchone()[0] == 1
    adapter.close()


def test_process_crash_after_application_replays_without_duplicate_effect(tmp_path):
    path = tmp_path / "domain.sqlite"
    _seed(path)
    _run_crash(path, tmp_path / "bridge.sqlite", "postapplication")
    adapter = SqliteExecutionAdmission(path)
    (submission,) = adapter.reconciliation_submissions()
    record = ResultRecord("result", submission.attempt_id, "result-digest")
    def must_not_run(connection):
        pytest.fail("duplicate domain effect")
    assert adapter.apply_result(record, apply_domain_effect=must_not_run, at=NOW) is False
    assert adapter.work_order_snapshot("WO") == (
        WorkOrderRevision(2), WorkOrderState.IMPLEMENTED, None,
    )
    assert adapter.attempt(submission.attempt_id).closed_at == NOW
    assert adapter.pending_submissions() == ()
    assert adapter._connection.execute("SELECT count(*) FROM effects").fetchone()[0] == 1
    adapter.close()
