from __future__ import annotations

import contextlib
import sqlite3
from datetime import timedelta

import pytest

from tactus.application import (
    ExecutionCycle,
    IntentDecisionError,
    ObservationCorrelationError,
    WorkOrderProjectionError,
    parse_execution_decision,
)
from tactus.domain import (
    ChildCreationUnavailableError,
    ClaimToken,
    DagsterRunId,
    ImmutableRecordConflictError,
    MissingChildPlanError,
    StaleWorkOrderRevisionError,
    OpenStatus,
    RecoveryDecisionRecord,
    SourceRevision,
    TransitionAuthority,
    WorkOrder,
    WorkOrderId,
    WorkOrderRevision,
    WorkOrderState,
)
from tactus.domain._clock import utcnow
from tactus.integrations.ictus import (
    SemanticBudget,
    StepRetryDiagnostic,
    parse_observation,
)
from tactus.persistence.sqlite import SqliteExecutionAdmission


WORK_ORDER = "WO-E2E-1"
SOURCE = "source-rev-1"
CAPABILITY = "demo.verify"


def _intent(
    *,
    intent_id: str = "intent-1",
    work_order_id: str = WORK_ORDER,
    decision_id: str = "proposal-1",
    target_id: str | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "intent_id": intent_id,
        "capability": CAPABILITY,
        "target": {"type": "work_order", "id": target_id or work_order_id},
        "arguments": {},
        "requested_by": {"decision_id": decision_id, "provider": "ictus.rules.v1"},
        "policy_context": {"snapshot_id": "snapshot-1"},
    }


def _envelope(
    *,
    decision: str = "REEXECUTE",
    proposal_id: str = "proposal-1",
    target_id: str = WORK_ORDER,
    intent: dict | None = None,
    policy: str = "ALLOW",
    include_intent: bool = True,
) -> dict:
    proposal = {
        "schema_version": 2,
        "proposal_id": proposal_id,
        "decision": decision,
        "subject": {"type": "work_order", "id": target_id},
        "provider": {"id": "rules.v1", "version": "0.1.0"},
        "timestamp": utcnow().isoformat(),
        "reason": "test decision",
    }
    if decision in {"REEXECUTE", "ROUTE", "EXECUTE_CAPABILITY"}:
        proposal["capability"] = CAPABILITY
    payload = {
        "proposal": proposal,
        "policy_decision": {
            "schema_version": 1,
            "decision": policy,
            "proposal_id": proposal_id,
            "reason": "test policy",
        },
    }
    if include_intent:
        payload["execution_intent"] = intent or _intent(
            decision_id=proposal_id, work_order_id=target_id
        )
    return payload


def _observation(
    *,
    category: str = "SUCCESS",
    execution_id: str = "run-1",
    intent_id: str = "intent-1",
    observation_id: str = "obs-1",
) -> dict:
    return {
        "schema_version": 1,
        "observation_id": observation_id,
        "execution_id": execution_id,
        "intent_id": intent_id,
        "category": category,
        "observed_at": utcnow().isoformat(),
        "message": "runtime outcome",
        "evidence": [{"kind": "dagster_run", "uri": f"dagster://runs/{execution_id}"}],
    }


def _cycle(tmp_path) -> tuple[ExecutionCycle, SqliteExecutionAdmission, WorkOrder, object]:
    now = utcnow()
    db = SqliteExecutionAdmission(tmp_path / "ledger.sqlite")
    work_order = WorkOrder.create(WORK_ORDER)
    work_order.admit(
        reason="imported",
        authority=TransitionAuthority.OPERATOR,
        at=now,
    )
    work_order.set_readiness(OpenStatus.READY)
    db.register_work_order(
        WORK_ORDER,
        revision=WorkOrderRevision(0),
        source_revision=SourceRevision(SOURCE),
        state=work_order.state,
        readiness=work_order.readiness,
        at=now,
    )
    claim = db.acquire_claim(
        WORK_ORDER,
        expected_revision=WorkOrderRevision(0),
        claim_token=ClaimToken("claim-1"),
        expires_at=now + timedelta(minutes=10),
        now=now,
    )
    cycle = ExecutionCycle(db)
    accepted = cycle.accept(
        claim,
        _envelope(),
        source_revision=SOURCE,
        work_order=work_order,
        at=now,
    )
    return cycle, db, work_order, accepted


