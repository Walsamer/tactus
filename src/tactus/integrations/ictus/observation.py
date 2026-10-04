"""Inbound validation/translation boundary for Ictus execution observations.

Pinned contract
---------------
* Ictus commit: ``833175d``
* Contract source: ``contracts/observation.schema.json`` (``ExecutionObservation``)
* Supported ``schema_version``: ``1``

This module is an anti-corruption boundary, not a second Ictus implementation
and not a translation layer for arbitrary Tactus facts. It does **not** define
``ExecutionResult``, does **not** implement ``ExecutionResult.to_observation()``
and does **not** map Dagster runs: those are owned by Ictus.

The boundary is deliberately **inbound-only**:

* Ictus validates execution semantics and produces ``ExecutionObservation`` v1;
* Tactus validates that wire payload strictly, fail-closed, and normalizes it
  into the Tactus-side :class:`TactusObservation` fact, preserving evidence and
  provenance.

Execution facts cross this boundary. Domain/control facts (dependency
resolution, complexity/split decisions, supersession, scope violations, backend
capacity/health, ...) are **not** execution observations and must not be forced
through ``ExecutionObservation``. They are represented as ``StateSnapshot``
facts/context by the StateSnapshot adapter (FIX-005). There is deliberately no
Tactus-wide failure taxonomy here and no outbound ``ExecutionObservation``
origination or wire round-trip.

Everything here is pure: no Work Order mutation, no lifecycle transition, no
persistence, no scheduler/recovery dispatch and no backend calls.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

#: Ictus commit pinned by this compatibility boundary.
ICTUS_COMMIT = "833175d"

#: Ictus ``ExecutionObservation`` contract version supported here.
SUPPORTED_SCHEMA_VERSION = 1


class ObservationContractError(ValueError):
    """Base class for Ictus observation compatibility failures."""


class UnsupportedObservationVersionError(ObservationContractError):
    """The payload declares a ``schema_version`` this boundary cannot handle."""


class UnknownObservationCategoryError(ObservationContractError):
    """The payload declares a category outside the closed Ictus v1 vocabulary.

    This is deliberately distinct from ``UNKNOWN``: ``UNKNOWN`` is a *valid*
    Ictus v1 category for an execution outcome Ictus could not classify, whereas
    an unknown wire category is invalid input and fails closed.
    """


class MalformedObservationError(ObservationContractError):
    """The payload is missing, or has an invalid type for, a contract field."""


class IctusObservationCategory(str, Enum):
    """The closed Ictus v1 observation vocabulary.

    Tactus must never invent categories beyond this set.
    """

    SUCCESS = "SUCCESS"
    WORKER_TIMEOUT = "WORKER_TIMEOUT"
    PROCESS_CRASH = "PROCESS_CRASH"
    VERIFICATION_FAILURE = "VERIFICATION_FAILURE"
    INTEGRATION_CONFLICT = "INTEGRATION_CONFLICT"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """Transport-independent evidence pointer (Ictus v1 shape, minus secrets)."""

    kind: str
    uri: str
    sha256: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class TactusObservation:
    """The Tactus-side normalized execution-observation fact.

    This is the boundary's normalized *input* to Tactus. It retains the Ictus
    category and the full evidence provenance (``kind``/``uri``/``sha256``/
    ``note``). Turning it into a Tactus domain ``FailureObservation`` and
    recording it on an ``ACTIVE`` Work Order is a later wiring step, not part of
    the compatibility boundary.
    """

    observation_id: str
    execution_id: str
    intent_id: str
    category: IctusObservationCategory
    observed_at: datetime
    message: str | None = None
    evidence: tuple[EvidenceRef, ...] = ()
    retryable: bool | None = None
    schema_version: int = field(default=SUPPORTED_SCHEMA_VERSION)


def parse_observation(payload: Mapping[str, Any]) -> TactusObservation:
    """Validate an Ictus ``ExecutionObservation`` v1 payload and normalize it.

    Fails closed with a specific :class:`ObservationContractError` subclass for
    an unsupported version, an unknown category or malformed fields.

    Optional fields are optional by **omission**, matching the authoritative v1
    JSON schema exactly: if ``message``, ``evidence``, ``retryable``,
    ``sha256`` or ``note`` is present it must have the declared type; an
    explicit JSON ``null`` is malformed (the schema declares no ``null`` type).
    """

    if not isinstance(payload, Mapping):
        raise MalformedObservationError("observation payload must be a mapping")

    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise MalformedObservationError("schema_version must be an integer")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise UnsupportedObservationVersionError(
            "unsupported Ictus observation schema_version "
            f"{version}; supported is {SUPPORTED_SCHEMA_VERSION} "
            f"(Ictus {ICTUS_COMMIT})"
        )

    raw_category = payload.get("category")
    if not isinstance(raw_category, str):
        raise MalformedObservationError("category must be a string")
    try:
        category = IctusObservationCategory(raw_category)
    except ValueError:
        raise UnknownObservationCategoryError(
            f"unknown Ictus observation category: {raw_category!r}"
        ) from None

    return TactusObservation(
        observation_id=_required_str(payload, "observation_id"),
        execution_id=_required_str(payload, "execution_id"),
        intent_id=_required_str(payload, "intent_id"),
        category=category,
        observed_at=_parse_datetime(payload.get("observed_at")),
        message=_optional_str(payload, "message"),
        evidence=_parse_evidence(payload),
        retryable=_optional_bool(payload, "retryable"),
        schema_version=version,
    )


# -- validation helpers ---------------------------------------------------


def _required_str(payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise MalformedObservationError(f"{field_name} must be a non-empty string")
    return value


def _optional_str(container: Mapping[str, Any], field_name: str) -> str | None:
    if field_name not in container:
        return None
    value = container[field_name]
    if not isinstance(value, str):
        raise MalformedObservationError(f"{field_name} must be a string when present")
    return value


def _optional_bool(container: Mapping[str, Any], field_name: str) -> bool | None:
    if field_name not in container:
        return None
    value = container[field_name]
    if not isinstance(value, bool):
        raise MalformedObservationError(f"{field_name} must be a boolean when present")
    return value


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise MalformedObservationError("observed_at must be a date-time string")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise MalformedObservationError(
            f"observed_at is not a valid date-time: {value!r}"
        ) from None


def _parse_evidence(payload: Mapping[str, Any]) -> tuple[EvidenceRef, ...]:
    if "evidence" not in payload:
        return ()
    value = payload["evidence"]
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise MalformedObservationError("evidence must be an array when present")
    references: list[EvidenceRef] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise MalformedObservationError("evidence entries must be objects")
        references.append(
            EvidenceRef(
                kind=_required_str(item, "kind"),
                uri=_required_str(item, "uri"),
                sha256=_optional_str(item, "sha256"),
                note=_optional_str(item, "note"),
            )
        )
    return tuple(references)
