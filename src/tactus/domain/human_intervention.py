"""Durable human-intervention requests, approval grants and typed resolutions.

Ownership boundary (normative)
------------------------------
Tactus owns the human-intervention lifecycle: the durable request, the
authenticated *typed* resolution, the bounded grant facts and their audit
history. Ictus owns approval **requirements** and validates grant
**sufficiency**; Tactus records grants verbatim and never asserts that a grant
was sufficient or that an ``ALLOW`` was issued.

A resolution is an authenticated typed command bound to one request and to the
exact work specification (source revision plus Tactus WorkOrder revision). It is
never an unstructured message and never an implicit unconditional approval.
Applying a resolution recomputes readiness through the normal readiness seam, so
it cannot clear unrelated blockers, dependency holds or budget constraints, and
it cannot widen scope or change route without the explicit authorization action
*and* a fresh binding that Ictus must still revalidate into a new decision.

What is deliberately **absent** here:

* no approval *requirement* policy (Ictus);
* no grant *sufficiency* verdict (Ictus);
* no execution-intent issuance (Ictus) and no lifecycle transition.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from ._clock import utcnow
from .admission import SourceRevision, WorkOrderRevision
from .readiness import ReadinessEvaluation, ReadinessFacts, apply_readiness
from .work_order import WorkOrder, WorkOrderId


def _require_non_empty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _require_aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{label} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


class HumanInterventionError(ValueError):
    """Base class for invalid human-intervention operations."""


class UnknownInterventionRequestError(HumanInterventionError):
    """A resolution referenced a request that is not durably recorded."""


class UnknownResolutionError(HumanInterventionError):
    """A lookup referenced a resolution that is not durably recorded."""


class WrongRequestError(HumanInterventionError):
    """A resolution targets a different request/WorkOrder than the request."""


class ExpiredRequestError(HumanInterventionError):
    """A resolution was submitted against an expired request."""


class ActionNotPermittedError(HumanInterventionError):
    """A resolution used a typed action the request does not permit."""


class ScopeWideningError(HumanInterventionError):
    """A resolution asserted a scope wider than the request's bound scope."""


class RouteChangeNotAuthorizedError(HumanInterventionError):
    """A resolution attempted a route change without the explicit action."""


class StaleResolutionBindingError(HumanInterventionError):
    """A request/resolution no longer matches the current source/domain revision."""


class RequestAlreadyResolvedError(HumanInterventionError):
    """A request already has one active resolution; a second is rejected."""


class ResolutionRevokedError(HumanInterventionError):
    """An operation referenced a revoked resolution."""


class ImmutableRequestConflictError(HumanInterventionError):
    """A request identity was replayed with different immutable content."""


class ImmutableResolutionConflictError(HumanInterventionError):
    """A resolution identity was replayed with different immutable content."""


class InterventionReason(str, Enum):
    """The typed human-intervention reasons (Tactus-owned coordination record).

    Mirrors the vocabulary in ``docs/architecture/HUMAN_INTERVENTION.md``. This
    is a Tactus-owned coordination record, not an Ictus decision and not an
    approval-requirement policy.
    """

    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
    SECRET_REQUIRED = "SECRET_REQUIRED"
    EXTERNAL_INPUT_REQUIRED = "EXTERNAL_INPUT_REQUIRED"
    MANUAL_SELECTION_REQUIRED = "MANUAL_SELECTION_REQUIRED"
    UNRESOLVED_RECOVERY = "UNRESOLVED_RECOVERY"
    SCOPE_CHANGE_REQUIRED = "SCOPE_CHANGE_REQUIRED"
    OTHER = "OTHER"


