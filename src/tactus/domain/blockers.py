"""Typed Work Order blockers and independent unblock semantics.

Ownership (normative)
---------------------
Tactus owns ``OPEN`` readiness and the blocker evidence attached to it. A
``BLOCKED`` Work Order is blocked *by one or more typed blockers*; clearing one
blocker never clears another. This module provides the pure value types:

* a closed :class:`BlockReason` vocabulary;
* immutable :class:`Blocker` records carrying reason, evidence, timestamps and a
  typed :class:`ResolutionCondition`;
* a :class:`BlockerSet` that resolves blockers independently.

What is deliberately **absent** here:

* no failure lifecycle state (failure lives in ``FailureObservation``);
* no backend ranking / route selection (Ictus owns routing);
* no semantic recovery selector (Ictus owns diagnosis/policy);
* no Dagster execution-concurrency state (Dagster owns capacity).

Blockers are readiness evidence. They are *not* lifecycle states.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum

from ._clock import utcnow
from .records import TransitionAuthority


class BlockerError(ValueError):
    """Base class for invalid blocker operations."""


class InvalidBlockerError(BlockerError):
    """A blocker is missing required typed information."""


class DuplicateBlockerError(BlockerError):
    """The same blocker identity was added to a set twice."""


class UnknownBlockerError(BlockerError):
    """An operation referenced a blocker that is not present."""


class AlreadyResolvedBlockerError(BlockerError):
    """A blocker that is already resolved was resolved again."""


class BlockReason(str, Enum):
    """The closed set of top-level blocker reasons.

    Mirrors ``docs/architecture/WORK_ORDER_LIFECYCLE.md``. ``OTHER`` is the
    escape hatch and therefore requires an explicit free-form ``detail`` so a
    blocker is never untyped.
    """

    DEPENDENCY = "DEPENDENCY"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    HUMAN_INPUT_REQUIRED = "HUMAN_INPUT_REQUIRED"
    INTEGRATION_BLOCKED = "INTEGRATION_BLOCKED"
    DELIVERY_BLOCKED = "DELIVERY_BLOCKED"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    OTHER = "OTHER"

    @property
    def requires_detail(self) -> bool:
        return self is BlockReason.OTHER


class ResolutionKind(str, Enum):
    """The closed set of typed resolution-condition kinds.

    A resolution condition says *what must become true* for a blocker to be
    clearable. It is data, not an action selector: it does not choose retries,
    backends or recovery paths.
    """

    DEPENDENCY_RESOLVED = "DEPENDENCY_RESOLVED"
    BACKEND_AVAILABLE = "BACKEND_AVAILABLE"
    HUMAN_INPUT_PROVIDED = "HUMAN_INPUT_PROVIDED"
    INTEGRATION_READY = "INTEGRATION_READY"
    DELIVERY_READY = "DELIVERY_READY"
    SOURCE_REFRESHED = "SOURCE_REFRESHED"
    EXPLICIT = "EXPLICIT"


#: Default resolution condition for each reason. Total over the closed reason
#: vocabulary so every blocker always carries a typed resolution condition.
_RESOLUTION_FOR_REASON: dict[BlockReason, ResolutionKind] = {
    BlockReason.DEPENDENCY: ResolutionKind.DEPENDENCY_RESOLVED,
    BlockReason.BACKEND_UNAVAILABLE: ResolutionKind.BACKEND_AVAILABLE,
    BlockReason.HUMAN_INPUT_REQUIRED: ResolutionKind.HUMAN_INPUT_PROVIDED,
    BlockReason.INTEGRATION_BLOCKED: ResolutionKind.INTEGRATION_READY,
    BlockReason.DELIVERY_BLOCKED: ResolutionKind.DELIVERY_READY,
    BlockReason.SOURCE_CHANGED: ResolutionKind.SOURCE_REFRESHED,
    BlockReason.OTHER: ResolutionKind.EXPLICIT,
}


@dataclass(frozen=True, slots=True)
class ResolutionCondition:
    """Typed condition that must hold before a blocker can be cleared."""

    kind: ResolutionKind
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ResolutionKind):
            raise InvalidBlockerError("resolution kind must be a ResolutionKind")
        if self.kind is ResolutionKind.EXPLICIT and not self.detail.strip():
            raise InvalidBlockerError(
                "an EXPLICIT resolution condition requires explicit detail"
            )


def default_resolution_condition(
    reason: BlockReason, *, detail: str = ""
) -> ResolutionCondition:
    """Return the typed resolution condition implied by ``reason``.

    ``OTHER`` maps to an ``EXPLICIT`` condition, so the blocker's own explicit
    detail is carried into the resolution condition.
    """

    kind = _RESOLUTION_FOR_REASON[reason]
    if kind is ResolutionKind.EXPLICIT:
        return ResolutionCondition(
            kind=kind, detail=detail or "explicit resolution required"
        )
    return ResolutionCondition(kind=kind)


@dataclass(frozen=True, slots=True)
class BlockerId:
    """Stable, validated identity for a single blocker."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("BlockerId must be a non-empty string")
        object.__setattr__(self, "value", self.value.strip())

    def __str__(self) -> str:
        return self.value


def coerce_blocker_id(value: BlockerId | str) -> BlockerId:
    return value if isinstance(value, BlockerId) else BlockerId(value)


