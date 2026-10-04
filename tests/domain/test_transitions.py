from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tactus.domain import (
    IllegalTransitionError,
    InvalidReadinessError,
    OpenStatus,
    TransitionAuthority,
    WorkOrder,
    WorkOrderState,
)

_NOON = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def open_work_order() -> WorkOrder:
    work_order = WorkOrder.create("WO-1")
    work_order.admit(reason="admitted", at=_NOON)
    return work_order


def ready_work_order() -> WorkOrder:
    work_order = open_work_order()
    work_order.set_readiness(OpenStatus.READY)
    return work_order


def active_work_order() -> WorkOrder:
    work_order = ready_work_order()
    work_order.claim(reason="claimed", at=_NOON)
    return work_order


# -- admission ------------------------------------------------------------


def test_admit_moves_draft_to_open_with_unknown_readiness() -> None:
    work_order = WorkOrder.create("WO-1")

    record = work_order.admit(reason="admitted", at=_NOON)

    assert work_order.state is WorkOrderState.OPEN
    assert work_order.readiness is OpenStatus.UNKNOWN
    assert record.from_state is WorkOrderState.DRAFT
    assert record.to_state is WorkOrderState.OPEN


def test_transition_record_carries_provenance() -> None:
    work_order = WorkOrder.create("WO-1")

    record = work_order.admit(reason="spec complete", at=_NOON)

    assert record.reason == "spec complete"
    assert record.authority is TransitionAuthority.ADMISSION
    assert record.occurred_at == _NOON
    assert record.transition_id
    assert work_order.transitions == (record,)


def test_withdraw_retires_draft() -> None:
    work_order = WorkOrder.create("WO-1")

    record = work_order.withdraw(reason="no longer needed", at=_NOON)

    assert work_order.state is WorkOrderState.RETIRED
    assert record.authority is TransitionAuthority.OPERATOR


# -- claiming -------------------------------------------------------------


def test_claim_requires_open_plus_ready() -> None:
    work_order = open_work_order()
    assert work_order.readiness is OpenStatus.UNKNOWN

    with pytest.raises(IllegalTransitionError):
        work_order.claim(reason="claimed", at=_NOON)

    assert work_order.state is WorkOrderState.OPEN


def test_claim_rejected_when_blocked() -> None:
    work_order = open_work_order()
    work_order.set_readiness(OpenStatus.BLOCKED)

    with pytest.raises(IllegalTransitionError):
        work_order.claim(reason="claimed", at=_NOON)


def test_claim_from_ready_becomes_active() -> None:
    work_order = ready_work_order()

    record = work_order.claim(reason="capacity available", at=_NOON)

    assert work_order.state is WorkOrderState.ACTIVE
    assert work_order.readiness is None
    assert record.authority is TransitionAuthority.SCHEDULER


# -- completion -----------------------------------------------------------


def test_complete_only_from_active() -> None:
    work_order = ready_work_order()

    with pytest.raises(IllegalTransitionError):
        work_order.complete(reason="done", at=_NOON)


def test_complete_from_active_implemented() -> None:
    work_order = active_work_order()

    record = work_order.complete(reason="verified", at=_NOON)

    assert work_order.state is WorkOrderState.IMPLEMENTED
    assert work_order.is_terminal
    assert record.authority is TransitionAuthority.VERIFIER


# -- recovery reopen ------------------------------------------------------


def test_reopen_from_active_sets_readiness() -> None:
    work_order = active_work_order()

    record = work_order.reopen(OpenStatus.BLOCKED, reason="escalated", at=_NOON)

    assert work_order.state is WorkOrderState.OPEN
    assert work_order.readiness is OpenStatus.BLOCKED
    assert record.authority is TransitionAuthority.RECOVERY


def test_reopen_rejects_unknown_readiness() -> None:
    work_order = active_work_order()

    with pytest.raises(InvalidReadinessError):
        work_order.reopen(OpenStatus.UNKNOWN, reason="escalated", at=_NOON)