class HumanAction(str, Enum):
    """The closed set of typed human actions a resolution may assert.

    ``AUTHORIZE_SCOPE_CHANGE`` and ``AUTHORIZE_ROUTE_CHANGE`` are the only
    actions that may widen scope or change route, and only when the request
    explicitly permits them and the binding is fresh. They record an
    authorization for Ictus to revalidate; they never establish readiness by
    themselves.
    """

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    PROVIDE_INPUT = "PROVIDE_INPUT"
    ACKNOWLEDGE = "ACKNOWLEDGE"
    AUTHORIZE_SCOPE_CHANGE = "AUTHORIZE_SCOPE_CHANGE"
    AUTHORIZE_ROUTE_CHANGE = "AUTHORIZE_ROUTE_CHANGE"


#: Default typed actions a request permits when none are stated explicitly.
DEFAULT_PERMITTED_ACTIONS: tuple[HumanAction, ...] = (
    HumanAction.APPROVE,
    HumanAction.REJECT,
)

#: Actions that never clear a human blocker directly; Ictus must revalidate.
_REVALIDATION_ACTIONS = frozenset(
    {HumanAction.AUTHORIZE_SCOPE_CHANGE, HumanAction.AUTHORIZE_ROUTE_CHANGE}
)


@dataclass(frozen=True, slots=True)
class HumanInterventionRequest:
    """A typed, durable human-intervention request.

    ``intervention_id`` is the stable request identity. A request is bound to
    the exact work specification when ``source_revision`` and
    ``work_order_revision`` are set; a resolution against a stale binding fails
    closed.
    """

    intervention_id: str
    work_order_id: WorkOrderId
    reason: InterventionReason
    summary: str
    requested_action: str | None = None
    permitted_actions: tuple[HumanAction, ...] = DEFAULT_PERMITTED_ACTIONS
    scope: tuple[str, ...] = ()
    source_revision: SourceRevision | None = None
    work_order_revision: WorkOrderRevision | None = None
    created_at: datetime = field(default_factory=utcnow)
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "intervention_id", _require_non_empty(self.intervention_id, "intervention_id")
        )
        if not isinstance(self.work_order_id, WorkOrderId):
            object.__setattr__(self, "work_order_id", WorkOrderId(self.work_order_id))
        if not isinstance(self.reason, InterventionReason):
            raise HumanInterventionError("reason must be an InterventionReason")
        object.__setattr__(self, "summary", _require_non_empty(self.summary, "summary"))
        permitted = tuple(self.permitted_actions)
        if not permitted:
            raise HumanInterventionError("a request must permit at least one action")
        for action in permitted:
            if not isinstance(action, HumanAction):
                raise HumanInterventionError("permitted_actions must be HumanAction values")
        object.__setattr__(self, "permitted_actions", permitted)
        object.__setattr__(
            self,
            "scope",
            tuple(_require_non_empty(item, "request scope") for item in self.scope),
        )
        if self.source_revision is not None and not isinstance(
            self.source_revision, SourceRevision
        ):
            object.__setattr__(self, "source_revision", SourceRevision(self.source_revision))
        if self.work_order_revision is not None and not isinstance(
            self.work_order_revision, WorkOrderRevision
        ):
            object.__setattr__(
                self, "work_order_revision", WorkOrderRevision(self.work_order_revision)
            )
        created_at = _require_aware(self.created_at, "created_at")
        object.__setattr__(self, "created_at", created_at)
        if self.expires_at is not None:
            expires_at = _require_aware(self.expires_at, "expires_at")
            if expires_at <= created_at:
                raise HumanInterventionError("expires_at must be after created_at")
            object.__setattr__(self, "expires_at", expires_at)

    def is_expired_at(self, at: datetime) -> bool:
        return self.expires_at is not None and at >= self.expires_at


