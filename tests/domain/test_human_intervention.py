"""Human-intervention domain contract tests (issue #11).

These tests pin the Tactus-owned human-intervention seam:

* a resolution is an authenticated typed command bound to one request and to the
  exact source/domain revision;
* expired, wrong-request, unpermitted-action, scope-widening, route-change and
  stale-binding resolutions fail closed;
* applying a resolution recomputes readiness from the complete facts, so
  unrelated blockers, dependency holds and budget constraints survive and no
  fresh Ictus decision is fabricated.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from tactus.domain import (
    AdmissionFacts,
    DecisionApplier,
    DecisionApplicationError,
    DecisionKind,
    ExecutionAdmission,
    HumanAction,
    HumanInterventionError,
    HumanInterventionRequest,
    HumanResolution,
    InterventionReason,
    OpenStatus,
    ReadinessFacts,
    SemanticDecision,
    SourceRevision,
    WorkOrder,
    WorkOrderId,
    WorkOrderRevision,
    WorkOrderState,
    apply_resolution_to_readiness,
    apply_readiness,
    validate_resolution,
)
from tactus.domain.human_intervention import (
    ActionNotPermittedError,
    ExpiredRequestError,
    ResolutionRevokedError,
    RouteChangeNotAuthorizedError,
    ScopeWideningError,
    StaleResolutionBindingError,
    WrongRequestError,
)

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
_LATER = _NOW + timedelta(hours=1)
_SOURCE = SourceRevision("src-rev-1")
_REVISION = WorkOrderRevision(7)


def request(**overrides: object) -> HumanInterventionRequest:
    defaults: dict[str, object] = dict(
        intervention_id="req-1",
        work_order_id=WorkOrderId("WO-1"),
        reason=InterventionReason.APPROVAL_REQUIRED,
        summary="approve the tricky migration",
        permitted_actions=(HumanAction.APPROVE, HumanAction.REJECT),
        scope=("src/**",),
        source_revision=_SOURCE,
        work_order_revision=_REVISION,
        created_at=_NOW,
    )
    defaults.update(overrides)
    return HumanInterventionRequest(**defaults)  # type: ignore[arg-type]


def resolution(**overrides: object) -> HumanResolution:
    defaults: dict[str, object] = dict(
        resolution_id="res-1",
        intervention_id="req-1",
        work_order_id=WorkOrderId("WO-1"),
        actor="operator-1",
        action=HumanAction.APPROVE,
        resolved_at=_NOW,
        scope=("src/**",),
        source_revision=_SOURCE,
        work_order_revision=_REVISION,
    )
    defaults.update(overrides)
    return HumanResolution(**defaults)  # type: ignore[arg-type]


def validate(
    req: HumanInterventionRequest,
    res: HumanResolution,
    *,
    source: SourceRevision = _SOURCE,
    revision: WorkOrderRevision = _REVISION,
    now: datetime = _NOW,
) -> None:
    validate_resolution(
        req,
        res,
        current_source_revision=source,
        current_work_order_revision=revision,
        now=now,
    )


# -- request shape ----------------------------------------------------------


def test_request_is_a_typed_bounded_value() -> None:
    req = request()
    assert req.reason is InterventionReason.APPROVAL_REQUIRED
    assert req.permitted_actions == (HumanAction.APPROVE, HumanAction.REJECT)
    assert req.scope == ("src/**",)
    assert req.source_revision == _SOURCE
    assert req.work_order_revision == _REVISION


def test_request_rejects_an_inverted_expiry_window() -> None:
    with pytest.raises(HumanInterventionError):
        request(expires_at=_NOW - timedelta(seconds=1))


def test_request_must_permit_at_least_one_action() -> None:
    with pytest.raises(HumanInterventionError):
        request(permitted_actions=())


# -- fail-closed validation -------------------------------------------------


def test_valid_approval_resolution_passes() -> None:
    validate(request(), resolution())


def test_wrong_request_is_rejected() -> None:
    with pytest.raises(WrongRequestError):
        validate(request(), resolution(intervention_id="other-request"))


def test_wrong_work_order_is_rejected() -> None:
    with pytest.raises(WrongRequestError):
        validate(request(), resolution(work_order_id=WorkOrderId("WO-2")))


def test_expired_request_is_rejected() -> None:
    req = request(expires_at=_NOW + timedelta(minutes=5))
    with pytest.raises(ExpiredRequestError):
        validate(req, resolution(), now=_NOW + timedelta(minutes=6))


def test_unpermitted_action_is_rejected() -> None:
    req = request(permitted_actions=(HumanAction.REJECT,))
    with pytest.raises(ActionNotPermittedError):
        validate(req, resolution(action=HumanAction.APPROVE))


def test_resolution_cannot_widen_scope_without_explicit_authorization() -> None:
    with pytest.raises(ScopeWideningError):
        validate(request(), resolution(scope=("src/**", "docs/**", "infra/**")))


def test_scoped_authorization_may_widen_only_with_explicit_action() -> None:
    req = request(
        permitted_actions=(HumanAction.APPROVE, HumanAction.AUTHORIZE_SCOPE_CHANGE)
    )
    widened = resolution(
        action=HumanAction.AUTHORIZE_SCOPE_CHANGE,
        scope=("src/**", "docs/**"),
    )
    validate(req, widened)


def test_route_change_requires_explicit_authorization_action() -> None:
    with pytest.raises(RouteChangeNotAuthorizedError):
        validate(request(), resolution(route_change=True))


def test_route_change_with_explicit_authorization_is_accepted() -> None:
    req = request(
        permitted_actions=(HumanAction.APPROVE, HumanAction.AUTHORIZE_ROUTE_CHANGE)
    )
    validate(req, resolution(action=HumanAction.AUTHORIZE_ROUTE_CHANGE, route_change=True))


def test_concurrent_source_edit_invalidates_the_request() -> None:
    with pytest.raises(StaleResolutionBindingError):
        validate(request(), resolution(), source=SourceRevision("src-rev-2"))


def test_concurrent_work_order_edit_invalidates_the_request() -> None:
    with pytest.raises(StaleResolutionBindingError):
        validate(request(), resolution(), revision=WorkOrderRevision(8))


def test_resolution_bound_to_a_stale_source_is_rejected() -> None:
    with pytest.raises(StaleResolutionBindingError):
        validate(request(), resolution(source_revision=SourceRevision("src-rev-0")))


# -- readiness application --------------------------------------------------


def _open_blocked_work_order(facts: ReadinessFacts) -> WorkOrder:
    work_order = WorkOrder.create("WO-1")
    work_order.admit(reason="admitted", at=_NOW)
    apply_readiness(work_order, facts, at=_NOW)
    assert work_order.state is WorkOrderState.OPEN
    return work_order


def test_resolution_clears_only_its_human_blocker() -> None:
    facts = ReadinessFacts(
        dependencies_satisfied=True,
        backend_available=True,
        route_available=True,
        source_current=True,
        integration_ready=True,
        delivery_ready=True,
        approval_required=True,
    )
    work_order = _open_blocked_work_order(facts)
    assert work_order.readiness is OpenStatus.BLOCKED

    evaluation = apply_resolution_to_readiness(
        work_order, request(), resolution(), facts=facts, at=_NOW
    )
    assert evaluation.status is OpenStatus.READY
    assert work_order.readiness is OpenStatus.READY
    assert work_order.active_blockers == ()


def test_resolution_does_not_bypass_unrelated_blockers() -> None:
    facts = ReadinessFacts(
        dependencies_satisfied=False,
        backend_available=True,
        route_available=True,
        source_current=True,
        integration_ready=True,
        delivery_ready=True,
        approval_required=True,
    )
    work_order = _open_blocked_work_order(facts)

    evaluation = apply_resolution_to_readiness(
        work_order, request(), resolution(), facts=facts, at=_NOW
    )
    assert evaluation.status is OpenStatus.BLOCKED
    assert work_order.readiness is OpenStatus.BLOCKED
    # The dependency blocker survives the approval resolution.
    assert any(blocker.reason.value == "DEPENDENCY" for blocker in work_order.active_blockers)


def test_scope_authorization_does_not_unblock_without_revalidation() -> None:
    facts = ReadinessFacts(
        dependencies_satisfied=True,
        backend_available=True,
        route_available=True,
        source_current=True,
        integration_ready=True,
        delivery_ready=True,
        approval_required=True,
    )
    work_order = _open_blocked_work_order(facts)
    req = request(
        permitted_actions=(HumanAction.AUTHORIZE_SCOPE_CHANGE,)
    )
    res = resolution(action=HumanAction.AUTHORIZE_SCOPE_CHANGE, scope=("src/**", "docs/**"))

    evaluation = apply_resolution_to_readiness(work_order, req, res, facts=facts, at=_NOW)
    assert evaluation.status is OpenStatus.BLOCKED
    assert work_order.readiness is OpenStatus.BLOCKED


def test_revoked_resolution_cannot_apply_readiness() -> None:
    facts = ReadinessFacts(
        dependencies_satisfied=True,
        backend_available=True,
        route_available=True,
        source_current=True,
        integration_ready=True,
        delivery_ready=True,
        approval_required=True,
    )
    work_order = _open_blocked_work_order(facts)
    revoked = resolution(revoked_at=_LATER)
    with pytest.raises(ResolutionRevokedError):
        apply_resolution_to_readiness(work_order, request(), revoked, facts=facts, at=_NOW)


def test_resolution_does_not_bypass_budget_or_fabricate_a_decision() -> None:
    facts = ReadinessFacts(
        dependencies_satisfied=True,
        backend_available=True,
        route_available=True,
        source_current=True,
        integration_ready=True,
        delivery_ready=True,
        approval_required=True,
    )
    work_order = _open_blocked_work_order(facts)
    apply_resolution_to_readiness(work_order, request(), resolution(), facts=facts, at=_NOW)
    assert work_order.readiness is OpenStatus.READY

    admission = ExecutionAdmission()
    assert admission.evaluate(work_order).eligible
    # A budget constraint still blocks; the human resolution does not bypass it.
    assert not admission.evaluate(
        work_order, facts=AdmissionFacts(budget_available=False)
    ).eligible
    # No execution attempt/intent was fabricated by the human resolution.
    assert admission.active_attempt("WO-1") is None


def test_non_approval_reason_relaxes_human_input_not_approval() -> None:
    facts = ReadinessFacts(
        dependencies_satisfied=True,
        backend_available=True,
        route_available=True,
        source_current=True,
        integration_ready=True,
        delivery_ready=True,
        human_input_required=True,
    )
    work_order = _open_blocked_work_order(facts)
    req = replace(
        request(),
        reason=InterventionReason.CLARIFICATION_REQUIRED,
        permitted_actions=(HumanAction.PROVIDE_INPUT,),
    )
    assert req.reason is InterventionReason.CLARIFICATION_REQUIRED

    evaluation = apply_resolution_to_readiness(
        work_order, req, resolution(action=HumanAction.PROVIDE_INPUT), facts=facts, at=_NOW
    )
    assert evaluation.status is OpenStatus.READY


# -- ESCALATE emits a spec-bound request ------------------------------------


def _active_work_order() -> WorkOrder:
    work_order = WorkOrder.create("WO-1")
    work_order.admit(reason="admitted", at=_NOW)
    work_order.set_readiness(OpenStatus.READY)
    work_order.claim(reason="claimed", at=_NOW)
    return work_order


def test_escalate_can_emit_a_spec_bound_request() -> None:
    work_order = _active_work_order()
    bound = request(expires_at=_LATER)
    record = DecisionApplier().apply(
        work_order,
        SemanticDecision(decision_id="p-ESCALATE", kind=DecisionKind.ESCALATE),
        intervention_request=bound,
        at=_NOW,
    )
    assert record.intervention == bound
    assert record.intervention is not None
    assert record.intervention.source_revision == _SOURCE
    assert record.intervention.work_order_revision == _REVISION
    assert record.intervention.expires_at == _LATER


def test_escalate_rejects_a_request_for_another_work_order() -> None:
    work_order = _active_work_order()
    foreign = request(work_order_id=WorkOrderId("WO-2"))
    with pytest.raises(DecisionApplicationError):
        DecisionApplier().apply(
            work_order,
            SemanticDecision(decision_id="p-ESCALATE", kind=DecisionKind.ESCALATE),
            intervention_request=foreign,
            at=_NOW,
        )
