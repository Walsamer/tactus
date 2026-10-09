"""Decision application tests: explicit mapping, fail-closed, audit, invariants.

These tests assert the Tactus-owned mapping from every frozen generic Ictus
decision to an explicit lifecycle/readiness effect, that unknown inputs fail
closed, that lifecycle/readiness invariants survive, that application is
auditable and that no decision *policy* lives in Tactus.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import tactus.domain.decision_application as decision_application
from tactus.domain import (
    DECISION_EFFECTS,
    ChildCreationError,
    ChildCreationUnavailableError,
    ChildPlan,
    ChildSpec,
    DecisionApplicationError,
    DecisionApplier,
    DecisionEffect,
    DecisionKind,
    HumanInterventionRequest,
    InMemoryChildCreation,
    InterventionReason,
    MissingChildPlanError,
    OpenStatus,
    RouteConstraints,
    SemanticDecision,
    TransitionAuthority,
    WorkOrder,
    WorkOrderState,
)

_NOON = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def active_work_order(work_order_id: str = "WO-1") -> WorkOrder:
    work_order = WorkOrder.create(work_order_id)
    work_order.admit(reason="admitted", at=_NOON)
    work_order.set_readiness(OpenStatus.READY)
    work_order.claim(reason="claimed", at=_NOON)
    return work_order


def decision(kind: DecisionKind) -> SemanticDecision:
    if kind.requires_capability:
        return SemanticDecision(decision_id=f"p-{kind.value}", kind=kind, capability="demo.verify")
    return SemanticDecision(decision_id=f"p-{kind.value}", kind=kind)


# -- mapping is explicit, total and closed ---------------------------------


def test_every_decision_kind_has_exactly_one_effect() -> None:
    assert set(DECISION_EFFECTS) == set(DecisionKind)


def test_effects_are_distinct_per_decision() -> None:
    assert len(set(DECISION_EFFECTS.values())) == len(DecisionKind)


def test_no_new_lifecycle_state_is_invented() -> None:
    effects = set(DECISION_EFFECTS.values())
    assert effects == set(DecisionEffect)


# -- per-decision effects --------------------------------------------------


def test_reexecute_ends_attempt_and_reopens_ready() -> None:
    work_order = active_work_order()
    record = DecisionApplier().apply(work_order, decision(DecisionKind.REEXECUTE), at=_NOON)

    assert work_order.state is WorkOrderState.OPEN
    assert work_order.readiness is OpenStatus.READY
    assert record.effect is DecisionEffect.REOPEN_READY_NEW_ATTEMPT
    assert record.from_state is WorkOrderState.ACTIVE
    assert record.to_state is WorkOrderState.OPEN
    assert record.to_readiness is OpenStatus.READY
    assert record.transitions[0].authority is TransitionAuthority.RECOVERY
    assert record.transitions[0].occurred_at == _NOON


def test_route_reopens_ready_and_preserves_constraints() -> None:
    work_order = active_work_order()
    route = RouteConstraints(exclude_backend="backend-b", required_provider="provider-a")
    routed = SemanticDecision(
        decision_id="p-route", kind=DecisionKind.ROUTE, capability="demo.verify", route=route
    )

    record = DecisionApplier().apply(work_order, routed, at=_NOON)

    assert work_order.state is WorkOrderState.OPEN
    assert work_order.readiness is OpenStatus.READY
    assert record.effect is DecisionEffect.REOPEN_READY_ROUTED_ATTEMPT
    assert record.route == route
    # Preserved verbatim; Tactus performs no backend selection or ranking.
    assert record.route.exclude_backend == "backend-b"


def test_execute_capability_reopens_ready_with_validated_capability() -> None:
    work_order = active_work_order()
    follow_up = SemanticDecision(
        decision_id="p-exec",
        kind=DecisionKind.EXECUTE_CAPABILITY,
        capability="data.quality_check",
    )

    record = DecisionApplier().apply(work_order, follow_up, at=_NOON)

    assert work_order.state is WorkOrderState.OPEN
    assert work_order.readiness is OpenStatus.READY
    assert record.effect is DecisionEffect.REOPEN_READY_FOLLOW_UP_CAPABILITY
    assert record.capability == "data.quality_check"


def test_escalate_blocks_open_and_emits_typed_intervention() -> None:
    work_order = active_work_order()
    record = DecisionApplier().apply(work_order, decision(DecisionKind.ESCALATE), at=_NOON)

    assert work_order.state is WorkOrderState.OPEN
    assert work_order.readiness is OpenStatus.BLOCKED
    assert record.effect is DecisionEffect.REOPEN_BLOCKED_INTERVENTION

    intervention = record.intervention
    assert isinstance(intervention, HumanInterventionRequest)
    assert intervention.work_order_id == work_order.id
    assert intervention.reason is InterventionReason.UNRESOLVED_RECOVERY
    assert intervention.created_at == _NOON


def test_escalate_intervention_reason_can_be_typed_by_the_application() -> None:
    work_order = active_work_order()
    record = DecisionApplier().apply(
        work_order,
        decision(DecisionKind.ESCALATE),
        intervention_reason=InterventionReason.APPROVAL_REQUIRED,
        at=_NOON,
    )
    assert record.intervention is not None
    assert record.intervention.reason is InterventionReason.APPROVAL_REQUIRED


def test_abort_retires_active_work_order() -> None:
    work_order = active_work_order()
    record = DecisionApplier().apply(work_order, decision(DecisionKind.ABORT), at=_NOON)

    assert work_order.state is WorkOrderState.RETIRED
    assert work_order.is_terminal
    assert record.effect is DecisionEffect.RETIRE_ABORTED
    assert record.to_state is WorkOrderState.RETIRED
    assert record.to_readiness is None


# -- DECOMPOSE: children first, parent retired last ------------------------


def test_decompose_creates_children_then_retires_parent() -> None:
    work_order = active_work_order()
    creation = InMemoryChildCreation()
    applier = DecisionApplier(child_creation=creation)
    plan = ChildPlan(
        children=(ChildSpec("WO-1a"), ChildSpec("WO-1b", title="child b")),
        max_children=4,
    )

    record = applier.apply(
        work_order, decision(DecisionKind.DECOMPOSE), child_plan=plan, at=_NOON
    )

    assert work_order.state is WorkOrderState.RETIRED
    assert record.effect is DecisionEffect.RETIRE_DECOMPOSED
    children = creation.children_of("WO-1")
    assert [child.id.value for child in children] == ["WO-1a", "WO-1b"]
    assert record.created_children == tuple(child.id for child in children)
    assert all(child.state is WorkOrderState.DRAFT for child in children)


def test_decompose_requires_a_bounded_child_plan() -> None:
    work_order = active_work_order()
    applier = DecisionApplier(child_creation=InMemoryChildCreation())

    with pytest.raises(MissingChildPlanError):
        applier.apply(work_order, decision(DecisionKind.DECOMPOSE), at=_NOON)

    assert work_order.state is WorkOrderState.ACTIVE


def test_decompose_requires_a_durable_creation_capability() -> None:
    work_order = active_work_order()
    plan = ChildPlan(children=(ChildSpec("WO-1a"),), max_children=2)

    with pytest.raises(ChildCreationUnavailableError):
        DecisionApplier().apply(
            work_order, decision(DecisionKind.DECOMPOSE), child_plan=plan, at=_NOON
        )

    assert work_order.state is WorkOrderState.ACTIVE


def test_decompose_does_not_retire_parent_when_creation_fails() -> None:
    class FailingCreation:
        def create_children(self, parent: WorkOrder, plan: ChildPlan) -> tuple[WorkOrder, ...]:
            raise ChildCreationError("durable store unavailable")

    work_order = active_work_order()
    plan = ChildPlan(children=(ChildSpec("WO-1a"),), max_children=2)

    with pytest.raises(ChildCreationError):
        DecisionApplier(child_creation=FailingCreation()).apply(
            work_order, decision(DecisionKind.DECOMPOSE), child_plan=plan, at=_NOON
        )

    assert work_order.state is WorkOrderState.ACTIVE


def test_decompose_does_not_retire_parent_when_creation_does_not_match_plan() -> None:
    class WrongCreation:
        def create_children(self, parent: WorkOrder, plan: ChildPlan) -> tuple[WorkOrder, ...]:
            return (WorkOrder.create("WO-other"),)

    work_order = active_work_order()
    plan = ChildPlan(children=(ChildSpec("WO-1a"),), max_children=2)

    with pytest.raises(ChildCreationError):
        DecisionApplier(child_creation=WrongCreation()).apply(
            work_order, decision(DecisionKind.DECOMPOSE), child_plan=plan, at=_NOON
        )

    # The parent was retired by the guard-checked op only after validation, so a
    # mismatched creation must leave it ACTIVE.
    assert work_order.state is WorkOrderState.ACTIVE


# -- fail-closed guard: only ACTIVE work orders ----------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        DecisionKind.REEXECUTE,
        DecisionKind.ROUTE,
        DecisionKind.EXECUTE_CAPABILITY,
        DecisionKind.ESCALATE,
        DecisionKind.ABORT,
        DecisionKind.DECOMPOSE,
    ],
)
def test_decisions_require_an_active_work_order(kind: DecisionKind) -> None:
    work_order = WorkOrder.create("WO-1")  # DRAFT, not ACTIVE
    before = work_order.state

    with pytest.raises(decision_application.WorkOrderNotActiveError):
        DecisionApplier(child_creation=InMemoryChildCreation()).apply(
            work_order, decision(kind), at=_NOON
        )

    assert work_order.state is before
    assert work_order.transitions == ()


def test_non_work_order_or_non_decision_input_fails_closed() -> None:
    work_order = active_work_order()
    applier = DecisionApplier()

    with pytest.raises(DecisionApplicationError):
        applier.apply("not-a-work-order", decision(DecisionKind.ABORT), at=_NOON)  # type: ignore[arg-type]

    with pytest.raises(DecisionApplicationError):
        applier.apply(work_order, "ABORT", at=_NOON)  # type: ignore[arg-type]

    assert work_order.state is WorkOrderState.ACTIVE


# -- auditability ----------------------------------------------------------


def test_application_is_auditable_and_ledgered() -> None:
    work_order = active_work_order()
    applier = DecisionApplier()

    record = applier.apply(work_order, decision(DecisionKind.REEXECUTE), at=_NOON)

    assert applier.applications == (record,)
    assert record.application_id
    assert record.decision_id == "p-REEXECUTE"
    assert record.work_order_id == work_order.id
    assert record.applied_at == _NOON
    assert record.from_state is WorkOrderState.ACTIVE
    assert record.to_state is WorkOrderState.OPEN
    assert record.from_readiness is None
    assert record.to_readiness is OpenStatus.READY


def test_record_is_immutable() -> None:
    work_order = active_work_order()
    record = DecisionApplier().apply(work_order, decision(DecisionKind.ABORT), at=_NOON)

    with pytest.raises(Exception):
        record.effect = DecisionEffect.RETIRE_ABORTED  # type: ignore[misc]


# -- invariants ------------------------------------------------------------


def test_readiness_is_none_outside_open_after_retire() -> None:
    work_order = active_work_order()
    DecisionApplier().apply(work_order, decision(DecisionKind.ABORT), at=_NOON)
    assert work_order.readiness is None


def test_decision_application_records_a_lifecycle_transition() -> None:
    work_order = active_work_order()
    before = len(work_order.transitions)

    record = DecisionApplier().apply(work_order, decision(DecisionKind.REEXECUTE), at=_NOON)

    assert len(work_order.transitions) == before + 1
    assert work_order.transitions[-1] is record.transitions[0]


# -- no decision policy ----------------------------------------------------


@pytest.mark.parametrize(
    "forbidden",
    [
        "decide",
        "choose_decision",
        "diagnose",
        "select_backend",
        "rank_backends",
        "retry_budget",
        "recovery_policy",
        "to_decision",
    ],
)
def test_module_has_no_decision_policy_surface(forbidden: str) -> None:
    assert not hasattr(decision_application, forbidden)