@dataclass(frozen=True, slots=True)
class HumanResolution:
    """An authenticated, typed human resolution bound to one request.

    ``resolved_at`` is the authenticated action time. ``source_revision`` and
    ``work_order_revision`` bind the action to the exact specification the human
    saw. ``route_change`` records an explicit route-change assertion; it is
    only permitted together with :attr:`HumanAction.AUTHORIZE_ROUTE_CHANGE`.
    """

    resolution_id: str
    intervention_id: str
    work_order_id: WorkOrderId
    actor: str
    action: HumanAction
    resolved_at: datetime
    scope: tuple[str, ...] = ()
    source_revision: SourceRevision | None = None
    work_order_revision: WorkOrderRevision | None = None
    route_change: bool = False
    evidence: tuple[str, ...] = ()
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    revocation_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "resolution_id", _require_non_empty(self.resolution_id, "resolution_id")
        )
        object.__setattr__(
            self, "intervention_id", _require_non_empty(self.intervention_id, "intervention_id")
        )
        if not isinstance(self.work_order_id, WorkOrderId):
            object.__setattr__(self, "work_order_id", WorkOrderId(self.work_order_id))
        object.__setattr__(self, "actor", _require_non_empty(self.actor, "actor"))
        if not isinstance(self.action, HumanAction):
            raise HumanInterventionError("action must be a HumanAction")
        object.__setattr__(self, "resolved_at", _require_aware(self.resolved_at, "resolved_at"))
        object.__setattr__(
            self,
            "scope",
            tuple(_require_non_empty(item, "resolution scope") for item in self.scope),
        )
        if self.source_revision is not None and not isinstance(
            self.source_revision, SourceRevision
        ):
            object.__setattr__(self, "source_revision", SourceRevision(self.source_revision))
        if self.work_order_revision is not None and not isinstance(
            self.work_order_revision, WorkOrderRevision
        ):
            object.__setattr__(
                self, "work_order_revision", WorkOrderRevision(self.work_order_revision)
            )
        object.__setattr__(
            self,
            "evidence",
            tuple(_require_non_empty(item, "evidence") for item in self.evidence),
        )
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", _require_aware(self.expires_at, "expires_at"))
        if self.revoked_at is not None:
            object.__setattr__(self, "revoked_at", _require_aware(self.revoked_at, "revoked_at"))

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None


@dataclass(frozen=True, slots=True)
class HumanGrant:
    """A durable, bounded approval/authorization grant fact.

    Tactus records the grant verbatim; Ictus decides whether it is sufficient.
    A grant is fresh only while not revoked and (when expiry is set) before its
    expiry.
    """

    grant_id: str
    resolution_id: str
    intervention_id: str
    work_order_id: WorkOrderId
    actor: str
    action: HumanAction
    scope: tuple[str, ...]
    source_revision: SourceRevision
    issued_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "grant_id", _require_non_empty(self.grant_id, "grant_id"))
        object.__setattr__(
            self, "resolution_id", _require_non_empty(self.resolution_id, "resolution_id")
        )
        object.__setattr__(
            self, "intervention_id", _require_non_empty(self.intervention_id, "intervention_id")
        )
        if not isinstance(self.work_order_id, WorkOrderId):
            object.__setattr__(self, "work_order_id", WorkOrderId(self.work_order_id))
        object.__setattr__(self, "actor", _require_non_empty(self.actor, "actor"))
        if not isinstance(self.action, HumanAction):
            raise HumanInterventionError("action must be a HumanAction")
        object.__setattr__(
            self, "scope", tuple(_require_non_empty(item, "grant scope") for item in self.scope)
        )
        if not isinstance(self.source_revision, SourceRevision):
            object.__setattr__(self, "source_revision", SourceRevision(self.source_revision))
        object.__setattr__(self, "issued_at", _require_aware(self.issued_at, "issued_at"))
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", _require_aware(self.expires_at, "expires_at"))
        if self.revoked_at is not None:
            object.__setattr__(self, "revoked_at", _require_aware(self.revoked_at, "revoked_at"))

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def is_fresh_at(self, at: datetime) -> bool:
        if self.is_revoked:
            return False
        return self.expires_at is None or at < self.expires_at


# -- pure validation / application -----------------------------------------


