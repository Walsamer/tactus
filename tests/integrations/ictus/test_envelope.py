"""Core envelope shape, normalization and fail-closed binding behavior."""

from collections.abc import Mapping
from datetime import datetime, timezone

import pytest

from tactus.domain.decisions import DecisionKind, RouteConstraints
from tactus.integrations.ictus.envelope import (
    DecisionAdmission,
    DecisionSource,
    EnvelopeExpiredError,
    EnvelopeOutcome,
    MalformedEnvelopeError,
    ProposalBindingUnverifiedError,
    UnsupportedEnvelopeVersionError,
    is_decision_envelope,
    is_legacy_decision_triple,
    parse_decision_payload,
    parse_envelope,
    verify_proposal_binding,
)


def _envelope(**overrides):
    payload = {
        "schema_version": 2,
        "envelope_id": "envelope-1",
        "subject": {"type": "work_order", "id": "WO-1"},
        "snapshot": {"snapshot_id": "snapshot-1", "revision": 0, "digest": "snapshot-digest"},
        "proposal": {"proposal_id": "proposal-1", "schema_version": 2, "decision": "ESCALATE"},
        "policy": {"policy_decision_id": "policy-1", "policy_version": "policy.v1", "verdict": "ALLOW"},
        "validation": {
            "context_id": "context-1", "proposal_digest": "a" * 64,
            "capability_version": "not-applicable", "validated_at": "2026-01-01T00:00:00Z",
        },
        "outcome": "NO_EXECUTION",
        "expiry": "2026-01-01T00:01:00Z",
    }
    payload.update(overrides)
    return payload


def _executable(**overrides):
    return _envelope(
        proposal={"proposal_id": "proposal-1", "schema_version": 2, "decision": "REEXECUTE"},
        capability_validation={"capability": "test.verify", "admitted": True, "permitted": True, "reason": "validated"},
        outcome="EXECUTABLE",
        intent={
            "schema_version": 1, "intent_id": "intent-1", "capability": "test.verify",
            "target": {"type": "work_order", "id": "WO-1"},
            "requested_by": {"provider": "rules", "decision_id": "proposal-1"},
        },
        **overrides,
    )


def _legacy(verdict="ALLOW", token="REEXECUTE"):
    proposal = {
        "proposal_id": "proposal-1", "schema_version": 2, "decision": token,
        "subject": {"type": "work_order", "id": "WO-1"},
        "provider": {"provider_type": "RULES", "provider_id": "test.rules"},
        "proposed_at": "2026-01-01T00:00:00Z",
    }
    if token in {"REEXECUTE", "RETRY", "ROUTE", "EXECUTE_CAPABILITY"}:
        proposal["capability"] = "test.verify"
    return {
        "proposal": proposal,
        "policy_decision": {"decision": verdict, "proposal_id": "proposal-1"},
        "execution_intent": _executable()["intent"] if verdict in {"ALLOW", "MODIFY"} and "capability" in proposal else None,
    }


@pytest.mark.parametrize("version", [1, 2])
def test_supported_envelope_versions(version):
    assert parse_envelope(_executable(schema_version=version)).schema_version == version


@pytest.mark.parametrize("version", [0, 3])
def test_unsupported_version_is_checked_before_conditional_shape(version):
    with pytest.raises(UnsupportedEnvelopeVersionError):
        parse_envelope(_envelope(schema_version=version))


@pytest.mark.parametrize("version", ["2", True, None])
def test_non_integer_version_is_malformed(version):
    with pytest.raises(MalformedEnvelopeError, match="integer"):
        parse_envelope(_envelope(schema_version=version))


def test_rule_1_capability_validation_present_exactly_for_capability_decisions():
    """Rule 1: capability_validation presence is an equivalence, not a hint."""
    payload = _executable()
    del payload["capability_validation"]
    with pytest.raises(MalformedEnvelopeError, match="present exactly"):
        parse_envelope(payload)
    with pytest.raises(MalformedEnvelopeError, match="present exactly"):
        parse_envelope(_envelope(capability_validation=_executable()["capability_validation"]))


