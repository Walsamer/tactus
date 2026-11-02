"""Pure inbound validation of Ictus decisions, without application wiring.

Pinned contract
---------------
* Ictus commit: ``67877fb99dacdba2ed44d3d1dfc47a02090566e2``
* Contract source: ``contracts/decision-envelope.schema.json`` and
  ``crates/core/src/envelope.rs::validate``; route: ``proposal.schema.json``
* Supported envelope ``schema_version``: ``1`` (frozen capability shape), ``2``

``parse_envelope`` always fills the envelope identity, snapshot, validation and
expiry fields of ``DecisionAdmission``. The deprecated legacy triple cannot
supply them: its missing provenance remains explicit as ``None``. Both shapes
normalize to one value, with a source tag, and refusals expose no actionable
semantic decision. No policy, capability admission or routing is recomputed.
Intent content remains a raw mapping for the application boundary to parse.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from tactus.domain.decisions import DecisionKind, RouteConstraints, SemanticDecision

from .decisions import parse_decision
from .digests import (
    CrossVersionDigestConflict,
    DigestContractError,
    MalformedDigestError,
    UnknownDigestVersionError,
)


class EnvelopeContractError(ValueError):
    """Base class for refused inbound decision envelopes."""


class UnsupportedEnvelopeVersionError(EnvelopeContractError):
    """The envelope version is outside the supported contract."""


class MalformedEnvelopeError(EnvelopeContractError):
    """An envelope field or conditional shape violates the contract."""


class EnvelopeExpiredError(EnvelopeContractError):
    """Expiry does not follow validation's fact timestamp (not wall time)."""


class ProposalBindingUnverifiedError(EnvelopeContractError):
    """The proposal serialization binding cannot yet be verified."""


class EnvelopeOutcome(str, Enum):
    """Closed Ictus outcome vocabulary, not a recovery policy."""

    EXECUTABLE = "EXECUTABLE"
    DENIED = "DENIED"
    PENDING = "PENDING"
    NO_ROUTE = "NO_ROUTE"
    INVALID = "INVALID"
    NO_EXECUTION = "NO_EXECUTION"


class DecisionSource(str, Enum):
    """Wire shape from which the admission was normalized."""

    ENVELOPE = "ENVELOPE"
    LEGACY_TRIPLE = "LEGACY_TRIPLE"


_AUTHORISING = frozenset({EnvelopeOutcome.EXECUTABLE, EnvelopeOutcome.NO_EXECUTION})
_VERDICTS = frozenset({"ALLOW", "DENY", "REQUIRE_APPROVAL", "MODIFY"})
_ROUTE_FIELDS = frozenset(
    {"exclude_backend", "preferred_backend", "required_provider", "required_runtime"}
)
#: Fields only a validated envelope carries. ``schema_version`` is deliberately
#: absent: the legacy triple carries a top-level ``schema_version`` too, so
#: treating the shared key as a marker would classify every real ``ictus decide``
#: payload as both shapes and refuse it.
_ENVELOPE_MARKERS = frozenset({"envelope_id", "outcome", "validation", "expiry"})
_LEGACY_MARKERS = frozenset({"policy_decision", "execution_intent"})


@dataclass(frozen=True, slots=True)
class EnvelopeValidation:
    """Ictus validation provenance; digest presence is not digest verification."""

    context_id: str
    proposal_digest: str
    capability_version: str
    validated_at: datetime

    def __post_init__(self) -> None:
        for name in ("context_id", "capability_version"):
            _required_str({name: getattr(self, name)}, name)
        if not isinstance(self.proposal_digest, str) or not re.fullmatch(
            r"[0-9a-fA-F]{64}", self.proposal_digest
        ):
            raise MalformedEnvelopeError("validation.proposal_digest must be 64 hex characters")
        _require_aware(self.validated_at, "validation.validated_at")