def _attach_and_correlate(cycle, db, accepted, observation_payload):
    receipt = cycle.attach_receipt(accepted, DagsterRunId("run-1"))
    correlated = cycle.correlate(
        observation_payload,
        attempt_id=accepted.attempt.attempt_id,
        submission_id=accepted.submission.submission_id,
        expected_work_order_id=WORK_ORDER,
        expected_intent_digest=accepted.intent.digest,
    )
    return receipt, correlated


def test_accept_binds_validated_intent_and_commits_before_dispatch(tmp_path):
    cycle, db, work_order, accepted = _cycle(tmp_path)

    assert accepted.intent.intent_id.value == "intent-1"
    assert accepted.intent.decision_id == "proposal-1"
    assert accepted.intent.digest == accepted.submission.intent_digest
    assert accepted.attempt.work_order_id.value == WORK_ORDER
    assert accepted.attempt.closed_at is None
    assert work_order.state.value == "ACTIVE"
    revision, state, readiness = db.work_order_snapshot(WORK_ORDER)
    assert revision == WorkOrderRevision(1)
    assert state.value == "ACTIVE"
    assert readiness is None
    assert len(db.pending_submissions()) == 1


def test_success_observation_closes_attempt_and_marks_implemented(tmp_path):
    cycle, db, work_order, accepted = _cycle(tmp_path)
    _, correlated = _attach_and_correlate(cycle, db, accepted, _observation())

    result = cycle.apply_observation(
        correlated,
        work_order=work_order,
        expected_revision=WorkOrderRevision(1),
    )

    assert result.applied is True
    assert result.work_order_state.value == "IMPLEMENTED"
    assert result.work_order_revision == WorkOrderRevision(2)
    assert result.attempt_closed is True
    assert work_order.state.value == "IMPLEMENTED"
    assert db.attempt(accepted.attempt.attempt_id).closed_at is not None
    assert db.work_order_snapshot(WORK_ORDER)[1].value == "IMPLEMENTED"

    replay = cycle.apply_observation(correlated)
    assert replay.applied is False
    assert replay.work_order_revision == WorkOrderRevision(2)
    assert db.work_order_snapshot(WORK_ORDER)[1].value == "IMPLEMENTED"


def test_failure_observation_closes_attempt_but_leaves_work_order_active(tmp_path):
    cycle, db, work_order, accepted = _cycle(tmp_path)
    _, correlated = _attach_and_correlate(
        cycle, db, accepted, _observation(category="WORKER_TIMEOUT")
    )

    result = cycle.apply_observation(
        correlated,
        work_order=work_order,
        expected_revision=WorkOrderRevision(1),
    )

    assert result.applied is True
    assert result.attempt_closed is True
    assert result.work_order_state.value == "ACTIVE"
    assert work_order.state.value == "ACTIVE"
    assert [failure.summary for failure in work_order.failure_observations] == ["WORKER_TIMEOUT"]
    assert db.work_order_snapshot(WORK_ORDER)[1].value == "ACTIVE"


def test_failure_observation_requires_active_work_order(tmp_path):
    cycle, db, work_order, accepted = _cycle(tmp_path)
    _, correlated = _attach_and_correlate(
        cycle, db, accepted, _observation(category="WORKER_TIMEOUT")
    )
    work_order.complete(reason="wrongly completed")
    db.apply_work_order_transition(
        WORK_ORDER,
        expected_revision=WorkOrderRevision(1),
        state=work_order.state,
        readiness=work_order.readiness,
    )

    with pytest.raises(Exception, match="ACTIVE Work Order"):
        cycle.apply_observation(correlated)