def test_rule_2_v1_requires_capability_validation_after_version_check():
    """Rule 2: v1 ESCALATE is a shape error, not an unsupported version."""
    with pytest.raises(MalformedEnvelopeError, match="schema_version 1"):
        parse_envelope(_envelope(schema_version=1))


@pytest.mark.parametrize("field,value", [
    ("route", {}), ("approvals", [{"name": "review", "satisfied": True}]),
    ("grants", [{"grant_id": "grant-1"}]), ("intent", {}),
])
def test_rule_3_non_capability_decision_has_no_execution_fields(field, value):
    """Rule 3: non-capability decisions route, gate and execute nothing."""
    with pytest.raises(MalformedEnvelopeError, match="without capability"):
        parse_envelope(_envelope(**{field: value}))


@pytest.mark.parametrize("verdict", ["DENY", "REQUIRE_APPROVAL"])
def test_rule_4_no_execution_cannot_claim_a_refusing_verdict(verdict):
    """Rule 4: NO_EXECUTION claims permission and cannot contradict policy."""
    payload = _envelope()
    payload["policy"]["verdict"] = verdict
    with pytest.raises(MalformedEnvelopeError, match="NO_EXECUTION"):
        parse_envelope(payload)


def test_rule_4_no_execution_requires_no_capability():
    """Rule 4: a capability-bearing decision cannot resolve to NO_EXECUTION."""
    payload = _executable()
    payload["outcome"] = "NO_EXECUTION"
    del payload["intent"]
    with pytest.raises(MalformedEnvelopeError, match="NO_EXECUTION"):
        parse_envelope(payload)


@pytest.mark.parametrize("outcome", ["PENDING", "NO_ROUTE"])
def test_rule_5_pending_and_no_route_require_capability(outcome):
    """Rule 5: no-capability decisions have no gate or route to wait for."""
    with pytest.raises(MalformedEnvelopeError, match="require a capability"):
        parse_envelope(_envelope(outcome=outcome))


@pytest.mark.parametrize("defect", ["admitted", "permitted", "intent", "capability", "target_type", "target_id"])
def test_rule_6_executable_requires_admitted_permitted_bound_intent(defect):
    """Rule 6: EXECUTABLE binds permitted capability and subject to intent."""
    payload = _executable()
    if defect in {"admitted", "permitted"}:
        payload["capability_validation"][defect] = False
    elif defect == "intent":
        del payload["intent"]
    elif defect == "capability":
        payload["intent"]["capability"] = "wrong"
    else:
        payload["intent"]["target"]["type" if defect == "target_type" else "id"] = "wrong"
    with pytest.raises(MalformedEnvelopeError, match="EXECUTABLE|intent"):
        parse_envelope(payload)


@pytest.mark.parametrize("outcome", ["DENIED", "PENDING", "NO_ROUTE", "INVALID", "NO_EXECUTION"])
def test_rule_7_non_executable_never_carries_intent(outcome):
    """Rule 7: every non-EXECUTABLE outcome forbids intent, including null."""
    payload = _envelope() if outcome == "NO_EXECUTION" else _executable()
    payload.update(outcome=outcome, intent=None)
    with pytest.raises(MalformedEnvelopeError, match="intent"):
        parse_envelope(payload)


@pytest.mark.parametrize("proposal_version", [1, 2])
def test_rule_8_retry_is_never_an_envelope_token(proposal_version):
    """Rule 8: RETRY remains legacy-only even under proposal version 1."""
    payload = _executable()
    payload["proposal"].update(schema_version=proposal_version, decision="RETRY")
    with pytest.raises(MalformedEnvelopeError, match="not an envelope token"):
        parse_envelope(payload)