def _scope_is_subset(inner: tuple[str, ...], outer: tuple[str, ...]) -> bool:
    return set(inner).issubset(set(outer))


def validate_resolution(
    request: HumanInterventionRequest,
    resolution: HumanResolution,
    *,
    current_source_revision: SourceRevision,
    current_work_order_revision: WorkOrderRevision,
    now: datetime | None = None,
) -> None:
    """Validate a resolution against its durable request and current binding.

    Fails closed on a wrong request, expired request, unpermitted action, stale
    source/domain revision, unauthorized scope widening or unauthorized route
    change. Pure: it records nothing.
    """

    if not isinstance(request, HumanInterventionRequest):
        raise HumanInterventionError("request must be a HumanInterventionRequest")
    if not isinstance(resolution, HumanResolution):
        raise HumanInterventionError("resolution must be a HumanResolution")
    if not isinstance(current_source_revision, SourceRevision):
        current_source_revision = SourceRevision(current_source_revision)
    if not isinstance(current_work_order_revision, WorkOrderRevision):
        current_work_order_revision = WorkOrderRevision(current_work_order_revision)

    if resolution.intervention_id != request.intervention_id:
        raise WrongRequestError(
            f"resolution targets request {resolution.intervention_id!r}, "
            f"not {request.intervention_id!r}"
        )
    if resolution.work_order_id != request.work_order_id:
        raise WrongRequestError(
            f"resolution targets WorkOrder {resolution.work_order_id}, "
            f"not {request.work_order_id}"
        )

    at = now if now is not None else resolution.resolved_at
    if request.is_expired_at(at):
        raise ExpiredRequestError(f"request {request.intervention_id!r} expired at {request.expires_at}")

    if resolution.action not in request.permitted_actions:
        raise ActionNotPermittedError(
            f"action {resolution.action.value} is not permitted by request "
            f"{request.intervention_id!r}"
        )

    # Binding: the request and the human action must both target the current
    # source/domain revision. A concurrent source edit invalidates the request.
    if request.source_revision is not None and request.source_revision != current_source_revision:
        raise StaleResolutionBindingError(
            "request is bound to a stale source revision "
            f"({request.source_revision} != {current_source_revision})"
        )
    if (
        request.work_order_revision is not None
        and request.work_order_revision != current_work_order_revision
    ):
        raise StaleResolutionBindingError(
            "request is bound to a stale WorkOrder revision "
            f"({request.work_order_revision.value} != {current_work_order_revision.value})"
        )
    if resolution.source_revision != current_source_revision:
        raise StaleResolutionBindingError(
            "resolution is bound to a stale source revision "
            f"({resolution.source_revision} != {current_source_revision})"
        )
    if resolution.work_order_revision != current_work_order_revision:
        raise StaleResolutionBindingError(
            "resolution is bound to a stale WorkOrder revision "
            f"({resolution.work_order_revision!r} != {current_work_order_revision!r})"
        )

    # Scope: a resolution may never widen the request's bounded scope unless it
    # explicitly uses the scope-change authorization action.
    if not _scope_is_subset(resolution.scope, request.scope):
        if resolution.action is not HumanAction.AUTHORIZE_SCOPE_CHANGE:
            raise ScopeWideningError(
                "resolution scope must be bounded by the request scope without "
                "AUTHORIZE_SCOPE_CHANGE"
            )

    # Route: a route change requires the explicit route-change authorization.
    if resolution.route_change and resolution.action is not HumanAction.AUTHORIZE_ROUTE_CHANGE:
        raise RouteChangeNotAuthorizedError(
            "a route change requires the AUTHORIZE_ROUTE_CHANGE action"
        )


def is_revalidation_resolution(resolution: HumanResolution) -> bool:
    """Whether ``resolution`` only authorizes revalidation rather than unblocking."""

    return resolution.action in _REVALIDATION_ACTIONS or resolution.route_change


