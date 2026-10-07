"""Readiness derivation and the narrow readiness-application seam.

This module turns validated prerequisites ("facts") into typed
:class:`~tactus.domain.blockers.Blocker` values and applies them to an ``OPEN``
:class:`~tactus.domain.work_order.WorkOrder`, recomputing readiness.

Ownership boundary (normative):

* Tactus owns ``OPEN`` readiness and blocker evidence;
* Ictus owns routing/diagnosis policy and therefore produces the validated
  no-route / approval facts;
* Dagster owns execution concurrency. A busy queue/capacity is **not** a Work
  Order blocker and is deliberately ignored when deriving blockers.

This module performs no backend ranking, no recovery selection and introduces no
failure lifecycle state.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from ._clock import utcnow
from .blockers import BlockReason, Blocker
from .work_order import OpenStatus, WorkOrder


@dataclass(frozen=True, slots=True)
class AuthenticatedHold:
    """A hold on an ``OPEN`` Work Order established by a validated authority.

    A hold is an input fact, not a decision: an authenticated operator/system
    has asserted that the Work Order must remain blocked for the stated reason
    until the hold is explicitly released. It converts mechanically to a typed
    :class:`Blocker`.
    """

    reason: BlockReason
    detail: str = ""
    subreason: str | None = None
    evidence: Mapping[str, str] = field(default_factory=dict)
    held_at: datetime = field(default_factory=utcnow)
    authenticated_by: str = "system"

    def __post_init__(self) -> None:
        if not isinstance(self.reason, BlockReason):
            raise ValueError("hold reason must be a BlockReason")
        object.__setattr__(self, "evidence", dict(self.evidence))

    def to_blocker(self) -> Blocker:
        return Blocker(
            reason=self.reason,
            detail=self.detail,
            subreason=self.subreason,
            evidence=self.evidence,
            blocked_at=self.held_at,
        )


@dataclass(frozen=True, slots=True)
class ReadinessFacts:
    """Validated readiness inputs for one ``OPEN`` Work Order.

    ``None`` means "not evaluated / unknown" for that prerequisite and never
    produces a blocker by itself. Callers pass a complete snapshot; each ``False``
    yields a typed blocker.

    ``capacity_busy`` is Dagster-owned execution-concurrency state. It is
    accepted only so a caller can pass a complete snapshot and is *ignored* by
    :func:`derive_blockers`: waiting for a Dagster slot leaves the Work Order
    ``READY``.
    """

    dependencies_satisfied: bool | None = None
    backend_available: bool | None = None
    route_available: bool | None = None
    source_current: bool | None = None
    integration_ready: bool | None = None
    delivery_ready: bool | None = None
    approval_required: bool = False
    human_input_required: bool = False
    holds: tuple[AuthenticatedHold, ...] = ()
    capacity_busy: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "holds", tuple(self.holds))


def derive_blockers(
    facts: ReadinessFacts,
    *,
    at: datetime | None = None,
) -> tuple[Blocker, ...]:
    """Derive the complete typed blocker set from validated readiness facts.

    Pure and total. ``capacity_busy`` alone never produces a blocker.
    """

    blocked_at = at if at is not None else utcnow()
    blockers: list[Blocker] = []

    if facts.dependencies_satisfied is False:
        blockers.append(
            Blocker(
                reason=BlockReason.DEPENDENCY,
                detail="domain prerequisites are not satisfied",
                evidence={"dependencies_satisfied": "false"},
                blocked_at=blocked_at,
            )
        )

    if facts.backend_available is False:
        blockers.append(
            Blocker(
                reason=BlockReason.BACKEND_UNAVAILABLE,
                detail="no backend is currently available",
                evidence={"backend_available": "false"},
                blocked_at=blocked_at,
            )
        )

    if facts.route_available is False:
        blockers.append(
            Blocker(
                reason=BlockReason.BACKEND_UNAVAILABLE,
                detail="no validated route to a compatible backend",
                subreason="NO_ROUTE",
                evidence={"route_available": "false"},
                blocked_at=blocked_at,
            )
        )

    if facts.source_current is False:
        blockers.append(
            Blocker(
                reason=BlockReason.SOURCE_CHANGED,
                detail="the Work Order source revision is stale",
                evidence={"source_current": "false"},
                blocked_at=blocked_at,
            )
        )

    if facts.integration_ready is False:
        blockers.append(
            Blocker(
                reason=BlockReason.INTEGRATION_BLOCKED,
                detail="integration prerequisites are not ready",
                evidence={"integration_ready": "false"},
                blocked_at=blocked_at,
            )
        )

    if facts.delivery_ready is False:
        blockers.append(
            Blocker(
                reason=BlockReason.DELIVERY_BLOCKED,
                detail="delivery prerequisites are not ready",
                subreason="DELIVERY",
                evidence={"delivery_ready": "false"},
                blocked_at=blocked_at,
            )
        )

    if facts.approval_required:
        blockers.append(
            Blocker(
                reason=BlockReason.HUMAN_INPUT_REQUIRED,
                detail="explicit approval is required before execution",
                subreason="APPROVAL_REQUIRED",
                evidence={"approval_required": "true"},
                blocked_at=blocked_at,
            )
        )

    if facts.human_input_required:
        blockers.append(
            Blocker(
                reason=BlockReason.HUMAN_INPUT_REQUIRED,
                detail="external human input is required",
                subreason="EXTERNAL_INPUT_REQUIRED",
                evidence={"human_input_required": "true"},
                blocked_at=blocked_at,
            )
        )

    for hold in facts.holds:
        blockers.append(hold.to_blocker())

    return tuple(blockers)


def readiness_status(blockers: Iterable[Blocker]) -> OpenStatus:
    """Recompute readiness from a blocker set.

    ``BLOCKED`` while any blocker is active, ``READY`` once none remain. It
    never yields ``UNKNOWN``: ``UNKNOWN`` means readiness has not been evaluated
    yet.
    """

    return (
        OpenStatus.BLOCKED
        if any(not blocker.is_resolved for blocker in blockers)
        else OpenStatus.READY
    )


@dataclass(frozen=True, slots=True)
class ReadinessEvaluation:
    """The result of deriving and applying readiness facts."""

    status: OpenStatus
    blockers: tuple[Blocker, ...]
    evaluated_at: datetime


def evaluate_readiness(
    facts: ReadinessFacts,
    *,
    at: datetime | None = None,
) -> ReadinessEvaluation:
    """Derive blockers from ``facts`` and compute readiness, without applying."""

    evaluated_at = at if at is not None else utcnow()
    blockers = derive_blockers(facts, at=evaluated_at)
    return ReadinessEvaluation(
        status=readiness_status(blockers),
        blockers=blockers,
        evaluated_at=evaluated_at,
    )


def apply_readiness(
    work_order: WorkOrder,
    facts: ReadinessFacts,
    *,
    at: datetime | None = None,
) -> ReadinessEvaluation:
    """Apply derived blockers to ``work_order`` and recompute its readiness.

    The Work Order's blocker set is replaced by the freshly derived set, so
    readiness always reflects the latest validated facts. Per-blocker resolution
    remains available on the Work Order between evaluations.
    """

    evaluation = evaluate_readiness(facts, at=at)
    work_order.replace_blockers(evaluation.blockers)
    return evaluation


__all__ = [
    "AuthenticatedHold",
    "ReadinessEvaluation",
    "ReadinessFacts",
    "apply_readiness",
    "derive_blockers",
    "evaluate_readiness",
    "readiness_status",
]