@pytest.mark.parametrize("expiry", ["2025-12-31T23:59:59Z", "2026-01-01T00:00:00Z"])
def test_expiry_must_strictly_follow_validation_fact_time(expiry):
    with pytest.raises(EnvelopeExpiredError, match="strictly later"):
        parse_envelope(_envelope(expiry=expiry))


def test_no_execution_escalate_normalization_and_provenance():
    admission = parse_envelope(_envelope(approvals=[], grants=[]))
    assert isinstance(admission, DecisionAdmission)
    assert admission.source is DecisionSource.ENVELOPE
    assert admission.outcome is EnvelopeOutcome.NO_EXECUTION
    assert admission.decision.kind is DecisionKind.ESCALATE
    assert admission.decision.decision_id == "proposal-1"
    assert admission.decision.capability is None
    assert admission.decision.route is None
    assert admission.decision.schema_version == 2
    assert admission.non_authorising is None
    assert admission.intent is None
    assert admission.subject_type == "work_order" and admission.subject_id == "WO-1"
    assert admission.snapshot_id == "snapshot-1" and admission.snapshot_revision == 0
    assert admission.snapshot_digest == "snapshot-digest"
    assert admission.policy_version == "policy.v1" and admission.verdict == "ALLOW"
    assert admission.validation.context_id == "context-1"
    assert admission.expires_at == datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)


def test_executable_reexecute_normalization():
    payload = _executable()
    admission = parse_envelope(payload)
    assert admission.decision.kind is DecisionKind.REEXECUTE
    assert admission.decision.capability == admission.capability == "test.verify"
    assert admission.decision.route is admission.route is None
    assert admission.decision.schema_version == 2
    assert admission.non_authorising is None
    assert admission.intent is payload["intent"]


def test_route_normalization_and_non_route_rejection():
    payload = _executable()
    payload["proposal"]["decision"] = "ROUTE"
    payload["route"] = {"exclude_backend": "old", "preferred_backend": "new", "required_provider": "provider", "required_runtime": "runtime"}
    admission = parse_envelope(payload)
    assert admission.route == admission.decision.route == RouteConstraints(**payload["route"])
    payload["proposal"]["decision"] = "REEXECUTE"
    with pytest.raises(MalformedEnvelopeError, match="only valid for ROUTE"):
        parse_envelope(payload)
    payload["proposal"]["decision"] = "ROUTE"
    payload["route"]["unknown"] = "value"
    with pytest.raises(MalformedEnvelopeError, match="unknown fields"):
        parse_envelope(payload)


@pytest.mark.parametrize("outcome", ["DENIED", "INVALID", "PENDING", "NO_ROUTE"])
def test_non_authorising_outcomes_expose_no_semantic_decision(outcome):
    payload = _envelope(outcome=outcome) if outcome in {"DENIED", "INVALID"} else _executable()
    payload["outcome"] = outcome
    if outcome == "DENIED":
        payload["policy"]["verdict"] = "DENY"
    payload.pop("intent", None)
    admission = parse_envelope(payload)
    assert admission.decision is None
    assert admission.non_authorising is EnvelopeOutcome(outcome)
    assert admission.proposal_decision == payload["proposal"]["decision"]
    if outcome == "DENIED":
        assert admission.proposal_decision == "ESCALATE"
        assert admission.verdict == "DENY"


def test_shape_dispatch_envelope_legacy_both_neither():
    envelope, legacy = _envelope(), _legacy()
    assert is_decision_envelope(envelope) and not is_legacy_decision_triple(envelope)
    assert is_legacy_decision_triple(legacy) and not is_decision_envelope(legacy)
    assert parse_decision_payload(envelope).source is DecisionSource.ENVELOPE
    assert parse_decision_payload(legacy).source is DecisionSource.LEGACY_TRIPLE
    for payload in ({**envelope, **legacy}, {}, {"proposal": {}}, None):
        with pytest.raises(MalformedEnvelopeError, match="exactly one"):
            parse_decision_payload(payload)