def readiness_facts_with_resolution(
    facts: ReadinessFacts,
    request: HumanInterventionRequest,
    resolution: HumanResolution,
) -> ReadinessFacts:
    """Derive the readiness facts after applying one accepted resolution.

    Only the human prerequisite named by the request's typed reason is relaxed.
    Every other prerequisite (dependencies, backend/route, source currency,
    integration/delivery) is left exactly as supplied, so an unrelated blocker
    or dependency hold still blocks the Work Order. An explicit scope/route
    authorization clears nothing: it is a grant for Ictus to revalidate.
    """

    if resolution.is_revoked:
        raise ResolutionRevokedError(f"resolution {resolution.resolution_id!r} is revoked")
    if is_revalidation_resolution(resolution):
        return facts
    if request.reason is InterventionReason.APPROVAL_REQUIRED:
        return replace(facts, approval_required=False)
    return replace(facts, human_input_required=False)


def apply_resolution_to_readiness(
    work_order: WorkOrder,
    request: HumanInterventionRequest,
    resolution: HumanResolution,
    *,
    facts: ReadinessFacts,
    at: datetime | None = None,
) -> ReadinessEvaluation:
    """Apply an accepted resolution to an ``OPEN`` WorkOrder's readiness.

    Readiness is recomputed from the *complete* facts, so unrelated blockers
    survive. This performs no lifecycle transition and issues no decision.
    """

    updated = readiness_facts_with_resolution(facts, request, resolution)
    return apply_readiness(work_order, updated, at=at)


# -- persistence boundary ---------------------------------------------------


@runtime_checkable
class HumanInterventionPort(Protocol):
    """Durable boundary for Tactus-owned human-intervention records.

    It records requests, resolutions and the bounded grants they produce.  It
    contains no approval-requirement policy and no grant-sufficiency verdict.
    """

    def record_request(self, request: HumanInterventionRequest) -> HumanInterventionRequest:
        """Durably record a request; a replay returns the original record."""

        ...

    def request(self, intervention_id: str) -> HumanInterventionRequest:
        """Load a durably recorded request."""

        ...

    def resolve(
        self,
        resolution: HumanResolution,
        *,
        current_source_revision: SourceRevision,
        current_work_order_revision: WorkOrderRevision,
    ) -> tuple[HumanResolution, HumanGrant]:
        """Validate against the stored request and record one effective grant."""

        ...

    def resolution(self, resolution_id: str) -> HumanResolution:
        """Load a durably recorded resolution."""

        ...

    def resolutions_for(self, intervention_id: str) -> tuple[HumanResolution, ...]:
        """All resolutions recorded for one request, in record order."""

        ...

    def grants_for(
        self, work_order_id: WorkOrderId | str, *, include_revoked: bool = False
    ) -> tuple[HumanGrant, ...]:
        """Grants for one WorkOrder; revoked grants are excluded by default."""

        ...

    def revoke(
        self, resolution_id: str, *, at: datetime | None = None, reason: str = ""
    ) -> tuple[HumanResolution, HumanGrant]:
        """Revoke a resolution and its grant; replay is idempotent."""

        ...


__all__ = [
    "ActionNotPermittedError",
    "DEFAULT_PERMITTED_ACTIONS",
    "ExpiredRequestError",
    "HumanAction",
    "HumanGrant",
    "HumanInterventionError",
    "HumanInterventionPort",
    "HumanInterventionRequest",
    "HumanResolution",
    "ImmutableRequestConflictError",
    "ImmutableResolutionConflictError",
    "InterventionReason",
    "RequestAlreadyResolvedError",
    "ResolutionRevokedError",
    "RouteChangeNotAuthorizedError",
    "ScopeWideningError",
    "StaleResolutionBindingError",
    "UnknownInterventionRequestError",
    "UnknownResolutionError",
    "WrongRequestError",
    "apply_resolution_to_readiness",
    "is_revalidation_resolution",
    "readiness_facts_with_resolution",
    "validate_resolution",
]
