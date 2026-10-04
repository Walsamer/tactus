from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tactus.domain import (
    FailureObservation,
    IllegalTransitionError,
    OpenStatus,
    WorkOrder,
    WorkOrderId,
    WorkOrderState,
)

_NOON = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_work_order_id_requires_non_empty_value() -> None:
    with pytest.raises(ValueError):
        WorkOrderId("")
    with pytest.raises(ValueError):
        WorkOrderId("   ")


def test_work_order_id_strips_whitespace() -> None:
    assert WorkOrderId("  WO-1  ").value == "WO-1"


def test_work_order_id_is_hashable_value() -> None:
    assert WorkOrderId("WO-1") == WorkOrderId("WO-1")
    assert len({WorkOrderId("WO-1"), WorkOrderId("WO-1")}) == 1


def test_create_starts_in_draft_without_readiness() -> None:
    work_order = WorkOrder.create("WO-1", title="Do the thing")

    assert work_order.id == WorkOrderId("WO-1")
    assert work_order.title == "Do the thing"
    assert work_order.state is WorkOrderState.DRAFT
    assert work_order.readiness is None
    assert work_order.transitions == ()
    assert work_order.failure_observations == ()
    assert not work_order.is_terminal


def test_failure_observations_accumulate_and_do_not_change_state() -> None:
    work_order = WorkOrder.create("WO-1")
    work_order.admit(reason="admitted", at=_NOON)
    work_order.set_readiness(OpenStatus.READY)
    work_order.claim(reason="claimed", at=_NOON)

    first = FailureObservation(summary="boom", observed_at=_NOON)
    second = FailureObservation(summary="boom again", observed_at=_NOON)
    work_order.record_failure(first)
    work_order.record_failure(second)

    assert work_order.state is WorkOrderState.ACTIVE
    assert work_order.failure_observations == (first, second)


def test_record_failure_rejected_unless_active() -> None:
    work_order = WorkOrder.create("WO-1")

    with pytest.raises(IllegalTransitionError):
        work_order.record_failure(FailureObservation(summary="boom"))
