"""Domain fact adapters for the versioned Tactus -> Ictus snapshot profile.

This module owns the *domain fact construction* side of the outbound
``StateSnapshot`` boundary: it turns Tactus-owned domain values (Work Order
history, fenced execution attempts, backend descriptors/health/quota, approval
grants) into the primitive, domain-neutral facts emitted on the versioned
snapshot profile. It decides nothing: no retry/reroute/decompose/escalate
policy and no backend ranking live here.

Normative sources
-----------------
* ``docs/architecture/CROSS_SYSTEM_CONTRACTS.md`` (snapshot profile, canonical
  fact semantics and legacy ``retry.attempt``/``retry.budget`` translation).
* ``docs/architecture/EXECUTION_AND_RECOVERY.md`` (semantic attempts vs Dagster
  step retries).

Two counters stay categorically separate
----------------------------------------
``recovery.semantic_attempts`` / ``recovery.max_semantic_attempts``
    The semantic, decision-relevant accepted-attempt count and authorized
    total limit. ``recovery.semantic_attempts`` includes the failed current
    attempt during recovery; another attempt is permitted only while
    ``count < limit``. Missing or invalid budget is a hard validation failure,
    never an implicit zero/default unlimited budget.

``execution.step_retry_index``
    The execution backend's (Dagster) diagnostic step-retry index. It is
    execution-owned diagnostics only and can never consume or refill the
    semantic attempt allowance by substitution.

Raw facts, never a filtered candidate list
------------------------------------------
Backend facts are exposed as raw descriptors, timestamped health observations
and provider quota observations. There is deliberately no ranked or
policy-filtered candidate list here; Ictus owns compatibility, freshness
suitability and route selection.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from tactus.backends.capacity import ProviderCapacityObservation
from tactus.backends.health import BackendStatusObservation
from tactus.backends.registry import BackendDescriptor

#: The Tactus fact-profile version this module constructs.
#:
#: The generic Ictus ``StateSnapshot`` ``schema_version`` stays ``1``; the
#: Tactus fact profile is versioned independently (Baseline v1).
SUPPORTED_SNAPSHOT_PROFILE_VERSION = 2


class StateSnapshotContractError(ValueError):
    """An adapter input cannot produce a valid versioned snapshot payload."""


class UnsupportedSnapshotVersionError(StateSnapshotContractError):
    """The generic Ictus ``schema_version`` is not supported."""


class UnsupportedSnapshotProfileError(StateSnapshotContractError):
    """The Tactus fact-profile version is not supported."""


class MalformedSnapshotError(StateSnapshotContractError):
    """The payload is missing, or has an invalid type for, a profile field."""


class SnapshotPhase(str, Enum):
    """The two profile phases.

    ``INITIAL`` carries no execution observation: first execution must never be
    given a fabricated previous observation. ``RECOVERY`` carries exactly one
    validated, correlated failure observation.
    """

    INITIAL = "INITIAL"
    RECOVERY = "RECOVERY"


def _require_non_empty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StateSnapshotContractError(f"{label} must be a non-empty string")
    return value.strip()


def _require_count(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StateSnapshotContractError(f"{label} must be an integer")
    if value < 0:
        raise StateSnapshotContractError(f"{label} must be non-negative")
    return value


def _require_datetime(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise StateSnapshotContractError(f"{label} must be a datetime")
    return value


@dataclass(frozen=True, slots=True)
class SemanticBudget:
    """Canonical semantic attempt count/limit facts.

    ``semantic_attempts`` is the number of already accepted semantic attempts
    (zero before the first; it includes the failed current attempt during
    recovery). ``max_semantic_attempts`` is the authorized total accepted-attempt
    limit. Both are mandatory: missing/invalid budget is a typed failure, never
    an implicit zero or unlimited budget.

    The adapter emits these facts and takes no decision. ``permits_another_attempt``
    is a read-only convenience mirroring the documented boundary rule
    (``count < limit``) for tests and diagnostics; it is not policy.
    """

    semantic_attempts: int
    max_semantic_attempts: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "semantic_attempts",
            _require_count(self.semantic_attempts, "semantic_attempts"),
        )
        object.__setattr__(
            self,
            "max_semantic_attempts",
            _require_count(self.max_semantic_attempts, "max_semantic_attempts"),
        )

    @property
    def permits_another_attempt(self) -> bool:
        """Documented boundary rule ``count < limit`` as a diagnostic."""

        return self.semantic_attempts < self.max_semantic_attempts


@dataclass(frozen=True, slots=True)
class StepRetryDiagnostic:
    """Execution-owned diagnostic step-retry index (never a semantic attempt)."""

    index: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "index", _require_count(self.index, "step_retry_index"))


@dataclass(frozen=True, slots=True)
class AuthorizationGrant:
    """Raw approval/authorization evidence, not a policy verdict.

    Tactus records grants; Ictus validates their sufficiency. The adapter
    transports the grant evidence verbatim and never asserts that a grant is
    sufficient or that an ``ALLOW`` was issued.
    """

    grant_id: str
    scope: str
    actor: str
    issued_at: datetime
    expires_at: datetime | None = None
    source_revision: str | None = None
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "grant_id", _require_non_empty(self.grant_id, "grant_id"))
        object.__setattr__(self, "scope", _require_non_empty(self.scope, "grant scope"))
        object.__setattr__(self, "actor", _require_non_empty(self.actor, "grant actor"))
        issued_at = _require_datetime(self.issued_at, "issued_at")
        object.__setattr__(self, "issued_at", issued_at)
        if self.expires_at is not None:
            object.__setattr__(
                self,
                "expires_at",
                _require_datetime(self.expires_at, "expires_at"),
            )
        if self.source_revision is not None:
            object.__setattr__(
                self,
                "source_revision",
                _require_non_empty(self.source_revision, "source_revision"),
            )
        evidence = tuple(
            _require_non_empty(item, "authorization evidence ref")
            for item in self.evidence
        )
        object.__setattr__(self, "evidence", evidence)

    def is_fresh_at(self, at: datetime) -> bool:
        if self.expires_at is None:
            return True
        return at < self.expires_at


@dataclass(frozen=True, slots=True)
class BackendFacts:
    """Raw backend facts: descriptors plus status and quota observations.

    This is deliberately not a filtered or ranked candidate list. It exposes
    stable backend identities, static descriptors (capability/provider/runtime/
    model/effort) and timestamped, expiring health and provider-quota facts so
    the decision plane can evaluate compatibility, freshness and route.
    """

    descriptors: tuple[BackendDescriptor, ...] = ()
    statuses: tuple[BackendStatusObservation, ...] = ()
    quotas: tuple[ProviderCapacityObservation, ...] = ()
    previous_backend: str | None = None

    def __post_init__(self) -> None:
        descriptors = tuple(self.descriptors)
        for descriptor in descriptors:
            if not isinstance(descriptor, BackendDescriptor):
                raise StateSnapshotContractError(
                    "backend descriptors must be BackendDescriptor values"
                )
        object.__setattr__(self, "descriptors", descriptors)

        statuses = tuple(self.statuses)
        for status in statuses:
            if not isinstance(status, BackendStatusObservation):
                raise StateSnapshotContractError(
                    "backend statuses must be BackendStatusObservation values"
                )
        object.__setattr__(self, "statuses", statuses)

        quotas = tuple(self.quotas)
        for quota in quotas:
            if not isinstance(quota, ProviderCapacityObservation):
                raise StateSnapshotContractError(
                    "backend quotas must be ProviderCapacityObservation values"
                )
        object.__setattr__(self, "quotas", quotas)

        if self.previous_backend is not None:
            object.__setattr__(
                self,
                "previous_backend",
                _require_non_empty(self.previous_backend, "previous_backend"),
            )


# -- JSON projection helpers ---------------------------------------------


def backend_descriptor_fact(descriptor: BackendDescriptor) -> dict[str, Any]:
    """Project a :class:`BackendDescriptor` onto a raw descriptor fact."""

    if not isinstance(descriptor, BackendDescriptor):
        raise StateSnapshotContractError("descriptor must be a BackendDescriptor")
    fact: dict[str, Any] = {
        "backend_id": descriptor.backend_id.value,
        "capabilities": sorted(descriptor.capabilities),
    }
    if descriptor.agent_runtime is not None:
        fact["agent_runtime"] = descriptor.agent_runtime
    if descriptor.model is not None:
        fact["model"] = descriptor.model
    if descriptor.provider is not None:
        fact["provider"] = descriptor.provider
    if descriptor.effort is not None:
        fact["effort"] = descriptor.effort.value
    return fact


def backend_status_fact(observation: BackendStatusObservation) -> dict[str, Any]:
    """Project a status observation onto a raw, timestamped health fact."""

    fact: dict[str, Any] = {
        "backend_id": observation.backend.value,
        "status": observation.status.value,
        "observed_at": to_rfc3339(observation.observed_at),
    }
    if observation.expires_at is not None:
        fact["expires_at"] = to_rfc3339(observation.expires_at)
    if observation.reason is not None:
        fact["reason"] = observation.reason
    return fact


def backend_quota_fact(observation: ProviderCapacityObservation) -> dict[str, Any]:
    """Project a provider-capacity observation onto a raw quota fact."""

    fact: dict[str, Any] = {
        "backend_id": observation.backend.value,
        "observed_at": to_rfc3339(observation.observed_at),
    }
    if observation.limit is not None:
        fact["limit"] = observation.limit
    if observation.unit is not None:
        fact["unit"] = observation.unit
    if observation.source is not None:
        fact["source"] = observation.source
    if observation.expires_at is not None:
        fact["expires_at"] = to_rfc3339(observation.expires_at)
    if observation.reason is not None:
        fact["reason"] = observation.reason
    return fact


def authorization_grant_fact(grant: AuthorizationGrant) -> dict[str, Any]:
    """Project an approval grant onto raw authorization evidence."""

    if not isinstance(grant, AuthorizationGrant):
        raise StateSnapshotContractError("grant must be an AuthorizationGrant")
    fact: dict[str, Any] = {
        "grant_id": grant.grant_id,
        "scope": grant.scope,
        "actor": grant.actor,
        "issued_at": to_rfc3339(grant.issued_at),
    }
    if grant.expires_at is not None:
        fact["expires_at"] = to_rfc3339(grant.expires_at)
    if grant.source_revision is not None:
        fact["source_revision"] = grant.source_revision
    if grant.evidence:
        fact["evidence"] = list(grant.evidence)
    return fact


def to_rfc3339(value: datetime) -> str:
    """Serialize a datetime as an RFC 3339 / ISO 8601 UTC instant."""

    if not isinstance(value, datetime):
        raise StateSnapshotContractError("timestamp must be a datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# -- legacy profile translation ------------------------------------------


#: Legacy profile fact keys (superseded, explicitly translated, never guessed).
LEGACY_FACT_RETRY_ATTEMPT = "retry.attempt"
LEGACY_FACT_RETRY_BUDGET = "retry.budget"
LEGACY_FACT_ATTEMPT_NUMBER = "attempt.number"


def _legacy_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def translate_legacy_budget_facts(
    legacy_facts: Mapping[str, Any],
    *,
    default: int = 0,
) -> tuple[int, int]:
    """Explicitly translate ``retry.attempt``/``retry.budget`` to ``(count, limit)``.

    Legacy Ictus rules read ``retry.attempt``/``retry.budget`` and default
    missing values to ``0``. That default is exactly the exhausted-budget
    mismatch this translation exposes. The function never guesses: it returns
    the legacy integers (defaulting missing/invalid values to ``default``) and
    leaves policy to the caller. It is a *compatibility* helper, not the
    canonical profile.
    """

    if not isinstance(legacy_facts, Mapping):
        raise StateSnapshotContractError("legacy_facts must be a mapping")
    attempt = _legacy_int(legacy_facts.get(LEGACY_FACT_RETRY_ATTEMPT))
    budget = _legacy_int(legacy_facts.get(LEGACY_FACT_RETRY_BUDGET))
    return (
        default if attempt is None else attempt,
        default if budget is None else budget,
    )


__all__ = [
    "LEGACY_FACT_ATTEMPT_NUMBER",
    "LEGACY_FACT_RETRY_ATTEMPT",
    "LEGACY_FACT_RETRY_BUDGET",
    "SUPPORTED_SNAPSHOT_PROFILE_VERSION",
    "AuthorizationGrant",
    "BackendFacts",
    "MalformedSnapshotError",
    "SemanticBudget",
    "SnapshotPhase",
    "StateSnapshotContractError",
    "StepRetryDiagnostic",
    "UnsupportedSnapshotProfileError",
    "UnsupportedSnapshotVersionError",
    "authorization_grant_fact",
    "backend_descriptor_fact",
    "backend_quota_fact",
    "backend_status_fact",
    "to_rfc3339",
    "translate_legacy_budget_facts",
]