@dataclass(frozen=True, slots=True)
class DecisionAdmission:
    """Normalized inbound fact, not authorization to mutate a Work Order."""

    source: DecisionSource
    outcome: EnvelopeOutcome
    subject_type: str
    subject_id: str
    proposal_id: str
    proposal_schema_version: int
    proposal_decision: str
    verdict: str
    capability: str | None
    route: RouteConstraints | None
    intent: Mapping[str, Any] | None
    decision: SemanticDecision | None
    non_authorising: EnvelopeOutcome | None
    envelope_id: str | None = None
    schema_version: int | None = None
    snapshot_id: str | None = None
    snapshot_revision: int | None = None
    snapshot_digest: str | None = None
    policy_decision_id: str | None = None
    policy_version: str | None = None
    validation: EnvelopeValidation | None = None
    expires_at: datetime | None = None
    approvals: tuple[Mapping[str, Any], ...] = ()
    grants: tuple[Mapping[str, Any], ...] = ()
    #: Replay provenance only for the deprecated LEGACY_TRIPLE path; envelopes
    #: do not carry the raw proposal or policy mappings.
    raw: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, DecisionSource) or not isinstance(
            self.outcome, EnvelopeOutcome
        ):
            raise MalformedEnvelopeError("source and outcome must be closed vocabulary values")
        if self.outcome in _AUTHORISING:
            if not isinstance(self.decision, SemanticDecision) or (
                self.non_authorising is not None
            ):
                raise MalformedEnvelopeError("authorising outcome requires a semantic decision")
        elif self.decision is not None or self.non_authorising is not self.outcome:
            raise MalformedEnvelopeError("non-authorising outcome must expose no semantic decision")
        if (self.intent is not None) != (self.outcome is EnvelopeOutcome.EXECUTABLE):
            raise MalformedEnvelopeError("only EXECUTABLE carries an intent, and it requires one")
        if self.source is DecisionSource.ENVELOPE:
            if any(
                value is None for value in (
                    self.envelope_id, self.schema_version, self.snapshot_id,
                    self.snapshot_revision, self.snapshot_digest, self.policy_decision_id,
                    self.policy_version, self.validation, self.expires_at,
                )
            ):
                raise MalformedEnvelopeError("envelope admission requires full provenance")
            _require_aware(self.expires_at, "expiry")
            if self.expires_at <= self.validation.validated_at:
                raise EnvelopeExpiredError(
                    "expiry must be strictly later than validation.validated_at"
                )


def is_decision_envelope(payload: Any) -> bool:
    """Recognize the fields only an envelope carries, even an incomplete one."""

    return isinstance(payload, Mapping) and bool(_ENVELOPE_MARKERS.intersection(payload))


def is_legacy_decision_triple(payload: Any) -> bool:
    """Recognize deprecated triple markers without validating their content."""

    return isinstance(payload, Mapping) and bool(_LEGACY_MARKERS.intersection(payload))


def parse_decision_payload(payload: Mapping[str, Any]) -> DecisionAdmission:
    """Dispatch one unambiguous wire shape; never fall back after a refusal."""

    envelope = is_decision_envelope(payload)
    legacy = is_legacy_decision_triple(payload)
    if envelope == legacy:
        raise MalformedEnvelopeError(
            "decision payload must carry exactly one of envelope or legacy triple"
        )
    return parse_envelope(payload) if envelope else _parse_legacy_triple(payload)