@dataclass(frozen=True, slots=True)
class Blocker:
    """An immutable typed blocker record.

    A blocker always records *why* it exists (``reason``/``subreason``), the
    evidence that established it, when it became active and the typed condition
    that would clear it. Resolution adds ``resolved_at``/``resolved_by`` without
    discarding the original evidence.
    """

    reason: BlockReason
    detail: str = ""
    subreason: str | None = None
    evidence: Mapping[str, str] = field(default_factory=dict)
    blocked_at: datetime = field(default_factory=utcnow)
    resolution: ResolutionCondition | None = None
    resolution_note: str = ""
    blocker_id: BlockerId | str = field(
        default_factory=lambda: BlockerId(uuid.uuid4().hex)
    )
    resolved_at: datetime | None = None
    resolved_by: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reason, BlockReason):
            raise InvalidBlockerError("blocker reason must be a BlockReason")
        if self.reason.requires_detail and not self.detail.strip():
            raise InvalidBlockerError(
                f"blocker reason {self.reason.value} requires explicit detail"
            )
        if self.resolution is None:
            object.__setattr__(
                self,
                "resolution",
                default_resolution_condition(self.reason, detail=self.detail),
            )
        elif not isinstance(self.resolution, ResolutionCondition):
            raise InvalidBlockerError("resolution must be a ResolutionCondition")
        if self.resolved_at is None and self.resolved_by is not None:
            raise InvalidBlockerError("resolved_by requires resolved_at")
        if self.resolved_at is not None and self.resolved_by is None:
            raise InvalidBlockerError("resolved_at requires resolved_by")
        object.__setattr__(self, "blocker_id", coerce_blocker_id(self.blocker_id))
        object.__setattr__(self, "evidence", dict(self.evidence))

    @property
    def is_resolved(self) -> bool:
        return self.resolved_at is not None

    def resolve(
        self,
        *,
        at: datetime,
        by: TransitionAuthority | str,
        note: str = "",
    ) -> "Blocker":
        """Return a resolved copy of this blocker.

        Resolving is refused if the blocker is already resolved. The original
        blocker is immutable, so the caller decides whether to keep history.
        """

        if self.is_resolved:
            raise AlreadyResolvedBlockerError(
                f"blocker already resolved: {self.blocker_id}"
            )
        resolved_by = by.value if isinstance(by, TransitionAuthority) else by
        return replace(
            self,
            resolved_at=at,
            resolved_by=resolved_by,
            resolution_note=note,
        )


class BlockerSet:
    """Ordered, independently-resolvable collection of blockers.

    Resolving one blocker leaves every other blocker active: unblocking is
    per-blocker, and readiness is only recomputed by the caller after the whole
    set is inspected.
    """

    def __init__(self, blockers: Iterable[Blocker] = ()) -> None:
        self._blockers: dict[BlockerId, Blocker] = {}
        for blocker in blockers:
            self.add(blocker)

    # -- read-only views --------------------------------------------------

    def __len__(self) -> int:
        return len(self._blockers)

    def __iter__(self):
        return iter(self._blockers.values())

    def __contains__(self, value: object) -> bool:
        if isinstance(value, Blocker):
            return self._blockers.get(value.blocker_id) == value
        try:
            key = coerce_blocker_id(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return key in self._blockers

    @property
    def all(self) -> tuple[Blocker, ...]:
        """Every blocker in insertion order, resolved or not."""

        return tuple(self._blockers.values())

    @property
    def active(self) -> tuple[Blocker, ...]:
        """The unresolved blockers, in insertion order."""

        return tuple(b for b in self._blockers.values() if not b.is_resolved)

    @property
    def resolved(self) -> tuple[Blocker, ...]:
        """The resolved blockers, in insertion order."""

        return tuple(b for b in self._blockers.values() if b.is_resolved)

    @property
    def has_active(self) -> bool:
        return any(not blocker.is_resolved for blocker in self._blockers.values())

    # -- mutation ---------------------------------------------------------

    def add(self, blocker: Blocker) -> Blocker:
        if not isinstance(blocker, Blocker):
            raise BlockerError("blocker must be a Blocker")
        if blocker.blocker_id in self._blockers:
            raise DuplicateBlockerError(f"duplicate blocker id: {blocker.blocker_id}")
        self._blockers[blocker.blocker_id] = blocker
        return blocker

    def resolve(
        self,
        blocker_id: BlockerId | str,
        *,
        at: datetime | None = None,
        authority: TransitionAuthority | str = TransitionAuthority.OPERATOR,
        note: str = "",
    ) -> Blocker:
        """Resolve exactly one blocker, leaving all others untouched."""

        key = coerce_blocker_id(blocker_id)
        blocker = self._blockers.get(key)
        if blocker is None:
            raise UnknownBlockerError(f"unknown blocker: {key}")
        resolved = blocker.resolve(
            at=at if at is not None else utcnow(),
            by=authority,
            note=note,
        )
        self._blockers[key] = resolved
        return resolved

    def replace(self, blockers: Iterable[Blocker]) -> None:
        """Atomically replace the whole set (used by readiness re-evaluation).

        The candidate is validated for duplicate identities before any mutation,
        so a rejected replacement leaves the current set unchanged.
        """

        materialized = list(blockers)
        ids = [blocker.blocker_id for blocker in materialized]
        if len(ids) != len(set(ids)):
            raise DuplicateBlockerError("duplicate blocker id in replacement set")
        self._blockers = {blocker.blocker_id: blocker for blocker in materialized}


__all__ = [
    "AlreadyResolvedBlockerError",
    "BlockReason",
    "Blocker",
    "BlockerError",
    "BlockerId",
    "BlockerSet",
    "DuplicateBlockerError",
    "InvalidBlockerError",
    "ResolutionCondition",
    "ResolutionKind",
    "UnknownBlockerError",
    "coerce_blocker_id",
    "default_resolution_condition",
]