def test_recovery_decision_reopens_and_allows_a_new_semantic_attempt(tmp_path):
    cycle, db, work_order, accepted = _cycle(tmp_path)
    receipt, correlated = _attach_and_correlate(
        cycle, db, accepted, _observation(category="PROCESS_CRASH")
    )
    failed = cycle.apply_observation(
        correlated, work_order=work_order, expected_revision=WorkOrderRevision(1)
    )
    assert failed.work_order_state.value == "ACTIVE"

    snapshot = cycle.recovery_snapshot(
        work_order,
        observation=correlated.observation,
        attempt=accepted.attempt,
        submission=receipt,
        semantic_budget=SemanticBudget(1, 3),
        step_retry=StepRetryDiagnostic(7),
        snapshot_id="snapshot-recovery-1",
        timestamp=utcnow(),
        capability_id=CAPABILITY,
        source_revision=SOURCE,
        work_order_revision=1,
    )
    facts = {fact["key"]: fact["value"] for fact in snapshot["facts"]}
    assert facts["recovery.semantic_attempts"] == 1
    assert facts["recovery.max_semantic_attempts"] == 3
    assert facts["execution.step_retry_index"] == 7
    assert facts["observation.category"] == "PROCESS_CRASH"

    recovery = cycle.apply_recovery_decision(
        work_order,
        _envelope(
            proposal_id="proposal-recovery-1",
            intent=_intent(intent_id="intent-2", decision_id="proposal-recovery-1"),
        ),
        state_port=db,
        expected_revision=WorkOrderRevision(1),
    )

    assert recovery.decision.kind.value == "REEXECUTE"
    assert recovery.next_intent is not None
    assert recovery.next_intent.intent_id.value == "intent-2"
    assert recovery.work_order_revision == WorkOrderRevision(2)
    assert work_order.state is WorkOrderState.OPEN
    assert work_order.readiness is OpenStatus.READY
    assert db.work_order_snapshot(WORK_ORDER)[1].value == "OPEN"
    assert db.work_order_snapshot(WORK_ORDER)[2] is OpenStatus.READY

    now = utcnow() + timedelta(minutes=11)
    claim = db.acquire_claim(
        WORK_ORDER,
        expected_revision=WorkOrderRevision(2),
        claim_token=ClaimToken("claim-2"),
        expires_at=now + timedelta(minutes=10),
        now=now,
    )
    second = cycle.accept(
        claim,
        _envelope(
            proposal_id="proposal-recovery-1",
            intent=_intent(intent_id="intent-2", decision_id="proposal-recovery-1"),
        ),
        source_revision=SOURCE,
        work_order=work_order,
        at=now,
    )
    assert second.attempt.intent_id.value == "intent-2"
    assert second.attempt.attempt_id != accepted.attempt.attempt_id
    assert work_order.state.value == "ACTIVE"


def test_observation_correlation_fails_closed_on_every_identity(tmp_path):
    cycle, db, _, accepted = _cycle(tmp_path)
    cycle.attach_receipt(accepted, DagsterRunId("run-1"))

    with pytest.raises(ObservationCorrelationError, match="Dagster run"):
        cycle.correlate(
            _observation(execution_id="other-run"),
            attempt_id=accepted.attempt.attempt_id,
            submission_id=accepted.submission.submission_id,
        )
    with pytest.raises(ObservationCorrelationError, match="intent"):
        cycle.correlate(
            _observation(intent_id="other-intent"),
            attempt_id=accepted.attempt.attempt_id,
            submission_id=accepted.submission.submission_id,
        )
    with pytest.raises(ObservationCorrelationError, match="different Work Order"):
        cycle.correlate(
            _observation(),
            attempt_id=accepted.attempt.attempt_id,
            submission_id=accepted.submission.submission_id,
            expected_work_order_id="WO-OTHER",
        )
    with pytest.raises(ObservationCorrelationError, match="digest"):
        cycle.correlate(
            _observation(),
            attempt_id=accepted.attempt.attempt_id,
            submission_id=accepted.submission.submission_id,
            expected_intent_digest="not-the-digest",
        )


def test_observation_correlation_rejects_unreceipted_and_closed_attempts(tmp_path):
    cycle, db, work_order, accepted = _cycle(tmp_path)

    with pytest.raises(ObservationCorrelationError, match="durable Dagster receipt"):
        cycle.correlate(
            _observation(),
            attempt_id=accepted.attempt.attempt_id,
            submission_id=accepted.submission.submission_id,
        )

    _, correlated = _attach_and_correlate(cycle, db, accepted, _observation())
    cycle.apply_observation(correlated)
    with pytest.raises(ObservationCorrelationError, match="already closed"):
        cycle.correlate(
            _observation(observation_id="obs-late"),
            attempt_id=accepted.attempt.attempt_id,
            submission_id=accepted.submission.submission_id,
        )