def parse_envelope(payload: Mapping[str, Any]) -> DecisionAdmission:
    """Validate envelope v1/v2 and normalize only its permitted effect.

    Expiry is compared only with validation fact time, never with a clock.
    This boundary does not verify proposal bytes; see ``verify_proposal_binding``.
    """

    if not isinstance(payload, Mapping):
        raise MalformedEnvelopeError("envelope payload must be a mapping")
    version = _integer(payload, "schema_version")
    if version not in {1, 2}:
        raise UnsupportedEnvelopeVersionError(
            f"unsupported envelope schema_version {version}; supported are 1 and 2"
        )
    subject = _subject(payload, "subject")
    snapshot = _object(payload, "snapshot", {"snapshot_id", "revision", "digest"})
    proposal = _object(payload, "proposal", {"proposal_id", "schema_version", "decision"})
    proposal_version = _integer(proposal, "schema_version")
    if proposal_version not in {1, 2}:
        raise MalformedEnvelopeError("proposal.schema_version must be 1 or 2")
    token = _required_str(proposal, "decision")
    if token not in {kind.value for kind in DecisionKind}:
        raise MalformedEnvelopeError(f"proposal.decision is not an envelope token: {token!r}")
    kind = DecisionKind.from_token(token)
    policy = _object(payload, "policy", {"policy_decision_id", "policy_version", "verdict"})
    verdict = _verdict(policy, "verdict")
    try:
        outcome = EnvelopeOutcome(_required_str(payload, "outcome"))
    except ValueError as error:
        raise MalformedEnvelopeError(
            f"unknown envelope outcome: {payload.get('outcome')!r}"
        ) from error

    has_capability = "capability_validation" in payload
    if has_capability != kind.requires_capability:
        raise MalformedEnvelopeError(
            "capability_validation must be present exactly for capability-bearing decisions"
        )
    if version == 1 and not has_capability:
        raise MalformedEnvelopeError("schema_version 1 requires capability_validation")
    approvals = _array(payload, "approvals")
    grants = _array(payload, "grants")
    if not kind.requires_capability and (
        "route" in payload or approvals or grants or "intent" in payload
    ):
        raise MalformedEnvelopeError(
            "a decision without capability carries no route, approvals, grants or intent"
        )
    if outcome is EnvelopeOutcome.NO_EXECUTION and (
        kind.requires_capability or verdict not in {"ALLOW", "MODIFY"}
    ):
        raise MalformedEnvelopeError(
            "NO_EXECUTION requires a non-capability decision and ALLOW or MODIFY verdict"
        )
    if outcome in {EnvelopeOutcome.PENDING, EnvelopeOutcome.NO_ROUTE} and (
        not kind.requires_capability
    ):
        raise MalformedEnvelopeError("PENDING and NO_ROUTE require a capability-bearing decision")

    capability = None
    capability_validation = None
    if has_capability:
        capability_validation = _object(
            payload, "capability_validation", {"capability", "admitted", "permitted", "reason"}
        )
        capability = _required_str(capability_validation, "capability")
        _required_str(capability_validation, "reason")
        _boolean(capability_validation, "admitted")
        _boolean(capability_validation, "permitted")
    route = _parse_route(payload, kind)
    validation = _object(
        payload, "validation", {"context_id", "proposal_digest", "capability_version", "validated_at"}
    )
    proposal_digest = _required_str(validation, "proposal_digest")
    validated_at = _parse_datetime(validation.get("validated_at"), "validation.validated_at")
    snapshot_revision = _integer(snapshot, "revision", minimum=0)
    intent = None
    if outcome is EnvelopeOutcome.EXECUTABLE:
        if capability_validation is None or not (
            capability_validation["admitted"] and capability_validation["permitted"]
        ):
            raise MalformedEnvelopeError(
                "EXECUTABLE requires admitted and permitted capability_validation"
            )
        intent = _object(payload, "intent")
        if _required_str(intent, "capability") != capability:
            raise MalformedEnvelopeError(
                "intent.capability must match capability_validation.capability"
            )
        target = _subject(intent, "target")
        if (target["type"], target["id"]) != (subject["type"], subject["id"]):
            raise MalformedEnvelopeError("intent.target must match subject")
        # An intent must bind the exact validated proposal and snapshot, or the
        # arguments that would execute were never the ones policy approved.
        _bind_intent_to_validation(
            intent,
            proposal_digest=proposal_digest,
            snapshot_id=_required_str(snapshot, "snapshot_id"),
            snapshot_revision=snapshot_revision,
            snapshot_digest=_required_str(snapshot, "digest"),
        )
    elif "intent" in payload:
        raise MalformedEnvelopeError("non-EXECUTABLE outcome must not carry intent")

    _validate_approvals(approvals)
    _validate_grants(grants, validated_at=validated_at)
    if capability is not None:
        _validate_approval_bindings(
            approvals,
            grants,
            subject=subject,
            revision=snapshot_revision,
            capability=capability,
            policy_version=_required_str(policy, "policy_version"),
        )
    proposal_id = _required_str(proposal, "proposal_id")
    return DecisionAdmission(
        source=DecisionSource.ENVELOPE,
        envelope_id=_required_str(payload, "envelope_id"),
        schema_version=version,
        outcome=outcome,
        subject_type=subject["type"],
        subject_id=subject["id"],
        snapshot_id=_required_str(snapshot, "snapshot_id"),
        snapshot_revision=snapshot_revision,
        snapshot_digest=_required_str(snapshot, "digest"),
        proposal_id=proposal_id,
        proposal_schema_version=proposal_version,
        proposal_decision=token,
        policy_decision_id=_required_str(policy, "policy_decision_id"),
        policy_version=_required_str(policy, "policy_version"),
        verdict=verdict,
        capability=capability,
        route=route,
        intent=intent,
        validation=EnvelopeValidation(
            context_id=_required_str(validation, "context_id"),
            proposal_digest=proposal_digest,
            capability_version=_required_str(validation, "capability_version"),
            validated_at=validated_at,
        ),
        expires_at=_parse_datetime(payload.get("expiry"), "expiry"),
        decision=(
            SemanticDecision(
                decision_id=proposal_id, kind=kind, capability=capability,
                # Route constraints belong to a ROUTE effect; the admission keeps
                # what the envelope said for reporting either way.
                route=route if kind is DecisionKind.ROUTE else None,
                schema_version=proposal_version,
            ) if outcome in _AUTHORISING else None
        ),
        non_authorising=None if outcome in _AUTHORISING else outcome,
        approvals=approvals,
        grants=grants,
    )


