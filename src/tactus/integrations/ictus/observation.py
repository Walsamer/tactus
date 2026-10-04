"""Versioned compatibility boundary for Ictus execution observations.

Pinned contract
---------------
* Ictus commit: ``833175d``
* Ictus ``ExecutionObservation`` ``schema_version``: ``1``

This module is an anti-corruption boundary, not a second Ictus implementation.
It does **not** define ``ExecutionResult``, does **not** implement
``ExecutionResult.to_observation()`` and does **not** map Dagster runs: those are
owned by Ictus. Tactus only:

* validates an inbound Ictus ``ExecutionObservation`` v1 payload (fail closed);
* normalizes it into the Tactus-side :class:`TactusObservation` fact;
* can originate an Ictus v1 observation payload from a Tactus failure
  classification (outbound direction).

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

    This is deliberately distinct from :attr:`IctusObservationCategory.UNKNOWN`:
    a *known* failure with no precise equivalent maps to ``UNKNOWN``, whereas an
    unknown wire category is invalid input and fails closed.
    """


class MalformedObservationError(ObservationContractError):
    """The payload is missing, or has an invalid type for, a required field."""


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


class TactusFailureCategory(str, Enum):
    """Tactus-side failure classification.

    Mirrors the diagnosis taxonomy in ``docs/architecture/TRIAGE_AND_RECOVERY.md``.
    It is intentionally broader than the Ictus vocabulary; the mapping below is
    conservative and many-to-one.
    """

    TRANSIENT_RUNTIME_FAILURE = "TRANSIENT_RUNTIME_FAILURE"
    PROVIDER_API_TIMEOUT = "PROVIDER_API_TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_QUOTA_EXHAUSTED = "PROVIDER_QUOTA_EXHAUSTED"
    WORKER_TIMEOUT = "WORKER_TIMEOUT"
    EXECUTION_TIMEOUT = "EXECUTION_TIMEOUT"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    DEPENDENCY_RESOLVED = "DEPENDENCY_RESOLVED"
    TASK_TOO_COMPLEX_SPLITTABLE = "TASK_TOO_COMPLEX_SPLITTABLE"
    TASK_TOO_COMPLEX_NOT_SPLITTABLE = "TASK_TOO_COMPLEX_NOT_SPLITTABLE"
    REQUEST_INVALID_OR_SUPERSEDED = "REQUEST_INVALID_OR_SUPERSEDED"
    VERIFICATION_FAILURE = "VERIFICATION_FAILURE"
    INTEGRATION_FAILURE = "INTEGRATION_FAILURE"
    UNKNOWN = "UNKNOWN"


#: Conservative, explicit Tactus -> Ictus category mapping.
#:
#: Only classifications with a reasonably precise Ictus v1 equivalent are
#: mapped to a specific category; everything else becomes ``UNKNOWN``. This is
#: deliberately many-to-one and never invents new Ictus categories.
TACTUS_TO_ICTUS_CATEGORY: Mapping[TactusFailureCategory, IctusObservationCategory] = {
    TactusFailureCategory.WORKER_TIMEOUT: IctusObservationCategory.WORKER_TIMEOUT,
    TactusFailureCategory.PROVIDER_UNAVAILABLE: IctusObservationCategory.PROVIDER_UNAVAILABLE,
    TactusFailureCategory.PROVIDER_QUOTA_EXHAUSTED: IctusObservationCategory.RESOURCE_EXHAUSTED,
    TactusFailureCategory.VERIFICATION_FAILURE: IctusObservationCategory.VERIFICATION_FAILURE,
    TactusFailureCategory.INTEGRATION_FAILURE: IctusObservationCategory.INTEGRATION_CONFLICT,
    # No precise Ictus v1 equivalent: fail safely to UNKNOWN rather than guess.
    TactusFailureCategory.TRANSIENT_RUNTIME_FAILURE: IctusObservationCategory.UNKNOWN,
    TactusFailureCategory.PROVIDER_API_TIMEOUT: IctusObservationCategory.UNKNOWN,
    TactusFailureCategory.EXECUTION_TIMEOUT: IctusObservationCategory.UNKNOWN,
    TactusFailureCategory.SCOPE_VIOLATION: IctusObservationCategory.UNKNOWN,
    TactusFailureCategory.DEPENDENCY_UNAVAILABLE: IctusObservationCategory.UNKNOWN,
    TactusFailureCategory.DEPENDENCY_RESOLVED: IctusObservationCategory.UNKNOWN,
    TactusFailureCategory.TASK_TOO_COMPLEX_SPLITTABLE: IctusObservationCategory.UNKNOWN,
    TactusFailureCategory.TASK_TOO_COMPLEX_NOT_SPLITTABLE: IctusObservationCategory.UNKNOWN,
    TactusFailureCategory.REQUEST_INVALID_OR_SUPERSEDED: IctusObservationCategory.UNKNOWN,
    TactusFailureCategory.UNKNOWN: IctusObservationCategory.UNKNOWN,
}