def test_decision_envelope_must_be_allow_with_matching_intent():
    with pytest.raises(IntentDecisionError, match="ALLOW"):
        parse_execution_decision(_envelope(policy="DENY"))
    with pytest.raises(IntentDecisionError, match="no ExecutionIntent"):
        parse_execution_decision(_envelope(include_intent=False))
    with pytest.raises(IntentDecisionError, match="does not match"):
        parse_execution_decision(_envelope(intent=_intent(target_id="WO-OTHER")))
    with pytest.raises(IntentDecisionError, match="not Work Order"):
        parse_execution_decision(_envelope(), expected_work_order_id="WO-OTHER")
    with pytest.raises(IntentDecisionError, match="not requested"):
        parse_execution_decision(
            _envelope(intent=_intent(decision_id="other-proposal"))
        )


def test_decision_envelope_accepts_legacy_retry_token():
    parsed = parse_execution_decision(_envelope(decision="RETRY"))

    assert parsed.decision_kind == "REEXECUTE"
    assert parsed.intent_id.value == "intent-1"


def test_observation_parser_rejects_unknown_categories_and_versions():
    with pytest.raises(Exception, match="unknown Ictus observation category"):
        parse_observation(_observation(category="MADE_UP"))
    payload = _observation()
    payload["schema_version"] = 99
    with pytest.raises(Exception, match="schema_version"):
        parse_observation(payload)


# -- issue #8: transactional, idempotent recovery application ---------------


def _rev(db):
    return db.work_order_snapshot(WORK_ORDER)[0]


def _failed_cycle(tmp_path):
    """Cycle with an ACTIVE Work Order whose attempt failed (attempt closed)."""
    cycle, db, work_order, accepted = _cycle(tmp_path)
    receipt, correlated = _attach_and_correlate(
        cycle, db, accepted, _observation(category="PROCESS_CRASH")
    )
    cycle.apply_observation(
        correlated, work_order=work_order, expected_revision=WorkOrderRevision(1)
    )
    return cycle, db, work_order, accepted, receipt


def test_recovery_decision_is_idempotent_on_redelivery(tmp_path):
    cycle, db, work_order, accepted, receipt = _failed_cycle(tmp_path)
    envelope = _envelope(
        proposal_id="proposal-idem-1",
        intent=_intent(intent_id="intent-2", decision_id="proposal-idem-1"),
    )

    first = cycle.apply_recovery_decision(
        work_order, envelope, state_port=db, expected_revision=_rev(db)
    )
    revision_after_first = db.work_order_snapshot(WORK_ORDER)[0]
    second = cycle.apply_recovery_decision(
        work_order, envelope, state_port=db, expected_revision=revision_after_first
    )

    assert first.applied is True
    assert second.applied is False
    assert second.application is None
    assert db.work_order_snapshot(WORK_ORDER)[0] == revision_after_first
    assert work_order.state is WorkOrderState.OPEN


def test_recovery_decision_replay_with_changed_payload_conflicts(tmp_path):
    cycle, db, work_order, accepted, receipt = _failed_cycle(tmp_path)
    cycle.apply_recovery_decision(
        work_order,
        _envelope(proposal_id="proposal-x", intent=_intent(intent_id="i-2", decision_id="proposal-x")),
        state_port=db,
        expected_revision=_rev(db),
    )
    with pytest.raises(ImmutableRecordConflictError):
        cycle.apply_recovery_decision(
            work_order,
            _envelope(
                decision="ROUTE",
                proposal_id="proposal-x",
                intent=_intent(intent_id="i-route", decision_id="proposal-x"),
            ),
            state_port=db,
            expected_revision=_rev(db),
        )


def test_recovery_decision_rejects_a_stale_revision_and_changes_nothing(tmp_path):
    cycle, db, work_order, accepted, receipt = _failed_cycle(tmp_path)
    before = db.work_order_snapshot(WORK_ORDER)
    with pytest.raises((WorkOrderProjectionError, StaleWorkOrderRevisionError)):
        cycle.apply_recovery_decision(
            work_order,
            _envelope(proposal_id="proposal-stale", intent=_intent(intent_id="i-2", decision_id="proposal-stale")),
            state_port=db,
            expected_revision=WorkOrderRevision(_rev(db).value + 5),
        )
    assert db.work_order_snapshot(WORK_ORDER) == before
    assert work_order.state is WorkOrderState.ACTIVE