def _parse_legacy_triple(payload: Mapping[str, Any]) -> DecisionAdmission:
    """Normalize deprecated semantics using the existing proposal parser."""

    proposal = _object(payload, "proposal")
    decision = parse_decision(proposal)
    policy = _object(payload, "policy_decision")
    verdict = _verdict(policy, "decision")
    # ``policy-decision.schema.json`` requires ``proposal_id``, and it is the only
    # provenance the deprecated triple can bind: a policy decision about another
    # proposal must never authorize this one.
    if _required_str(policy, "proposal_id") != decision.decision_id:
        raise MalformedEnvelopeError(
            "policy decision does not belong to the supplied proposal"
        )
    subject = _subject(proposal, "subject")
    raw_intent = payload.get("execution_intent")
    if raw_intent is not None and not isinstance(raw_intent, Mapping):
        raise MalformedEnvelopeError("execution_intent must be an object or null")
    if verdict in {"DENY", "REQUIRE_APPROVAL"}:
        if raw_intent is not None:
            raise MalformedEnvelopeError("refusing legacy verdict must not carry execution_intent")
        outcome = EnvelopeOutcome.DENIED if verdict == "DENY" else EnvelopeOutcome.PENDING
    elif decision.kind.requires_capability:
        if raw_intent is None:
            raise MalformedEnvelopeError("legacy capability decision carries no ExecutionIntent")
        outcome = EnvelopeOutcome.EXECUTABLE
    else:
        if raw_intent is not None:
            raise MalformedEnvelopeError("legacy non-capability decision must not carry execution_intent")
        outcome = EnvelopeOutcome.NO_EXECUTION
    return DecisionAdmission(
        source=DecisionSource.LEGACY_TRIPLE,
        outcome=outcome,
        subject_type=subject["type"], subject_id=subject["id"],
        proposal_id=decision.decision_id, proposal_schema_version=decision.schema_version,
        proposal_decision=_required_str(proposal, "decision"), verdict=verdict,
        capability=decision.capability, route=decision.route, intent=raw_intent,
        decision=decision if outcome in _AUTHORISING else None,
        non_authorising=None if outcome in _AUTHORISING else outcome,
        raw=payload,
    )


