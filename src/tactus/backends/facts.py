"""Versioned backend fact records for Tactus -> Ictus context.

These records are factual inputs, not routing decisions. They deliberately do
not encode compatibility, ranking, preference, selected routes, worker slots or
Dagster concurrency. The ``to_fact()`` methods return plain JSON-compatible
mappings suitable for use as values under the stable Ictus StateSnapshot fact
collection keys.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .capacity import ProviderCapacityObservation
from .health import BackendHealth, BackendStatusObservation
from .registry import BackendDescriptor, BackendId, coerce_backend_id

BACKEND_FACT_SCHEMA_VERSION = 1

FACT_BACKEND_DESCRIPTORS = "backend.descriptors"
FACT_BACKEND_HEALTH = "backend.health"
FACT_BACKEND_ADMINISTRATIVE_ENABLEMENT = "backend.administrative_enablement"
FACT_BACKEND_PROVIDER_QUOTA = "backend.provider_quota"

UNKNOWN_HEALTH = "UNKNOWN"


def _to_rfc3339(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    normalized = normalized.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z")


def _non_empty(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_non_empty(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _non_empty(value, field)


def _immutable_strings(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    normalized = tuple(values)
    for item in normalized:
        _non_empty(item, field)
    return normalized


@dataclass(frozen=True, slots=True)
class BackendDescriptorFact:
    """A declared backend descriptor, with provenance and validity metadata."""

    backend: BackendId
    capabilities: tuple[str, ...] = ()
    runtime: str | None = None
    model: str | None = None
    provider: str | None = None
    constraints: tuple[str, ...] = ()
    provenance: str | None = None
    observed_at: datetime | None = None
    expires_at: datetime | None = None
    schema_version: int = BACKEND_FACT_SCHEMA_VERSION

    @classmethod
    def from_descriptor(
        cls,
        descriptor: BackendDescriptor,
        *,
        provenance: str | None = None,
        observed_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> "BackendDescriptorFact":
        return cls(
            backend=descriptor.backend_id,
            capabilities=tuple(sorted(descriptor.capabilities)),
            runtime=descriptor.agent_runtime,
            model=descriptor.model,
            provider=descriptor.provider,
            constraints=tuple(sorted(descriptor.constraints)),
            provenance=provenance,
            observed_at=observed_at,
            expires_at=expires_at,
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "backend", coerce_backend_id(self.backend))
        object.__setattr__(
            self, "capabilities", _immutable_strings(self.capabilities, "capability")
        )
        object.__setattr__(
            self, "constraints", _immutable_strings(self.constraints, "constraint")
        )
        object.__setattr__(
            self, "runtime", _optional_non_empty(self.runtime, "runtime")
        )
        object.__setattr__(self, "model", _optional_non_empty(self.model, "model"))
        object.__setattr__(
            self, "provider", _optional_non_empty(self.provider, "provider")
        )
        object.__setattr__(
            self, "provenance", _optional_non_empty(self.provenance, "provenance")
        )

    def to_fact(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "backend_id": self.backend.value,
            "capabilities": list(self.capabilities),
            "runtime": self.runtime,
            "model": self.model,
            "provider": self.provider,
            "constraints": list(self.constraints),
            "provenance": self.provenance,
            "observed_at": _to_rfc3339(self.observed_at),
            "expires_at": _to_rfc3339(self.expires_at),
        }


@dataclass(frozen=True, slots=True)
class BackendHealthFact:
    """Latest observed backend health, or explicit UNKNOWN."""

    backend: BackendId
    health: BackendHealth | None
    provenance: str | None = None
    observed_at: datetime | None = None
    expires_at: datetime | None = None
    reason: str | None = None
    schema_version: int = BACKEND_FACT_SCHEMA_VERSION

    @classmethod
    def from_observation(
        cls,
        observation: BackendStatusObservation | None,
        *,
        backend: BackendId | str | None = None,
        at: datetime | None = None,
        provenance: str | None = None,
    ) -> "BackendHealthFact":
        if observation is None:
            if backend is None:
                raise ValueError("backend is required when observation is absent")
            return cls(
                backend=coerce_backend_id(backend),
                health=None,
                provenance=provenance,
            )
        fresh = at is None or observation.is_fresh_at(at)
        return cls(
            backend=observation.backend,
            health=observation.status if fresh else None,
            provenance=provenance,
            observed_at=observation.observed_at,
            expires_at=observation.expires_at,
            reason=observation.reason,
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "backend", coerce_backend_id(self.backend))
        if self.health is not None and not isinstance(self.health, BackendHealth):
            raise ValueError("health must be a BackendHealth or None")
        object.__setattr__(
            self, "provenance", _optional_non_empty(self.provenance, "provenance")
        )
        object.__setattr__(self, "reason", _optional_non_empty(self.reason, "reason"))

    def to_fact(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "backend_id": self.backend.value,
            "health": self.health.value if self.health is not None else UNKNOWN_HEALTH,
            "provenance": self.provenance,
            "observed_at": _to_rfc3339(self.observed_at),
            "expires_at": _to_rfc3339(self.expires_at),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class BackendAdministrativeEnablementFact:
    """Operator/admin enablement, independent from observed backend health."""

    backend: BackendId
    enabled: bool
    provenance: str | None = None
    observed_at: datetime | None = None
    expires_at: datetime | None = None
    reason: str | None = None
    schema_version: int = BACKEND_FACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "backend", coerce_backend_id(self.backend))
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")
        object.__setattr__(
            self, "provenance", _optional_non_empty(self.provenance, "provenance")
        )
        object.__setattr__(self, "reason", _optional_non_empty(self.reason, "reason"))

    def to_fact(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "backend_id": self.backend.value,
            "enabled": self.enabled,
            "provenance": self.provenance,
            "observed_at": _to_rfc3339(self.observed_at),
            "expires_at": _to_rfc3339(self.expires_at),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class BackendProviderQuotaFact:
    """Externally reported provider quota/capacity fact.

    This is not an execution-slot fact and exposes no occupancy fields.
    """

    backend: BackendId
    limit: int | None = None
    unit: str | None = None
    provenance: str | None = None
    observed_at: datetime | None = None
    expires_at: datetime | None = None
    reason: str | None = None
    schema_version: int = BACKEND_FACT_SCHEMA_VERSION

    @classmethod
    def from_observation(
        cls, observation: ProviderCapacityObservation
    ) -> "BackendProviderQuotaFact":
        return cls(
            backend=observation.backend,
            limit=observation.limit,
            unit=observation.unit,
            provenance=observation.source,
            observed_at=observation.observed_at,
            expires_at=observation.expires_at,
            reason=observation.reason,
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "backend", coerce_backend_id(self.backend))
        if self.limit is not None and self.limit < 0:
            raise ValueError("provider quota limit must be non-negative")
        object.__setattr__(self, "unit", _optional_non_empty(self.unit, "unit"))
        object.__setattr__(
            self, "provenance", _optional_non_empty(self.provenance, "provenance")
        )
        object.__setattr__(self, "reason", _optional_non_empty(self.reason, "reason"))

    def to_fact(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "backend_id": self.backend.value,
            "limit": self.limit,
            "unit": self.unit,
            "provenance": self.provenance,
            "observed_at": _to_rfc3339(self.observed_at),
            "expires_at": _to_rfc3339(self.expires_at),
            "reason": self.reason,
        }
