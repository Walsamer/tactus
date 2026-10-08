from __future__ import annotations

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest

from tactus.domain import (
    ClaimToken,
    ClaimUnavailableError,
    DagsterRunId,
    DurableAdmissionRequest,
    ExecutionIntentId,
    FencingError,
    ImmutableRecordConflictError,
    OpenStatus,
    ResultRecord,
    SourceRevision,
    StaleWorkOrderRevisionError,
    WorkOrderId,
    WorkOrderRevision,
    WorkOrderState,
)
from tactus.persistence import SqliteExecutionAdmission


NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def request(token: str = "claim", digest: str = "intent-sha") -> DurableAdmissionRequest:
    return DurableAdmissionRequest(
        WorkOrderId("WO-1"), WorkOrderRevision(7), SourceRevision("source-7"),
        ClaimToken(token), ExecutionIntentId("intent-1"), digest,
    )


def store(tmp_path) -> SqliteExecutionAdmission:
    result = SqliteExecutionAdmission(tmp_path / "domain.sqlite")
    result.register_work_order(
        "WO-1", revision=WorkOrderRevision(7), source_revision=SourceRevision("source-7"), at=NOW
    )
    return result


def claim(adapter: SqliteExecutionAdmission, token: str = "claim", *, at: datetime = NOW):
    return adapter.acquire_claim(
        "WO-1", expected_revision=WorkOrderRevision(7), claim_token=ClaimToken(token),
        expires_at=at + timedelta(minutes=5), now=at,
    )


def test_competing_process_cannot_claim_or_accept_same_revision(tmp_path) -> None:
    first = store(tmp_path)
    second = SqliteExecutionAdmission(tmp_path / "domain.sqlite")
    held = claim(first, "one")

    with pytest.raises(ClaimUnavailableError):
        claim(second, "two")

    attempt, submission = first.accept_durable(held, request("one"), at=NOW)
    assert submission.attempt_id == attempt.attempt_id
    revision, state, readiness = second.work_order_snapshot("WO-1")
    assert revision == WorkOrderRevision(8)
    assert state is WorkOrderState.ACTIVE
    assert readiness is None


def test_simultaneous_sqlite_claims_have_one_holder(tmp_path) -> None:
    seed = store(tmp_path)
    seed.close()

    def acquire(token: str):
        adapter = SqliteExecutionAdmission(tmp_path / "domain.sqlite")
        try:
            return claim(adapter, token)
        except ClaimUnavailableError:
            return None
        finally:
            adapter.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(acquire, ("first", "second")))
    assert sum(item is not None for item in claims) == 1


def test_expired_holder_is_fenced_even_if_it_still_has_the_old_claim_object(tmp_path) -> None:
    adapter = store(tmp_path)
    old = claim(adapter, "old")
    later = NOW + timedelta(minutes=6)
    current = claim(adapter, "new", at=later)

    with pytest.raises(FencingError):
        adapter.accept_durable(old, request("old"), at=later)

    attempt, _ = adapter.accept_durable(current, request("new"), at=later)
    assert attempt.fence == 2


def test_restart_and_postsend_preack_recover_the_original_submission_identity(tmp_path) -> None:
    first = store(tmp_path)
    held = claim(first)
    attempt, submission = first.accept_durable(held, request(), at=NOW)
    first.mark_dispatched(submission.submission_id, at=NOW)  # send happened; receipt did not
    first.close()  # process crashes after commit/before acknowledgement

    restarted = SqliteExecutionAdmission(tmp_path / "domain.sqlite")
    assert restarted.pending_submissions() == (submission,)
    recovered_attempt, recovered_submission = restarted.accept_durable(held, request(), at=NOW)
    assert (recovered_attempt, recovered_submission) == (attempt, submission)
    assert restarted.attach_receipt(submission.submission_id, DagsterRunId("run-1")).dagster_run_id == DagsterRunId("run-1")


def test_precommit_rejection_leaves_no_attempt_or_dispatch_record(tmp_path) -> None:
    adapter = store(tmp_path)
    held = claim(adapter)
    stale_source = DurableAdmissionRequest(
        WorkOrderId("WO-1"), WorkOrderRevision(7), SourceRevision("changed"),
        ClaimToken("claim"), ExecutionIntentId("intent-1"), "intent-sha",
    )

    with pytest.raises(StaleWorkOrderRevisionError):
        adapter.accept_durable(held, stale_source, at=NOW)
    assert adapter.pending_submissions() == ()
    assert adapter.work_order_snapshot("WO-1") == (
        WorkOrderRevision(7), WorkOrderState.OPEN, OpenStatus.READY
    )


def test_receipt_and_submission_identity_are_immutable(tmp_path) -> None:
    adapter = store(tmp_path)
    held = claim(adapter)
    _, submission = adapter.accept_durable(held, request(), at=NOW)
    adapter.attach_receipt(submission.submission_id, "run-1")

    with pytest.raises(ImmutableRecordConflictError):
        adapter.attach_receipt(submission.submission_id, "run-2")

    with pytest.raises(ImmutableRecordConflictError):
        adapter.accept_durable(held, request(digest="changed"), at=NOW)


def test_result_effect_and_close_roll_back_together_then_deduplicate(tmp_path) -> None:
    adapter = store(tmp_path)
    attempt, _ = adapter.accept_durable(claim(adapter), request(), at=NOW)
    adapter._connection.execute("CREATE TABLE effects (name TEXT PRIMARY KEY)")
    record = ResultRecord("result-1", attempt.attempt_id, "payload-sha")

    def broken_effect(connection) -> None:
        connection.execute("INSERT INTO effects VALUES ('effect')")
        raise RuntimeError("fault after domain effect")

    with pytest.raises(RuntimeError):
        adapter.apply_result(record, apply_domain_effect=broken_effect, at=NOW)
    assert adapter.attempt(attempt.attempt_id).closed_at is None
    assert adapter._connection.execute("SELECT * FROM effects").fetchall() == []

    def effect(connection) -> None:
        connection.execute("INSERT INTO effects VALUES ('effect')")

    assert adapter.apply_result(record, apply_domain_effect=effect, at=NOW) is True
    assert adapter.attempt(attempt.attempt_id).closed_at == NOW
    assert adapter.apply_result(record, apply_domain_effect=effect, at=NOW) is False
    assert adapter._connection.execute("SELECT count(*) FROM effects").fetchone()[0] == 1
    with pytest.raises(ImmutableRecordConflictError):
        adapter.apply_result(ResultRecord("result-1", attempt.attempt_id, "changed"), at=NOW)