def ictus_category_for(failure: TactusFailureCategory) -> IctusObservationCategory:
    """Map a Tactus failure classification to its Ictus v1 category."""

    return TACTUS_TO_ICTUS_CATEGORY[failure]


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """Transport-independent evidence pointer (Ictus v1 shape, minus secrets)."""

    kind: str
    uri: str
    sha256: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class TactusObservation:
    """The Tactus-side normalized execution observation fact.

    This is the boundary's normalized output. It retains the Ictus category and
    the full evidence provenance (``kind``/``uri``/``sha256``/``note``). Turning
    it into a Tactus domain ``FailureObservation`` and recording it on an
    ``ACTIVE`` Work Order is a later wiring step, not part of the compatibility
    boundary.
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
    an unsupported version, an unknown category or malformed required fields.
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
        message=_optional_str(payload.get("message"), "message"),
        evidence=_parse_evidence(payload.get("evidence")),
        retryable=_optional_bool(payload.get("retryable"), "retryable"),
        schema_version=version,
    )


def observation_from_failure(
    *,
    observation_id: str,
    execution_id: str,
    intent_id: str,
    failure: TactusFailureCategory,
    observed_at: datetime,
    message: str | None = None,
    evidence: Sequence[EvidenceRef] = (),
    retryable: bool | None = None,
) -> TactusObservation:
    """Originate a normalized observation from a Tactus failure classification."""

    return TactusObservation(
        observation_id=observation_id,
        execution_id=execution_id,
        intent_id=intent_id,
        category=ictus_category_for(failure),
        observed_at=observed_at,
        message=message,
        evidence=tuple(evidence),
        retryable=retryable,
    )


def to_ictus_payload(observation: TactusObservation) -> dict[str, Any]:
    """Serialize a normalized observation back to an Ictus v1 wire payload."""

    payload: dict[str, Any] = {
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "observation_id": observation.observation_id,
        "execution_id": observation.execution_id,
        "intent_id": observation.intent_id,
        "category": observation.category.value,
        "observed_at": observation.observed_at.isoformat(),
    }
    if observation.message is not None:
        payload["message"] = observation.message
    if observation.evidence:
        payload["evidence"] = [_evidence_payload(ref) for ref in observation.evidence]
    if observation.retryable is not None:
        payload["retryable"] = observation.retryable
    return payload


# -- validation helpers ---------------------------------------------------


def _required_str(payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise MalformedObservationError(f"{field_name} must be a non-empty string")
    return value


def _optional_str(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MalformedObservationError(f"{field_name} must be a string or null")
    return value


def _optional_bool(value: Any, field_name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise MalformedObservationError(f"{field_name} must be a boolean or null")
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


def _parse_evidence(value: Any) -> tuple[EvidenceRef, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise MalformedObservationError("evidence must be an array")
    references: list[EvidenceRef] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise MalformedObservationError("evidence entries must be objects")
        references.append(
            EvidenceRef(
                kind=_required_str(item, "kind"),
                uri=_required_str(item, "uri"),
                sha256=_optional_str(item.get("sha256"), "sha256"),
                note=_optional_str(item.get("note"), "note"),
            )
        )
    return tuple(references)


def _evidence_payload(reference: EvidenceRef) -> dict[str, Any]:
    payload: dict[str, Any] = {"kind": reference.kind, "uri": reference.uri}
    if reference.sha256 is not None:
        payload["sha256"] = reference.sha256
    if reference.note is not None:
        payload["note"] = reference.note
    return payload
