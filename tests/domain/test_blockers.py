"""Typed Work Order blockers and independent unblock semantics.

These tests pin the domain contract for issue #3:

* the typed reason vocabulary (including ``SOURCE_CHANGED`` and ``OTHER``);
* immutable blocker evidence, timestamps and typed resolution conditions;
* independent (partial) resolution of concurrent blockers;
* readiness re-evaluation that never makes an unevaluated Work Order executable;
* Dagster-owned capacity being busy is *not* a blocker;
* no failure lifecycle state, backend ranking or recovery selector.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import tactus.domain as domain
from tactus.domain import (
    AlreadyResolvedBlockerError,
    AuthenticatedHold,
    BlockReason,
    Blocker,
    BlockerId,
    BlockerSet,
    DuplicateBlockerError,
    IllegalTransitionError,
    InvalidBlockerError,
    InvalidReadinessError,
    OpenStatus,
    ReadinessFacts,
    ResolutionCondition,
    ResolutionKind,
    TransitionAuthority,
    UnknownBlockerError,
    WorkOrder,
    WorkOrderState,
    apply_readiness,
    default_resolution_condition,
    derive_blockers,
    evaluate_readiness,
    readiness_status,
)

_NOON = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
_LATER = datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)

_REQUIRED_REASONS = {
    "DEPENDENCY",
    "BACKEND_UNAVAILABLE",
    "HUMAN_INPUT_REQUIRED",
    "INTEGRATION_BLOCKED",
    "DELIVERY_BLOCKED",
    "SOURCE_CHANGED",
    "OTHER",
}


def open_work_order() -> WorkOrder:
    work_order = WorkOrder.create("WO-1")
    work_order.admit(reason="admitted", at=_NOON)
    return work_order


def blocker(reason: BlockReason, blocker_id: str, **kwargs: object) -> Blocker:
    return Blocker(
        reason=reason, blocker_id=blocker_id, blocked_at=_NOON, **kwargs  # type: ignore[arg-type]
    )


# -- reason vocabulary -----------------------------------------------------


def test_reason_vocabulary_covers_required_reasons() -> None:
    assert {reason.value for reason in BlockReason} == _REQUIRED_REASONS


def test_only_other_requires_detail() -> None:
    assert BlockReason.OTHER.requires_detail
    assert not BlockReason.DEPENDENCY.requires_detail


def test_other_without_detail_is_rejected() -> None:
    with pytest.raises(InvalidBlockerError):
        Blocker(reason=BlockReason.OTHER)


def test_other_with_detail_carries_explicit_resolution_condition() -> None:
    other = Blocker(
        reason=BlockReason.OTHER,
        detail="vendor API contract changed shape",
        blocked_at=_NOON,
    )

    assert other.resolution.kind is ResolutionKind.EXPLICIT
    assert "vendor API" in other.resolution.detail


# -- blocker records evidence, timestamps and typed resolution -------------


def test_blocker_records_evidence_timestamp_and_typed_resolution() -> None:
    depend = blocker(
        BlockReason.DEPENDENCY,
        "b1",
        detail="WO-2 not merged",
        evidence={"upstream": "WO-2"},
    )

    assert depend.blocked_at == _NOON
    assert depend.evidence == {"upstream": "WO-2"}
    assert depend.resolution.kind is ResolutionKind.DEPENDENCY_RESOLVED
    assert not depend.is_resolved


def test_every_reason_has_a_typed_default_resolution_condition() -> None:
    for reason in BlockReason:
        condition = default_resolution_condition(reason, detail="why")
        assert isinstance(condition, ResolutionCondition)
        assert isinstance(condition.kind, ResolutionKind)

    assert (
        default_resolution_condition(BlockReason.SOURCE_CHANGED).kind
        is ResolutionKind.SOURCE_REFRESHED
    )


def test_explicit_resolution_condition_requires_detail() -> None:
    with pytest.raises(InvalidBlockerError):
        ResolutionCondition(kind=ResolutionKind.EXPLICIT)

    condition = ResolutionCondition(kind=ResolutionKind.EXPLICIT, detail="do the thing")
    assert condition.kind is ResolutionKind.EXPLICIT


def test_resolved_sets_timestamp_and_authority_without_losing_evidence() -> None:
    depend = blocker(BlockReason.DEPENDENCY, "b1", evidence={"upstream": "WO-2"})

    resolved = depend.resolve(at=_LATER, by=TransitionAuthority.OPERATOR, note="merged")

    assert resolved.is_resolved
    assert resolved.resolved_at == _LATER
    assert resolved.resolved_by == TransitionAuthority.OPERATOR.value
    assert resolved.resolution_note == "merged"
    assert resolved.evidence == {"upstream": "WO-2"}
    # The original remains an immutable record.
    assert not depend.is_resolved


def test_double_resolution_is_rejected() -> None:
    resolved = blocker(BlockReason.DEPENDENCY, "b1").resolve(
        at=_LATER, by=TransitionAuthority.OPERATOR
    )

    with pytest.raises(AlreadyResolvedBlockerError):
        resolved.resolve(at=_LATER, by=TransitionAuthority.OPERATOR)


# -- concurrent blockers and independent unblock ---------------------------


def test_concurrent_blockers_hold_readiness_blocked() -> None:
    work_order = open_work_order()
    dependency = work_order.add_blocker(blocker(BlockReason.DEPENDENCY, "b1"))
    backend = work_order.add_blocker(blocker(BlockReason.BACKEND_UNAVAILABLE, "b2"))

    assert work_order.readiness is OpenStatus.BLOCKED
    assert work_order.active_blockers == (dependency, backend)
    with pytest.raises(IllegalTransitionError):
        work_order.claim(reason="claimed", at=_NOON)


def test_resolving_one_blocker_leaves_the_other_active() -> None:
    work_order = open_work_order()
    dependency = work_order.add_blocker(blocker(BlockReason.DEPENDENCY, "b1"))
    work_order.add_blocker(blocker(BlockReason.BACKEND_UNAVAILABLE, "b2"))

    work_order.resolve_blocker(
        dependency.blocker_id, at=_LATER, authority=TransitionAuthority.OPERATOR
    )

    assert work_order.readiness is OpenStatus.BLOCKED
    assert {b.reason for b in work_order.active_blockers} == {
        BlockReason.BACKEND_UNAVAILABLE
    }
    assert work_order.blockers[0].is_resolved
    with pytest.raises(IllegalTransitionError):
        work_order.claim(reason="claimed", at=_LATER)


def test_resolving_the_last_blocker_recomputes_readiness_ready() -> None:
    work_order = open_work_order()
    first = work_order.add_blocker(blocker(BlockReason.DEPENDENCY, "b1"))
    second = work_order.add_blocker(blocker(BlockReason.SOURCE_CHANGED, "b2"))

    work_order.resolve_blocker(first.blocker_id, at=_LATER)
    assert work_order.readiness is OpenStatus.BLOCKED

    work_order.resolve_blocker(second.blocker_id, at=_LATER)

    assert work_order.readiness is OpenStatus.READY
    assert work_order.active_blockers == ()
    record = work_order.claim(reason="claimed", at=_LATER)
    assert record.to_state is WorkOrderState.ACTIVE


def test_resolving_unknown_blocker_raises() -> None:
    work_order = open_work_order()
    work_order.add_blocker(blocker(BlockReason.DEPENDENCY, "b1"))

    with pytest.raises(UnknownBlockerError):
        work_order.resolve_blocker("missing", at=_LATER)


def test_duplicate_blocker_identity_is_rejected() -> None:
    work_order = open_work_order()
    work_order.add_blocker(blocker(BlockReason.DEPENDENCY, "b1"))

    with pytest.raises(DuplicateBlockerError):
        work_order.add_blocker(blocker(BlockReason.SOURCE_CHANGED, "b1"))


# -- readiness re-evaluation is the only path to READY ---------------------


def test_set_readiness_ready_is_refused_while_a_blocker_is_active() -> None:
    work_order = open_work_order()
    work_order.add_blocker(blocker(BlockReason.SOURCE_CHANGED, "b1"))

    with pytest.raises(InvalidReadinessError):
        work_order.set_readiness(OpenStatus.READY)

    assert work_order.readiness is OpenStatus.BLOCKED


def test_recompute_readiness_never_yields_unknown() -> None:
    work_order = open_work_order()
    assert work_order.readiness is OpenStatus.UNKNOWN

    # Re-evaluation with no blockers resolves to READY, not UNKNOWN.
    assert work_order.recompute_readiness() is OpenStatus.READY


def test_unknown_never_admits_execution() -> None:
    work_order = open_work_order()
    assert work_order.readiness is OpenStatus.UNKNOWN

    with pytest.raises(IllegalTransitionError):
        work_order.claim(reason="claimed", at=_NOON)

    assert work_order.state is WorkOrderState.OPEN


def test_blocker_rejected_outside_open() -> None:
    draft = WorkOrder.create("WO-1")
    with pytest.raises(InvalidReadinessError):
        draft.add_blocker(blocker(BlockReason.DEPENDENCY, "b1"))

    active = open_work_order()
    active.set_readiness(OpenStatus.READY)
    active.claim(reason="claimed", at=_NOON)
    with pytest.raises(InvalidReadinessError):
        active.add_blocker(blocker(BlockReason.DEPENDENCY, "b2"))


# -- facts -> typed blockers -----------------------------------------------


def test_stale_source_produces_a_source_changed_blocker() -> None:
    facts = ReadinessFacts(dependencies_satisfied=True, source_current=False)

    derived = derive_blockers(facts, at=_NOON)

    assert len(derived) == 1
    stale = derived[0]
    assert stale.reason is BlockReason.SOURCE_CHANGED
    assert stale.resolution.kind is ResolutionKind.SOURCE_REFRESHED
    assert stale.blocked_at == _NOON

    work_order = open_work_order()
    evaluation = apply_readiness(work_order, facts, at=_NOON)
    assert evaluation.status is OpenStatus.BLOCKED
    assert work_order.readiness is OpenStatus.BLOCKED
    assert work_order.active_blockers[0].reason is BlockReason.SOURCE_CHANGED


def test_busy_capacity_alone_is_not_a_blocker() -> None:
    facts = ReadinessFacts(
        dependencies_satisfied=True,
        backend_available=True,
        route_available=True,
        source_current=True,
        capacity_busy=True,  # Dagster-owned; must be ignored
    )

    assert derive_blockers(facts, at=_NOON) == ()

    work_order = open_work_order()
    evaluation = apply_readiness(work_order, facts, at=_NOON)

    assert evaluation.status is OpenStatus.READY
    assert evaluation.blockers == ()
    assert work_order.readiness is OpenStatus.READY


def test_facts_cover_dependency_backend_human_integration_and_delivery() -> None:
    facts = ReadinessFacts(
        dependencies_satisfied=False,
        backend_available=False,
        integration_ready=False,
        delivery_ready=False,
        approval_required=True,
    )

    reasons = {b.reason for b in derive_blockers(facts, at=_NOON)}

    assert reasons == {
        BlockReason.DEPENDENCY,
        BlockReason.BACKEND_UNAVAILABLE,
        BlockReason.HUMAN_INPUT_REQUIRED,
        BlockReason.INTEGRATION_BLOCKED,
        BlockReason.DELIVERY_BLOCKED,
    }


def test_unset_facts_alone_do_not_block() -> None:
    assert derive_blockers(ReadinessFacts(), at=_NOON) == ()
    assert readiness_status(()) is OpenStatus.READY


def test_no_validated_route_produces_backend_unavailable() -> None:
    derived = derive_blockers(ReadinessFacts(route_available=False), at=_NOON)

    assert [b.reason for b in derived] == [BlockReason.BACKEND_UNAVAILABLE]
    assert derived[0].subreason == "NO_ROUTE"
    assert derived[0].resolution.kind is ResolutionKind.BACKEND_AVAILABLE


def test_authenticated_hold_becomes_a_typed_blocker() -> None:
    hold = AuthenticatedHold(
        reason=BlockReason.HUMAN_INPUT_REQUIRED,
        subreason="APPROVAL_REQUIRED",
        detail="security review required",
        evidence={"ticket": "SEC-9"},
        held_at=_NOON,
        authenticated_by="operator:sam",
    )

    work_order = open_work_order()
    evaluation = evaluate_readiness(
        ReadinessFacts(dependencies_satisfied=True, holds=(hold,)), at=_NOON
    )
    assert evaluation.status is OpenStatus.BLOCKED

    work_order.replace_blockers(evaluation.blockers)
    assert work_order.readiness is OpenStatus.BLOCKED
    held = work_order.active_blockers[0]
    assert held.reason is BlockReason.HUMAN_INPUT_REQUIRED
    assert held.subreason == "APPROVAL_REQUIRED"
    assert held.evidence == {"ticket": "SEC-9"}


def test_replace_blockers_is_independent_of_previous_resolution() -> None:
    work_order = open_work_order()
    stale = work_order.add_blocker(blocker(BlockReason.SOURCE_CHANGED, "b1"))
    work_order.resolve_blocker(stale.blocker_id, at=_LATER)
    assert work_order.readiness is OpenStatus.READY

    # A fresh evaluation re-materializes the stale-source blocker.
    evaluation = apply_readiness(
        work_order, ReadinessFacts(source_current=False), at=_LATER
    )
    assert evaluation.status is OpenStatus.BLOCKED
    assert work_order.readiness is OpenStatus.BLOCKED
    assert len(work_order.active_blockers) == 1


def test_blocker_set_replace_rejects_duplicate_ids_atomically() -> None:
    blocker_set = BlockerSet([blocker(BlockReason.DEPENDENCY, "b1")])

    with pytest.raises(DuplicateBlockerError):
        blocker_set.replace(
            [blocker(BlockReason.DEPENDENCY, "b2"), blocker(BlockReason.SOURCE_CHANGED, "b2")]
        )

    assert [b.blocker_id for b in blocker_set.all] == [BlockerId("b1")]


# -- architecture guards ---------------------------------------------------


def test_no_failure_lifecycle_state_is_introduced() -> None:
    assert {state.value for state in WorkOrderState} == {
        "DRAFT",
        "OPEN",
        "ACTIVE",
        "IMPLEMENTED",
        "RETIRED",
    }
    assert "FAILED" not in {state.value for state in WorkOrderState}


def test_blocker_value_types_are_frozen() -> None:
    import dataclasses

    assert dataclasses.is_dataclass(Blocker)
    assert Blocker.__dataclass_params__.frozen
    assert dataclasses.is_dataclass(ResolutionCondition)
    assert ResolutionCondition.__dataclass_params__.frozen


_FORBIDDEN_POLICY_PREFIXES = (
    "rank",
    "select",
    "choose",
    "best",
    "prefer",
    "recovery_selector",
    "select_recovery",
)


def test_domain_blocker_api_has_no_backend_ranking_or_recovery_selector() -> None:
    for name in domain.__all__:
        lowered = name.lower()
        for banned in _FORBIDDEN_POLICY_PREFIXES:
            assert banned not in lowered, (
                f"tactus.domain must not expose selection policy: {name}"
            )
