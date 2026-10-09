"""Generic semantic decision vocabulary tests (Tactus read-only mirror).

The vocabulary is Ictus-owned; Tactus only mirrors it and fails closed on
anything outside it. These tests cover the value form and its structural
invariants, not decision policy.
"""

from __future__ import annotations

import pytest

from tactus.domain import (
    DecisionError,
    DecisionKind,
    RouteConstraints,
    SemanticDecision,
    UnknownDecisionError,
)

_FROZEN_TOKENS = (
    "REEXECUTE",
    "ROUTE",
    "DECOMPOSE",
    "ESCALATE",
    "ABORT",
    "EXECUTE_CAPABILITY",
)


def test_vocabulary_is_the_frozen_ictus_set() -> None:
    assert [kind.value for kind in DecisionKind] == list(_FROZEN_TOKENS)


@pytest.mark.parametrize("token", _FROZEN_TOKENS)
def test_from_token_accepts_every_frozen_token(token: str) -> None:
    assert DecisionKind.from_token(token) is DecisionKind(token)


def test_legacy_retry_normalizes_to_reexecute() -> None:
    assert DecisionKind.from_token("RETRY") is DecisionKind.REEXECUTE


@pytest.mark.parametrize(
    "token",
    ["REQUEUE_READY", "BLOCK", "RETIRE", "ROUTE_DIFFERENTLY", "reexecute", "", "  "],
)
def test_unknown_or_out_of_vocabulary_tokens_fail_closed(token: str) -> None:
    with pytest.raises(UnknownDecisionError):
        DecisionKind.from_token(token)


def test_non_string_token_fails_closed() -> None:
    with pytest.raises(UnknownDecisionError):
        DecisionKind.from_token(None)  # type: ignore[arg-type]


def test_requires_capability_mirrors_the_ictus_contract() -> None:
    assert DecisionKind.REEXECUTE.requires_capability
    assert DecisionKind.ROUTE.requires_capability
    assert DecisionKind.EXECUTE_CAPABILITY.requires_capability
    assert not DecisionKind.DECOMPOSE.requires_capability
    assert not DecisionKind.ESCALATE.requires_capability
    assert not DecisionKind.ABORT.requires_capability


def test_v2_only_tokens_are_route_and_decompose() -> None:
    assert DecisionKind.ROUTE.is_v2_only
    assert DecisionKind.DECOMPOSE.is_v2_only
    assert not DecisionKind.REEXECUTE.is_v2_only
    assert not DecisionKind.ABORT.is_v2_only


# -- semantic decision invariants -----------------------------------------


def test_decision_requires_capability_when_required() -> None:
    with pytest.raises(DecisionError):
        SemanticDecision(decision_id="p1", kind=DecisionKind.REEXECUTE)


def test_decision_rejects_capability_when_not_applicable() -> None:
    with pytest.raises(DecisionError):
        SemanticDecision(
            decision_id="p1", kind=DecisionKind.ABORT, capability="demo.verify"
        )


def test_decision_rejects_route_outside_route_kind() -> None:
    with pytest.raises(DecisionError):
        SemanticDecision(
            decision_id="p1",
            kind=DecisionKind.REEXECUTE,
            capability="demo.verify",
            route=RouteConstraints(),
        )


def test_decision_rejects_blank_identity_and_capability() -> None:
    with pytest.raises(DecisionError):
        SemanticDecision(decision_id="  ", kind=DecisionKind.ESCALATE)
    with pytest.raises(DecisionError):
        SemanticDecision(
            decision_id="p1", kind=DecisionKind.REEXECUTE, capability="  "
        )


def test_decision_trims_identity_and_capability() -> None:
    decision = SemanticDecision(
        decision_id="  p1  ", kind=DecisionKind.REEXECUTE, capability="  demo.verify  "
    )
    assert decision.decision_id == "p1"
    assert decision.capability == "demo.verify"


# -- route constraints -----------------------------------------------------


def test_route_constraints_are_roles_not_vendors() -> None:
    route = RouteConstraints(exclude_backend="backend-b", required_provider="provider-a")
    assert route.exclude_backend == "backend-b"
    assert route.required_provider == "provider-a"
    assert not route.is_empty


def test_empty_route_is_valid_and_expresses_no_preference() -> None:
    route = RouteConstraints()
    assert route.is_empty
    assert DecisionKind.ROUTE.is_v2_only


@pytest.mark.parametrize(
    "field_name",
    ["exclude_backend", "preferred_backend", "required_provider", "required_runtime"],
)
def test_route_constraints_reject_blank_values(field_name: str) -> None:
    with pytest.raises(DecisionError):
        RouteConstraints(**{field_name: "  "})