def test_recovery_decision_escalates_durably_and_closes_in_one_step(tmp_path):
    cycle, db, work_order, accepted, receipt = _failed_cycle(tmp_path)
    decision = cycle.apply_recovery_decision(
        work_order,
        _envelope(decision="ESCALATE", proposal_id="proposal-esc", include_intent=False),
        state_port=db,
        expected_revision=_rev(db),
    )
    assert decision.applied is True
    assert decision.application is not None
    assert decision.application.intervention is not None
    assert work_order.state is WorkOrderState.OPEN
    assert work_order.readiness is OpenStatus.BLOCKED
    assert db.work_order_snapshot(WORK_ORDER)[1] is WorkOrderState.OPEN


def test_recovery_decision_abort_is_terminal_and_audited(tmp_path):
    cycle, db, work_order, accepted, receipt = _failed_cycle(tmp_path)
    decision = cycle.apply_recovery_decision(
        work_order,
        _envelope(decision="ABORT", proposal_id="proposal-abort", include_intent=False),
        state_port=db,
        expected_revision=_rev(db),
    )
    assert decision.applied is True
    assert work_order.state is WorkOrderState.RETIRED
    assert db.work_order_snapshot(WORK_ORDER)[1] is WorkOrderState.RETIRED


def test_decompose_fails_closed_and_leaves_the_ledger_untouched(tmp_path):
    cycle, db, work_order, accepted, receipt = _failed_cycle(tmp_path)
    before = db.work_order_snapshot(WORK_ORDER)
    with pytest.raises((ChildCreationUnavailableError, MissingChildPlanError)):
        cycle.apply_recovery_decision(
            work_order,
            _envelope(decision="DECOMPOSE", proposal_id="proposal-dec", include_intent=False),
            state_port=db,
            expected_revision=_rev(db),
        )
    assert db.work_order_snapshot(WORK_ORDER) == before
    assert work_order.state is WorkOrderState.ACTIVE
    assert db._connection.execute(
        "SELECT COUNT(*) FROM recovery_decision_inbox"
    ).fetchone()[0] == 0


def test_decision_effect_and_ledger_roll_back_together_on_fault(tmp_path):
    cycle, db, work_order, accepted, receipt = _failed_cycle(tmp_path)
    before = db.work_order_snapshot(WORK_ORDER)

    def explode(_connection):
        raise RuntimeError("injected fault inside the decision transaction")

    record = RecoveryDecisionRecord(
        decision_id="proposal-fault",
        work_order_id=WorkOrderId(WORK_ORDER),
        attempt_id=accepted.attempt.attempt_id,
        decision_digest="digest-fault",
        expected_revision=_rev(db),
    )
    with pytest.raises(RuntimeError):
        db.apply_recovery_decision(record, apply_domain_effect=explode)

    assert db.work_order_snapshot(WORK_ORDER) == before
    assert db._connection.execute(
        "SELECT COUNT(*) FROM recovery_decision_inbox"
    ).fetchone()[0] == 0


def test_applied_decision_survives_restart(tmp_path):
    cycle, db, work_order, accepted, receipt = _failed_cycle(tmp_path)
    envelope = _envelope(
        proposal_id="proposal-restart",
        intent=_intent(intent_id="i-restart", decision_id="proposal-restart"),
    )
    cycle.apply_recovery_decision(
        work_order, envelope, state_port=db, expected_revision=_rev(db)
    )
    db.close()

    reopened = SqliteExecutionAdmission(tmp_path / "ledger.sqlite")
    try:
        assert reopened._connection.execute(
            "SELECT COUNT(*) FROM recovery_decision_inbox"
        ).fetchone()[0] == 1
        from tactus.domain.work_order import WorkOrder as _WorkOrder

        entity = _WorkOrder.create(WORK_ORDER)
        reopened_cycle = ExecutionCycle(reopened)
        replay = reopened_cycle.apply_recovery_decision(
            entity,
            envelope,
            state_port=reopened,
            expected_revision=reopened.work_order_snapshot(WORK_ORDER)[0],
        )
        assert replay.applied is False
        assert reopened.work_order_snapshot(WORK_ORDER)[1] is WorkOrderState.OPEN
    finally:
        reopened.close()


