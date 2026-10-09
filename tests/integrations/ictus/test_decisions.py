"""Inbound generic Ictus ``DecisionProposal`` decision boundary tests.

These cover the decision-relevant part of the wire contract only: fail-closed
token/version handling, legacy ``RETRY`` normalization, route constraints and
normalization into the Tactus decision value. There are deliberately no
diagnosis or policy tests: Ictus owns those and Tactus must not duplicate them.
"""

from __future__ import annotations

import pytest

import tactus.integrations.ictus.decisions as ictus_decisions
from tactus.domain import DecisionKind, SemanticDecision, UnknownDecisionError
from tactus.integrations.ictus import (
    LEGACY_DECISION_SCHEMA_VERSION,
    SUPPORTED_DECISION_SCHEMA_VERSION,
    MalformedDecisionError,
    UnsupportedDecisionVersionError,
    parse_decision,
)


def payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "schema_version": SUPPORTED_DECISION_SCHEMA_VERSION,
        "proposal_id": "proposal-0001",
        "decision": "REEXECUTE",
        "capability": "demo.verify",
    }
    base.update(overrides)
    return base


# -- normalization ---------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "kind"),
    [
        ("REEXECUTE", DecisionKind.REEXECUTE),
        ("ROUTE", DecisionKind.ROUTE),
        ("DECOMPOSE", DecisionKind.DECOMPOSE),
        ("ESCALATE", DecisionKind.ESCALATE),
        ("ABORT", DecisionKind.ABORT),
        ("EXECUTE_CAPABILITY", DecisionKind.EXECUTE_CAPABILITY),
    ],
)
def test_every_frozen_v2_token_parses(token: str, kind: DecisionKind) -> None:
    raw = payload(decision=token)
    if kind.requires_capability:
        raw["capability"] = "demo.verify"
    else:
        raw.pop("capability", None)

    decision = parse_decision(raw)

    assert decision.kind is kind
    assert decision.decision_id == "proposal-0001"
    assert decision.schema_version == SUPPORTED_DECISION_SCHEMA_VERSION


def test_legacy_v1_retry_normalizes_to_reexecute() -> None:
    decision = parse_decision(
        payload(schema_version=LEGACY_DECISION_SCHEMA_VERSION, decision="RETRY")
    )
    assert decision.kind is DecisionKind.REEXECUTE
    assert decision.schema_version == LEGACY_DECISION_SCHEMA_VERSION


def test_route_constraints_are_parsed_and_preserved() -> None:
    decision = parse_decision(
        payload(
            decision="ROUTE",
            route={
                "exclude_backend": "backend-b",
                "preferred_backend": "backend-a",
                "required_provider": "provider-a",
                "required_runtime": "runtime-x",
            },
        )
    )
    assert decision.route is not None
    assert decision.route.exclude_backend == "backend-b"
    assert decision.route.preferred_backend == "backend-a"
    assert decision.route.required_provider == "provider-a"
    assert decision.route.required_runtime == "runtime-x"


def test_unconstrained_route_is_valid() -> None:
    decision = parse_decision(payload(decision="ROUTE", route={}))
    assert decision.route is not None
    assert decision.route.is_empty


# -- fail-closed version handling ------------------------------------------


def test_unknown_decision_token_fails_closed() -> None:
    with pytest.raises(UnknownDecisionError):
        parse_decision(payload(decision="REQUEUE_READY"))


def test_unknown_decision_token_does_not_leak_into_the_mapping() -> None:
    with pytest.raises(UnknownDecisionError):
        parse_decision(payload(decision="BLOCK"))


@pytest.mark.parametrize("token", ["ROUTE", "DECOMPOSE"])
def test_v2_only_tokens_are_rejected_under_v1(token: str) -> None:
    raw = payload(schema_version=LEGACY_DECISION_SCHEMA_VERSION, decision=token)
    with pytest.raises(MalformedDecisionError):
        parse_decision(raw)


def test_legacy_retry_is_rejected_under_v2() -> None:
    with pytest.raises(MalformedDecisionError):
        parse_decision(payload(schema_version=SUPPORTED_DECISION_SCHEMA_VERSION, decision="RETRY"))


@pytest.mark.parametrize("version", [0, 3, 99])
def test_unsupported_integer_version_fails_closed(version: int) -> None:
    with pytest.raises(UnsupportedDecisionVersionError):
        parse_decision(payload(schema_version=version))


@pytest.mark.parametrize("version", ["2", True, None])
def test_malformed_version_fails_closed(version: object) -> None:
    with pytest.raises(MalformedDecisionError):
        parse_decision(payload(schema_version=version))


def test_non_mapping_payload_fails_closed() -> None:
    with pytest.raises(MalformedDecisionError):
        parse_decision(["not", "a", "mapping"])  # type: ignore[arg-type]


# -- fail-closed structural validation -------------------------------------


def test_decision_must_be_a_string() -> None:
    with pytest.raises(MalformedDecisionError):
        parse_decision(payload(decision=123))


def test_proposal_id_is_required_and_non_empty() -> None:
    raw = payload()
    del raw["proposal_id"]
    with pytest.raises(MalformedDecisionError):
        parse_decision(raw)


def test_capability_is_required_for_capability_decisions() -> None:
    raw = payload(decision="EXECUTE_CAPABILITY")
    del raw["capability"]
    with pytest.raises(MalformedDecisionError):
        parse_decision(raw)


def test_capability_is_rejected_for_non_capability_decisions() -> None:
    with pytest.raises(MalformedDecisionError):
        parse_decision(payload(decision="ABORT", capability="demo.verify"))


def test_route_is_rejected_for_non_route_decisions() -> None:
    with pytest.raises(MalformedDecisionError):
        parse_decision(payload(decision="REEXECUTE", route={}))


def test_unknown_route_field_fails_closed() -> None:
    with pytest.raises(MalformedDecisionError):
        parse_decision(payload(decision="ROUTE", route={"backend_vendor": "acme"}))


def test_blank_route_constraint_fails_closed() -> None:
    with pytest.raises(MalformedDecisionError):
        parse_decision(payload(decision="ROUTE", route={"exclude_backend": "  "}))


def test_blank_capability_fails_closed() -> None:
    with pytest.raises(MalformedDecisionError):
        parse_decision(payload(capability="   "))


# -- purity: no Work Order mutation and no decision policy -----------------


@pytest.mark.parametrize(
    "forbidden",
    [
        "WorkOrder",
        "apply_decision",
        "apply",
        "mutate",
        "decide",
        "diagnose",
        "select_backend",
        "rank_backends",
        "retry_budget",
    ],
)
def test_ictus_boundary_has_no_work_order_mutation_or_policy_surface(forbidden: str) -> None:
    assert not hasattr(ictus_decisions, forbidden)


def test_parsing_is_pure_and_returns_a_value() -> None:
    raw = payload()
    snapshot = dict(raw)

    decision = parse_decision(raw)

    assert raw == snapshot
    assert isinstance(decision, SemanticDecision)
