"""Backend registry: factual description of the backends that exist.

This module owns *facts*, not routing policy. A backend descriptor says what a
backend is and what it supports; ``compatible()`` performs a purely mechanical
hard-constraint match. Ranking, preference, cost and effort trade-offs belong
to the decision plane (Ictus), not here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True, slots=True)
class BackendId:
    """Stable, validated identity for a backend."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("BackendId must be a non-empty string")
        object.__setattr__(self, "value", self.value.strip())

    def __str__(self) -> str:
        return self.value


def coerce_backend_id(value: BackendId | str) -> BackendId:
    return value if isinstance(value, BackendId) else BackendId(value)


class EffortLevel(str, Enum):
    """Coarse, backend-declared effort tier.

    Optional: not every backend exposes an effort tier. Where present it is a
    hard compatibility constraint when a requirement specifies one, never a
    preference.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class BackendDescriptor:
    """Static, factual description of a backend.

    Fields are deliberately generic tags rather than product names so core
    contracts stay provider-neutral.
    """

    backend_id: BackendId
    capabilities: frozenset[str] = frozenset()
    agent_runtime: str | None = None
    model: str | None = None
    provider: str | None = None
    effort: EffortLevel | None = None


@dataclass(frozen=True, slots=True)
class BackendRequirements:
    """Hard compatibility requirements for a backend.

    Every specified field is an equality/inclusion constraint. Unset fields
    are unconstrained.
    """

    required_capabilities: frozenset[str] = frozenset()
    agent_runtime: str | None = None
    model: str | None = None
    provider: str | None = None
    effort: EffortLevel | None = None


class BackendError(ValueError):
    """Base class for backend registry errors."""


class DuplicateBackendError(BackendError):
    """A backend with the same identity is already registered."""


class UnknownBackendError(BackendError):
    """A backend identity is not registered."""


class BackendRegistry:
    """Registry of known backends.

    Compatibility is intentionally independent from backend status (health) and
    from capacity. Those are separate sources of operational truth.
    """

    def __init__(self, descriptors: Iterable[BackendDescriptor] = ()) -> None:
        self._descriptors: dict[BackendId, BackendDescriptor] = {}
        for descriptor in descriptors:
            self.register(descriptor)

    def register(self, descriptor: BackendDescriptor) -> None:
        if descriptor.backend_id in self._descriptors:
            raise DuplicateBackendError(
                f"backend already registered: {descriptor.backend_id}"
            )
        self._descriptors[descriptor.backend_id] = descriptor

    def get(self, backend: BackendId | str) -> BackendDescriptor:
        backend_id = coerce_backend_id(backend)
        try:
            return self._descriptors[backend_id]
        except KeyError:
            raise UnknownBackendError(f"unknown backend: {backend_id}") from None

    def all(self) -> tuple[BackendDescriptor, ...]:
        """All registered descriptors in deterministic identity order."""

        return tuple(
            self._descriptors[key]
            for key in sorted(self._descriptors, key=lambda backend: backend.value)
        )

    def compatible(self, requirements: BackendRequirements) -> tuple[BackendDescriptor, ...]:
        """Descriptors satisfying the hard requirements.

        This is a mechanical match only: it does not consider health, capacity,
        quota, ranking or preference.
        """

        return tuple(
            descriptor
            for descriptor in self.all()
            if _is_compatible(descriptor, requirements)
        )


def _is_compatible(
    descriptor: BackendDescriptor, requirements: BackendRequirements
) -> bool:
    if not requirements.required_capabilities <= descriptor.capabilities:
        return False
    for attribute in ("agent_runtime", "model", "provider", "effort"):
        required = getattr(requirements, attribute)
        if required is not None and required != getattr(descriptor, attribute):
            return False
    return True