@pytest.mark.parametrize("verdict,outcome", [
    ("ALLOW", EnvelopeOutcome.EXECUTABLE), ("MODIFY", EnvelopeOutcome.EXECUTABLE),
    ("DENY", EnvelopeOutcome.DENIED), ("REQUIRE_APPROVAL", EnvelopeOutcome.PENDING),
])
def test_legacy_admission_preserves_semantics_without_inventing_provenance(verdict, outcome):
    admission = parse_decision_payload(_legacy(verdict))
    assert admission.outcome is outcome and admission.verdict == verdict
    assert (admission.decision is None) == (outcome in {EnvelopeOutcome.DENIED, EnvelopeOutcome.PENDING})
    assert admission.non_authorising is (outcome if admission.decision is None else None)
    assert admission.envelope_id is admission.schema_version is admission.snapshot_id is None
    assert admission.snapshot_revision is admission.snapshot_digest is admission.validation is admission.expires_at is None


def test_legacy_uses_existing_retry_alias_and_refuses_missing_or_contradictory_intent():
    payload = _legacy(token="RETRY")
    payload["proposal"]["schema_version"] = 1
    assert parse_decision_payload(payload).decision.kind is DecisionKind.REEXECUTE
    payload["execution_intent"] = None
    with pytest.raises(MalformedEnvelopeError, match="carries no ExecutionIntent"):
        parse_decision_payload(payload)
    payload = _legacy("DENY")
    payload["execution_intent"] = _executable()["intent"]
    with pytest.raises(MalformedEnvelopeError, match="refusing legacy verdict"):
        parse_decision_payload(payload)
    assert parse_decision_payload(_legacy(token="ESCALATE")).outcome is EnvelopeOutcome.NO_EXECUTION


def test_legacy_triple_refuses_a_policy_decision_for_another_proposal():
    # The deprecated triple has no other binding than ``proposal_id``; the old
    # intent path checked it and the envelope path keeps that check here.
    payload = _legacy()
    payload["policy_decision"]["proposal_id"] = "proposal-other"
    with pytest.raises(MalformedEnvelopeError, match="does not belong"):
        parse_decision_payload(payload)

    payload = _legacy()
    del payload["policy_decision"]["proposal_id"]
    with pytest.raises(MalformedEnvelopeError, match="proposal_id"):
        parse_decision_payload(payload)


def test_real_binary_legacy_triple_is_not_mistaken_for_an_envelope():
    # ``ictus decide`` emits a top-level ``schema_version`` beside the triple.
    # Classifying that shared key as an envelope marker would refuse every real
    # payload as ambiguous, and the deprecated path has to keep working.
    payload = {"schema_version": 1, **_legacy(token="ESCALATE")}
    assert not is_decision_envelope(payload)
    admission = parse_decision_payload(payload)
    assert admission.source is DecisionSource.LEGACY_TRIPLE
    assert admission.outcome is EnvelopeOutcome.NO_EXECUTION


def test_proposal_binding_fails_closed_even_for_a_well_formed_proposal():
    proposal = _legacy(token="ESCALATE")["proposal"]
    with pytest.raises(ProposalBindingUnverifiedError, match=r"validation.proposal_digest.*contracts/digest-vectors.json"):
        verify_proposal_binding(parse_envelope(_envelope()), proposal)


def test_proposal_binding_raises_before_using_either_input():
    class UnreadableMapping(Mapping):
        def __getitem__(self, key):
            pytest.fail("binding stub must not inspect unverified input")

        def __iter__(self):
            pytest.fail("binding stub must not inspect unverified input")

        def __len__(self):
            pytest.fail("binding stub must not inspect unverified input")

    with pytest.raises(ProposalBindingUnverifiedError):
        verify_proposal_binding(UnreadableMapping(), UnreadableMapping())