def verify_proposal_binding(envelope: Any, raw_proposal: Any) -> None:
    """Always fail closed until Ictus publishes ``contracts/digest-vectors.json``.

    Ictus's ``validation.proposal_digest`` is currently computed using
    ``sha256(serde_json::to_vec(proposal))``: struct field order, not canonical
    JSON. Canonical vectors arrive later; until then no canonical re-verification
    can pass. This single hook must raise before inspecting or using either input.
    """

    raise ProposalBindingUnverifiedError(
        "validation.proposal_digest cannot be verified until Ictus publishes "
        "contracts/digest-vectors.json for canonical proposal serialization"
    )


# -- validation helpers ---------------------------------------------------


def _required_str(payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise MalformedEnvelopeError(f"{field_name} must be a non-empty string")
    return value


def _optional_str(payload: Mapping[str, Any], field_name: str) -> str | None:
    return _required_str(payload, field_name) if field_name in payload else None


def _object(
    payload: Mapping[str, Any],
    field_name: str,
    allowed: set[str] | frozenset[str] | None = None,
) -> Mapping[str, Any]:
    value = payload.get(field_name)
    if not isinstance(value, Mapping):
        raise MalformedEnvelopeError(f"{field_name} must be an object")
    if allowed is not None and set(value) - allowed:
        raise MalformedEnvelopeError(f"{field_name} contains unknown fields")
    return value


def _subject(payload: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    value = _object(payload, field_name)
    _required_str(value, "type")
    _required_str(value, "id")
    return value


def _integer(payload: Mapping[str, Any], field_name: str, *, minimum: int | None = None) -> int:
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedEnvelopeError(f"{field_name} must be an integer")
    if minimum is not None and value < minimum:
        raise MalformedEnvelopeError(f"{field_name} must be at least {minimum}")
    return value


def _boolean(payload: Mapping[str, Any], field_name: str) -> bool:
    value = payload.get(field_name)
    if not isinstance(value, bool):
        raise MalformedEnvelopeError(f"{field_name} must be a boolean")
    return value


def _verdict(payload: Mapping[str, Any], field_name: str) -> str:
    value = _required_str(payload, field_name)
    if value not in _VERDICTS:
        raise MalformedEnvelopeError(f"unknown policy verdict: {value!r}")
    return value


def _parse_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise MalformedEnvelopeError(f"{field_name} must be a date-time string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise MalformedEnvelopeError(f"{field_name} is not a valid date-time: {value!r}") from None
    _require_aware(parsed, field_name)
    return parsed


def _require_aware(value: Any, field_name: str) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise MalformedEnvelopeError(f"{field_name} must be a timezone-aware date-time")


def _array(payload: Mapping[str, Any], field_name: str) -> tuple[Mapping[str, Any], ...]:
    if field_name not in payload:
        return ()
    value = payload[field_name]
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise MalformedEnvelopeError(f"{field_name} must be an array")
    if any(not isinstance(item, Mapping) for item in value):
        raise MalformedEnvelopeError(f"{field_name} entries must be objects")
    return tuple(value)


def _parse_route(payload: Mapping[str, Any], kind: DecisionKind) -> RouteConstraints | None:
    if "route" not in payload:
        return None
    if not kind.requires_capability:
        raise MalformedEnvelopeError(
            "a decision without capability carries no route constraints"
        )
    raw = _object(payload, "route", _ROUTE_FIELDS)
    return RouteConstraints(**{name: _optional_str(raw, name) for name in _ROUTE_FIELDS})


def _bind_intent_to_validation(
    intent: Mapping[str, Any],
    *,
    proposal_digest: str,
    snapshot_id: str,
    snapshot_revision: int,
    snapshot_digest: str,
) -> None:
    """Require the intent to bind the validated proposal and the exact snapshot.

    Ictus refuses an executable envelope whose intent does not name the validated
    ``proposal_digest`` (``envelope.rs``); without that binding the arguments that
    would execute are not the ones policy approved. The snapshot triple is checked
    when present, because the composition writes it into every intent it emits.
    """

    policy_context = intent.get("policy_context")
    if not isinstance(policy_context, Mapping):
        raise MalformedEnvelopeError(
            "an EXECUTABLE intent must carry policy_context binding the validated proposal"
        )
    if policy_context.get("proposal_digest") != proposal_digest:
        raise MalformedEnvelopeError(
            "intent.policy_context.proposal_digest must equal validation.proposal_digest"
        )
    for field_name, expected in (
        ("snapshot_id", snapshot_id),
        ("snapshot_revision", snapshot_revision),
        ("snapshot_digest", snapshot_digest),
    ):
        if field_name in policy_context and policy_context[field_name] != expected:
            raise MalformedEnvelopeError(
                f"intent.policy_context.{field_name} must match the envelope snapshot"
            )


def _validate_approval_bindings(
    approvals: tuple[Mapping[str, Any], ...],
    grants: tuple[Mapping[str, Any], ...],
    *,
    subject: Mapping[str, Any],
    revision: int,
    capability: str,
    policy_version: str,
) -> None:
    """A satisfied approval must name a live grant that binds this decision."""

    for approval in approvals:
        if not approval["satisfied"]:
            continue
        bound = any(
            not grant["revoked"]
            and grant["approval"] == approval["name"]
            and (grant["subject"]["type"], grant["subject"]["id"])
            == (subject["type"], subject["id"])
            and grant["revision"] == revision
            and grant["capability"] == capability
            and grant["policy_version"] == policy_version
            for grant in grants
        )
        if not bound:
            raise MalformedEnvelopeError(
                f"satisfied approval {approval['name']!r} has no matching bound grant"
            )


def _validate_approvals(approvals: tuple[Mapping[str, Any], ...]) -> None:
    for approval in approvals:
        _object({"approval": approval}, "approval", {"name", "satisfied"})
        _required_str(approval, "name")
        _boolean(approval, "satisfied")


def _validate_grants(
    grants: tuple[Mapping[str, Any], ...], *, validated_at: datetime
) -> None:
    for grant in grants:
        _object({"grant": grant}, "grant", {
            "grant_id", "approval", "subject", "revision", "capability", "policy_version",
            "issued_at", "expires_at", "revoked", "evidence",
        })
        for name in ("grant_id", "approval", "capability", "policy_version"):
            _required_str(grant, name)
        if grant["approval"] == "*":
            raise MalformedEnvelopeError("grant.approval must not be '*'")
        _subject(grant, "subject")
        _integer(grant, "revision", minimum=0)
        _boolean(grant, "revoked")
        issued_at = _parse_datetime(grant.get("issued_at"), "issued_at")
        expires_at = _parse_datetime(grant.get("expires_at"), "expires_at")
        # A grant only counts for the instant this decision was validated at.
        if issued_at > validated_at or expires_at <= validated_at:
            raise MalformedEnvelopeError(
                "grant must be issued before and expire after validation.validated_at"
            )
        evidence = _array(grant, "evidence")
        if not evidence:
            raise MalformedEnvelopeError("grant.evidence must not be empty")
        for item in evidence:
            _object({"evidence": item}, "evidence", {"kind", "uri", "sha256", "note"})
            _required_str(item, "kind")
            _required_str(item, "uri")
            for name in ("sha256", "note"):
                if name in item and not isinstance(item[name], str):
                    raise MalformedEnvelopeError(f"evidence.{name} must be a string")


__all__ = [
    "CrossVersionDigestConflict", "DigestContractError", "MalformedDigestError",
    "UnknownDigestVersionError", "DecisionAdmission", "DecisionSource",
    "EnvelopeContractError", "EnvelopeExpiredError", "EnvelopeOutcome",
    "EnvelopeValidation", "MalformedEnvelopeError", "ProposalBindingUnverifiedError",
    "UnsupportedEnvelopeVersionError", "is_decision_envelope", "is_legacy_decision_triple",
    "parse_decision_payload", "parse_envelope", "verify_proposal_binding",
]
