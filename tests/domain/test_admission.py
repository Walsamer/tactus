from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone

import pytest

from tactus.domain import (
    AdmissionFacts,
    DagsterRunId,
    DuplicateExecutionAttemptError,
    ExecutionAdmission,
    ExecutionAdmissionPort,
    ExecutionAttempt,
    ExecutionIntentId,
    ExecutionRequest,
    ExecutionRequestMismatchError,
    IneligibleWorkOrderError,
    OpenStatus,
    TransitionAuthority,
    UnknownExecutionAttemptError,
    WorkOrder,
    WorkOrderId,
    WorkOrderState,
)

_NOON = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def admitted_work_order(work_order_id: str = "WO-1") -> WorkOrder:
    work_order = WorkOrder.create(work_order_id)
    work_order.admit(reason="admitted", at=_NOON)
    return work_order


def ready_work_order(work_order_id: str = "WO-1") -> WorkOrder:
    work_order = admitted_work_order(work_order_id)
    work_order.set_readiness(OpenStatus.READY)
    return work_order


def active_work_order(work_order_id: str = "WO-1") -> WorkOrder:
    work_order = ready_work_order(work_order_id)
    work_order.claim(reason="claimed", at=_NOON)
    return work_order


def execution_request(
    work_order_id: str = "WO-1",
    intent_id: str = "intent-1",
    dagster_run_id: str = "run-1",
) -> ExecutionRequest:
    return ExecutionRequest(
        work_order_id=WorkOrderId(work_order_id),
        intent_id=ExecutionIntentId(intent_id),
        dagster_run_id=DagsterRunId(dagster_run_id),
    )


# -- port contract --------------------------------------------------------


def test_implementation_satisfies_the_port() -> None:
    assert isinstance(ExecutionAdmission(), ExecutionAdmissionPort)


def test_attempt_value_carries_only_correlation_not_dagster_state() -> None:
    assert {field.name for field in fields(ExecutionAttempt)} == {
        "attempt_id",
        "work_order_id",
        "intent_id",
        "dagster_run_id",
        "accepted_at",
    }


# -- atomic acceptance ----------------------------------------------------


def test_accept_transitions_open_ready_to_active_and_correlates() -> None:
    admission = ExecutionAdmission()
    work_order = ready_work_order()

    attempt = admission.accept(work_order, execution_request(), at=_NOON)

    assert work_order.state is WorkOrderState.ACTIVE
    assert work_order.readiness is None
    assert attempt.work_order_id == WorkOrderId("WO-1")
    assert attempt.intent_id == ExecutionIntentId("intent-1")
    assert attempt.dagster_run_id == DagsterRunId("run-1")
    assert attempt.accepted_at == _NOON
    assert admission.active_attempt("WO-1") is attempt


def test_accept_records_execution_admission_authority() -> None:
    admission = ExecutionAdmission()
    work_order = ready_work_order()

    admission.accept(work_order, execution_request(), at=_NOON)

    record = work_order.transitions[-1]
    assert record.from_state is WorkOrderState.OPEN
    assert record.to_state is WorkOrderState.ACTIVE
    assert record.authority is TransitionAuthority.EXECUTION_ADMISSION
    assert record.occurred_at == _NOON


# -- eligibility: no non-READY claim --------------------------------------


def test_evaluate_reports_ready_work_order_eligible() -> None:
    admission = ExecutionAdmission()
    work_order = ready_work_order()

    decision = admission.evaluate(work_order)

    assert decision.work_order_id == work_order.id
    assert decision.eligible is True
    assert decision.reason


def test_evaluate_reports_open_but_unknown_as_ineligible() -> None:
    admission = ExecutionAdmission()
    work_order = admitted_work_order()

    decision = admission.evaluate(work_order)

    assert decision.eligible is False
    assert "READY" in decision.reason


@pytest.mark.parametrize(
    "readiness",
    [OpenStatus.UNKNOWN, OpenStatus.BLOCKED],
)
def test_accept_rejects_open_but_not_ready(readiness: OpenStatus) -> None:
    admission = ExecutionAdmission()
    work_order = admitted_work_order()
    work_order.set_readiness(readiness)

    with pytest.raises(IneligibleWorkOrderError):
        admission.accept(work_order, execution_request(), at=_NOON)

    assert work_order.state is WorkOrderState.OPEN
    assert work_order.readiness is readiness
    assert admission.active_attempt("WO-1") is None


def test_accept_rejects_draft_work_order() -> None:
    admission = ExecutionAdmission()
    work_order = WorkOrder.create("WO-1")

    with pytest.raises(IneligibleWorkOrderError):
        admission.accept(work_order, execution_request(), at=_NOON)

    assert work_order.state is WorkOrderState.DRAFT
    assert admission.active_attempt("WO-1") is None


def test_accept_rejects_already_active_work_order() -> None:
    admission = ExecutionAdmission()
    work_order = active_work_order()

    with pytest.raises(IneligibleWorkOrderError):
        admission.accept(
            work_order, execution_request(intent_id="intent-2", dagster_run_id="run-2")
        )


def test_duplicate_protection_is_by_work_order_id_not_object_identity() -> None:
    admission = ExecutionAdmission()
    first = ready_work_order("WO-1")
    attempt = admission.accept(first, execution_request("WO-1", "intent-1", "run-1"), at=_NOON)

    # A separate object for the same Work Order id cannot slip a second attempt
    # in, even if its own lifecycle state looks OPEN + READY.
    duplicate = ready_work_order("WO-1")
    with pytest.raises(DuplicateExecutionAttemptError):
        admission.accept(duplicate, execution_request("WO-1", "intent-2", "run-2"))

    assert admission.active_attempt("WO-1") is attempt