# -- retirement -----------------------------------------------------------


def test_retire_from_open_and_active() -> None:
    open_order = ready_work_order()
    open_order.retire(reason="superseded", at=_NOON)
    assert open_order.state is WorkOrderState.RETIRED

    active_order = active_work_order()
    active_order.retire(reason="superseded", at=_NOON)
    assert active_order.state is WorkOrderState.RETIRED


# -- illegal transitions --------------------------------------------------


@pytest.mark.parametrize(
    ("start", "attempt"),
    [
        ("draft", "claim"),
        ("draft", "complete"),
        ("open", "complete"),
        ("implemented", "retire"),
        ("retired", "admit"),
    ],
)
def test_illegal_transitions_are_rejected(start: str, attempt: str) -> None:
    if start == "draft":
        work_order = WorkOrder.create("WO-1")
    elif start == "open":
        work_order = ready_work_order()
    elif start == "implemented":
        work_order = active_work_order()
        work_order.complete(reason="verified", at=_NOON)
    else:
        work_order = WorkOrder.create("WO-1")
        work_order.withdraw(reason="cancelled", at=_NOON)

    actions = {
        "claim": lambda: work_order.claim(reason="x", at=_NOON),
        "complete": lambda: work_order.complete(reason="x", at=_NOON),
        "retire": lambda: work_order.retire(reason="x", at=_NOON),
        "admit": lambda: work_order.admit(reason="x", at=_NOON),
    }

    before = work_order.state
    with pytest.raises(IllegalTransitionError):
        actions[attempt]()
    assert work_order.state is before


def test_terminal_states_have_no_exit() -> None:
    implemented = active_work_order()
    implemented.complete(reason="verified", at=_NOON)
    assert implemented.is_terminal

    retired = WorkOrder.create("WO-2")
    retired.withdraw(reason="cancelled", at=_NOON)
    assert retired.is_terminal


# -- readiness is not a lifecycle transition ------------------------------


def test_readiness_change_is_not_a_transition() -> None:
    work_order = open_work_order()
    before = work_order.transitions

    work_order.set_readiness(OpenStatus.READY)
    work_order.set_readiness(OpenStatus.BLOCKED)
    work_order.set_readiness(OpenStatus.READY)

    assert work_order.state is WorkOrderState.OPEN
    assert work_order.transitions == before


def test_readiness_rejected_outside_open() -> None:
    work_order = WorkOrder.create("WO-1")
    with pytest.raises(InvalidReadinessError):
        work_order.set_readiness(OpenStatus.READY)

    active = active_work_order()
    with pytest.raises(InvalidReadinessError):
        active.set_readiness(OpenStatus.READY)


# -- operations are guarded by their own source state ---------------------


def test_admit_only_from_draft() -> None:
    work_order = WorkOrder.create("WO-1")
    work_order.admit(reason="admitted", at=_NOON)

    with pytest.raises(IllegalTransitionError):
        work_order.admit(reason="admitted again", at=_NOON)

    active = active_work_order()
    with pytest.raises(IllegalTransitionError):
        active.admit(reason="still active", at=_NOON)


def test_withdraw_only_from_draft() -> None:
    work_order = open_work_order()

    with pytest.raises(IllegalTransitionError):
        work_order.withdraw(reason="too late", at=_NOON)


def test_retire_rejected_from_draft() -> None:
    work_order = WorkOrder.create("WO-1")

    with pytest.raises(IllegalTransitionError):
        work_order.retire(reason="use withdraw", at=_NOON)


def test_reopen_only_from_active() -> None:
    draft = WorkOrder.create("WO-1")
    with pytest.raises(IllegalTransitionError):
        draft.reopen(OpenStatus.READY, reason="not active", at=_NOON)

    open_order = ready_work_order()
    with pytest.raises(IllegalTransitionError):
        open_order.reopen(OpenStatus.READY, reason="not active", at=_NOON)