# -- issue #8 follow-up: faults after the callback still roll the entity back --


class _FailingConnection:
    """Proxy that fails one statement or the commit, after the effect ran."""

    def __init__(self, connection, *, fail_on: str):
        self._connection = connection
        self._fail_on = fail_on

    def execute(self, sql, params=()):
        if self._fail_on in sql:
            raise sqlite3.OperationalError(f"injected failure: {self._fail_on}")
        return self._connection.execute(sql, params)

    def fetchone(self):
        return self._connection.fetchone()

    def fetchall(self):
        return self._connection.fetchall()

    def __getattr__(self, name):
        return getattr(self._connection, name)


def _decision_envelope(decision_id: str) -> dict:
    return _envelope(
        proposal_id=decision_id,
        intent=_intent(intent_id=f"intent-{decision_id}", decision_id=decision_id),
    )


def _failing_transaction(db, fail_on):
    real_transaction = db._transaction

    @contextlib.contextmanager
    def failing_transaction():
        original = db._connection
        db._connection = _FailingConnection(original, fail_on=fail_on)
        try:
            with real_transaction():
                yield
        finally:
            db._connection = original

    return failing_transaction


@pytest.mark.parametrize(
    "fail_on",
    [
        "INSERT INTO recovery_decision_inbox",   # after the effect + projection
        "COMMIT",                                # after the whole unit of work
    ],
)
def test_recovery_fault_after_the_callback_leaves_no_partial_state(tmp_path, fail_on, monkeypatch):
    cycle, db, work_order, accepted, receipt = _failed_cycle(tmp_path)
    before_snapshot = db.work_order_snapshot(WORK_ORDER)
    before_entity = (work_order.state, work_order.readiness)

    monkeypatch.setattr(db, "_transaction", _failing_transaction(db, fail_on))

    with pytest.raises(sqlite3.OperationalError):
        cycle.apply_recovery_decision(
            work_order,
            _decision_envelope(f"proposal-fault-{fail_on.split()[0].lower()}"),
            state_port=db,
            expected_revision=before_snapshot[0],
        )

    monkeypatch.undo()
    assert db.work_order_snapshot(WORK_ORDER) == before_snapshot
    assert (work_order.state, work_order.readiness) == before_entity
    assert db._connection.execute(
        "SELECT COUNT(*) FROM recovery_decision_inbox"
    ).fetchone()[0] == 0


def test_attempt_closure_failure_restores_the_entity(tmp_path, monkeypatch):
    """The attempt is still open, so the closure UPDATE runs and can fail."""
    cycle, db, work_order, accepted = _cycle(tmp_path)
    before_snapshot = db.work_order_snapshot(WORK_ORDER)
    before_entity = (work_order.state, work_order.readiness)

    monkeypatch.setattr(
        db, "_transaction", _failing_transaction(db, "UPDATE execution_attempts SET closed_at")
    )

    with pytest.raises(sqlite3.OperationalError):
        cycle.apply_recovery_decision(
            work_order,
            _decision_envelope("proposal-fault-closure"),
            state_port=db,
            expected_revision=before_snapshot[0],
        )

    monkeypatch.undo()
    assert db.work_order_snapshot(WORK_ORDER) == before_snapshot
    assert (work_order.state, work_order.readiness) == before_entity
    assert db.latest_attempt(WORK_ORDER).closed_at is None


def test_replay_never_authorizes_a_second_dispatch(tmp_path):
    cycle, db, work_order, accepted, receipt = _failed_cycle(tmp_path)
    envelope = _decision_envelope("proposal-redispatch")

    first = cycle.apply_recovery_decision(
        work_order, envelope, state_port=db, expected_revision=_rev(db)
    )
    replay = cycle.apply_recovery_decision(
        work_order, envelope, state_port=db, expected_revision=_rev(db)
    )

    assert first.applied is True and first.next_intent is not None
    assert replay.applied is False
    assert replay.next_intent is None