def test_accept_rejects_request_for_a_different_work_order() -> None:
    admission = ExecutionAdmission()
    work_order = ready_work_order("WO-1")

    with pytest.raises(ExecutionRequestMismatchError):
        admission.accept(work_order, execution_request(work_order_id="WO-2"))

    assert work_order.state is WorkOrderState.OPEN
    assert admission.active_attempt("WO-1") is None


@pytest.mark.parametrize(
    "facts",
    [
        AdmissionFacts(dependencies_satisfied=False),
        AdmissionFacts(capabilities_present=False),
        AdmissionFacts(system_paused=True),
        AdmissionFacts(budget_available=False),
    ],
)
def test_accept_rejects_unsatisfied_domain_gates(facts: AdmissionFacts) -> None:
    admission = ExecutionAdmission()
    work_order = ready_work_order()

    decision = admission.evaluate(work_order, facts=facts)
    assert decision.eligible is False
    assert decision.reason

    with pytest.raises(IneligibleWorkOrderError):
        admission.accept(work_order, execution_request(), facts=facts, at=_NOON)

    assert work_order.state is WorkOrderState.OPEN
    assert admission.active_attempt("WO-1") is None


# -- duplicate-execution protection ---------------------------------------


def test_duplicate_active_attempt_is_atomic_and_rejected() -> None:
    admission = ExecutionAdmission()
    work_order = ready_work_order()
    first = admission.accept(work_order, execution_request(), at=_NOON)
    transitions_before = work_order.transitions

    with pytest.raises(DuplicateExecutionAttemptError):
        admission.accept(
            work_order,
            execution_request(intent_id="intent-2", dagster_run_id="run-2"),
            at=_NOON,
        )

    assert work_order.transitions == transitions_before
    assert work_order.state is WorkOrderState.ACTIVE
    assert admission.active_attempt("WO-1") is first


def test_reused_execution_intent_is_rejected() -> None:
    admission = ExecutionAdmission()
    first = ready_work_order("WO-1")
    admission.accept(first, execution_request("WO-1", "shared-intent", "run-1"), at=_NOON)

    second = ready_work_order("WO-2")
    with pytest.raises(DuplicateExecutionAttemptError):
        admission.accept(second, execution_request("WO-2", "shared-intent", "run-2"))

    assert second.state is WorkOrderState.OPEN


def test_reused_dagster_run_id_is_rejected() -> None:
    admission = ExecutionAdmission()
    first = ready_work_order("WO-1")
    admission.accept(first, execution_request("WO-1", "intent-1", "shared-run"), at=_NOON)

    second = ready_work_order("WO-2")
    with pytest.raises(DuplicateExecutionAttemptError):
        admission.accept(second, execution_request("WO-2", "intent-2", "shared-run"))

    assert second.state is WorkOrderState.OPEN


def test_rejected_admission_records_no_correlation() -> None:
    admission = ExecutionAdmission()
    work_order = admitted_work_order()  # OPEN + UNKNOWN

    with pytest.raises(IneligibleWorkOrderError):
        admission.accept(work_order, execution_request(), at=_NOON)

    with pytest.raises(UnknownExecutionAttemptError):
        admission.attempt_for_intent("intent-1")
    with pytest.raises(UnknownExecutionAttemptError):
        admission.attempt_for_dagster_run("run-1")


# -- correlation round-trip -----------------------------------------------


def test_correlation_round_trip_work_order_intent_dagster_run() -> None:
    admission = ExecutionAdmission()
    work_order = ready_work_order()
    attempt = admission.accept(work_order, execution_request(), at=_NOON)

    by_work_order = admission.active_attempt("WO-1")
    by_intent = admission.attempt_for_intent("intent-1")
    by_run = admission.attempt_for_dagster_run("run-1")

    assert by_work_order is by_intent is by_run is attempt
    assert by_run.work_order_id == work_order.id
    assert by_run.intent_id == ExecutionIntentId("intent-1")
    assert by_run.dagster_run_id == DagsterRunId("run-1")


def test_unknown_correlation_lookups_fail_closed() -> None:
    admission = ExecutionAdmission()

    with pytest.raises(UnknownExecutionAttemptError):
        admission.attempt_for_dagster_run("missing-run")
    with pytest.raises(UnknownExecutionAttemptError):
        admission.attempt("missing-attempt")


# -- concluding an attempt ------------------------------------------------


def test_closed_attempt_allows_new_attempt_after_recovery_reopen() -> None:
    admission = ExecutionAdmission()
    work_order = ready_work_order()

    first = admission.accept(work_order, execution_request("WO-1", "intent-1", "run-1"), at=_NOON)
    admission.close_attempt(first.attempt_id)
    assert admission.active_attempt("WO-1") is None

    work_order.reopen(OpenStatus.READY, reason="semantic retry", at=_NOON)
    second = admission.accept(work_order, execution_request("WO-1", "intent-2", "run-2"), at=_NOON)

    assert second.attempt_id != first.attempt_id
    assert admission.active_attempt("WO-1") is second


def test_concluded_attempt_history_keeps_intent_and_run_unique() -> None:
    admission = ExecutionAdmission()
    first = ready_work_order("WO-1")
    attempt = admission.accept(first, execution_request("WO-1", "intent-1", "run-1"), at=_NOON)
    admission.close_attempt(attempt.attempt_id)

    # A concluded attempt stays recorded, so its intent/run cannot be reused.
    other = ready_work_order("WO-2")
    with pytest.raises(DuplicateExecutionAttemptError):
        admission.accept(other, execution_request("WO-2", "intent-1", "run-2"))
    with pytest.raises(DuplicateExecutionAttemptError):
        admission.accept(other, execution_request("WO-2", "intent-2", "run-1"))
