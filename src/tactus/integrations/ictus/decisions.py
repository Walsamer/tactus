"""Inbound validation of generic Ictus ``DecisionProposal`` decisions.

Pinned contract
---------------
* Ictus ADR: ``docs/adr/0001-generic-decision-vocabulary.md``
* Contract source: ``contracts/proposal.schema.json`` (``DecisionProposal``)
* Supported ``schema_version``: ``2`` (plus deliberate ``1`` compatibility)

This module is the anti-corruption boundary for the generic decision edge. It
mirrors the semantics of the Ictus state snapshot adapter (FIX-005) in the
opposite direction: Ictus produces a validated semantic decision, Tactus
validates the decision-relevant part of the wire payload fail-closed and
normalizes it into the Tactus-side :class:`~tactus.domain.decisions.SemanticDecision`.

It deliberately does **not**:

* re-validate Ictus-owned proposal metadata (``provider``/``subject``/
  ``evidence``): capability validation, policy and proposal provenance are
  Ictus's;
* diagnose a failure or choose a decision;
* mutate a Work Order (that is :mod:`tactus.domain.decision_application`).

Version handling (deliberate, from the frozen ADR):

* ``schema_version: 2`` is the current vocabulary and is emitted by Ictus.
* ``schema_version: 1`` is accepted for compatibility; its legacy ``RETRY`` token
  is normalized to ``REEXECUTE``. The v2-only tokens ``ROUTE``/``DECOMPOSE`` are
  rejected under v1.
* Any other ``schema_version`` fails closed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tactus.domain.decisions import (
    DecisionKind,
    RouteConstraints,
    SemanticDecision,
    UnknownDecisionError,
)

#: Current ``DecisionProposal`` schema version (frozen generic vocabulary).
SUPPORTED_DECISION_SCHEMA_VERSION = 2

#: Legacy v1 schema version accepted for compatibility.
LEGACY_DECISION_SCHEMA_VERSION = 1

#: The v2-only tokens, rejected under the legacy v1 contract.
_V2_ONLY_TOKENS = frozenset({DecisionKind.ROUTE.value, DecisionKind.DECOMPOSE.value})

_ROUTE_FIELDS = (
    "exclude_backend",
    "preferred_backend",
    "required_provider",
    "required_runtime",
)


class DecisionContractError(ValueError):
    """Base class for generic decision compatibility failures."""


class UnsupportedDecisionVersionError(DecisionContractError):
    """The payload declares a ``schema_version`` this boundary cannot handle."""


class MalformedDecisionError(DecisionContractError):
    """The payload is missing, or has an invalid type for, a contract field."""


def parse_decision(payload: Mapping[str, Any]) -> SemanticDecision:
    """Validate the decision part of an Ictus ``DecisionProposal`` payload.

    Fails closed with a specific :class:`DecisionContractError` subclass for an
    unsupported version or malformed fields, and with
    :class:`~tactus.domain.decisions.UnknownDecisionError` for a token outside
    the frozen vocabulary. Ictus-owned proposal metadata is not re-validated.
    """

    if not isinstance(payload, Mapping):
        raise MalformedDecisionError("decision payload must be a mapping")

    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise MalformedDecisionError("schema_version must be an integer")
    if version not in {
        LEGACY_DECISION_SCHEMA_VERSION,
        SUPPORTED_DECISION_SCHEMA_VERSION,
    }:
        raise UnsupportedDecisionVersionError(
            "unsupported Ictus DecisionProposal schema_version "
            f"{version}; supported are {LEGACY_DECISION_SCHEMA_VERSION} and "
            f"{SUPPORTED_DECISION_SCHEMA_VERSION}"
        )

    raw_token = payload.get("decision")
    if not isinstance(raw_token, str):
        raise MalformedDecisionError("decision must be a string")
    token = raw_token.strip()

    if version == LEGACY_DECISION_SCHEMA_VERSION:
        if token in _V2_ONLY_TOKENS:
            raise MalformedDecisionError(
                f"decision {token!r} is not part of the v1 vocabulary"
            )
    elif token == "RETRY":
        # RETRY is a v1-only legacy alias; v2 writers emit REEXECUTE.
        raise MalformedDecisionError(
            "decision 'RETRY' is a v1 legacy alias and is not valid under "
            "schema_version 2"
        )

    kind = DecisionKind.from_token(token)
    capability = _optional_str(payload, "capability")
    if kind.requires_capability and capability is None:
        raise MalformedDecisionError(
            f"decision {kind.value} requires a capability id"
        )
    if capability is not None and not kind.requires_capability:
        raise MalformedDecisionError(
            f"decision {kind.value} must not carry a capability id"
        )
    route = _parse_route(payload, kind)

    return SemanticDecision(
        decision_id=_required_str(payload, "proposal_id"),
        kind=kind,
        capability=capability,
        route=route,
        schema_version=version,
    )


def _parse_route(
    payload: Mapping[str, Any], kind: DecisionKind
) -> RouteConstraints | None:
    if "route" not in payload:
        return None
    raw = payload["route"]
    if kind is not DecisionKind.ROUTE:
        raise MalformedDecisionError(
            f"route constraints are only valid for ROUTE, not {kind.value}"
        )
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Mapping):
        raise MalformedDecisionError("route must be an object when present")
    unknown = set(raw) - set(_ROUTE_FIELDS)
    if unknown:
        raise MalformedDecisionError(
            f"unknown route constraint fields: {sorted(unknown)}"
        )
    values: dict[str, str | None] = {}
    for field_name in _ROUTE_FIELDS:
        values[field_name] = _optional_str(raw, field_name)
    return RouteConstraints(**values)


# -- validation helpers ---------------------------------------------------


def _required_str(payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise MalformedDecisionError(f"{field_name} must be a non-empty string")
    return value


def _optional_str(container: Mapping[str, Any], field_name: str) -> str | None:
    if field_name not in container:
        return None
    value = container[field_name]
    if not isinstance(value, str) or not value.strip():
        raise MalformedDecisionError(
            f"{field_name} must be a non-empty string when present"
        )
    return value


# Re-export the closed vocabulary error so consumers of this boundary can catch
# it without importing the domain module directly.
__all__ = [
    "LEGACY_DECISION_SCHEMA_VERSION",
    "SUPPORTED_DECISION_SCHEMA_VERSION",
    "DecisionContractError",
    "MalformedDecisionError",
    "UnknownDecisionError",
    "UnsupportedDecisionVersionError",
    "parse_decision",
]
