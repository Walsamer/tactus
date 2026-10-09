"""Apply validated generic Ictus decisions to Work Order lifecycle/readiness.

Ownership boundary (normative)
------------------------------
Ictus owns the decision vocabulary, diagnosis and policy. Tactus owns the Work
Order lifecycle/readiness and therefore owns the **explicit mapping** from a
validated generic decision to a lifecycle transition / readiness effect plus an
auditable application record. This module applies; it never decides.

The mapping is static, total over the frozen vocabulary and closed::

    Ictus decision        Tactus effect
    --------------------  ------------------------------------------------
    REEXECUTE             ACTIVE -> OPEN + READY   (new semantic attempt)
    ROUTE                 ACTIVE -> OPEN + READY   (+ preserved route constraints)
    EXECUTE_CAPABILITY    ACTIVE -> OPEN + READY   (+ validated follow-up capability)
    ESCALATE              ACTIVE -> OPEN + BLOCKED (+ typed intervention request)
    ABORT                 ACTIVE -> RETIRED
    DECOMPOSE             bounded child plan -> children durably created ->
                          parent ACTIVE -> RETIRED (parent retired last)

Effects are applied only through the guard-checked lifecycle/readiness operations
on :class:`~tactus.domain.work_order.WorkOrder`; the module never mutates private
state and never invents a lifecycle state. Readiness changes are not lifecycle
transitions (``OPEN`` readiness is orthogonal to the five-state lifecycle).

What is deliberately **absent** here:

* no diagnosis / classification of failures (Ictus);
* no retry/reroute/decompose/escalate *policy* (Ictus);
* no backend ranking/selection (Ictus);
* no capability validation policy (Ictus);
* no Dagster execution mechanics (Dagster);
* no mutation of a Work Order by Ictus (Tactus applies the validated decision).

The typed ``BlockReason``/unblock work (issue #3) is an external prerequisite for
a WorkOrder-level typed block reason. This module does not re-implement it: the
typed reason for an ``ESCALATE`` is carried by the Tactus-owned
:class:`HumanInterventionRequest` created as the decision's additional effect,
and a generic block reason can be attached when #3 lands.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from ._clock import utcnow
from .decisions import DecisionKind, RouteConstraints, SemanticDecision
from .records import TransitionAuthority, TransitionRecord
from .work_order import OpenStatus, WorkOrder, WorkOrderId, WorkOrderState


class DecisionApplicationError(ValueError):
    """Base class for a decision that cannot be applied to a Work Order."""


class WorkOrderNotActiveError(DecisionApplicationError):
    """A recovery decision was applied to a Work Order that is not ``ACTIVE``."""


class MissingChildPlanError(DecisionApplicationError):
    """``DECOMPOSE`` was applied without a bounded child plan."""


class ChildCreationUnavailableError(DecisionApplicationError):
    """``DECOMPOSE`` was applied without a durable child-creation capability."""


class ChildCreationError(DecisionApplicationError):
    """Durable child creation did not produce the bounded child plan."""


# -- explicit mapping ------------------------------------------------------


class DecisionEffect(str, Enum):
    """The closed set of explicit Tactus effects for the frozen vocabulary."""

    REOPEN_READY_NEW_ATTEMPT = "REOPEN_READY_NEW_ATTEMPT"
    REOPEN_READY_ROUTED_ATTEMPT = "REOPEN_READY_ROUTED_ATTEMPT"
    REOPEN_READY_FOLLOW_UP_CAPABILITY = "REOPEN_READY_FOLLOW_UP_CAPABILITY"
    REOPEN_BLOCKED_INTERVENTION = "REOPEN_BLOCKED_INTERVENTION"
    RETIRE_ABORTED = "RETIRE_ABORTED"
    RETIRE_DECOMPOSED = "RETIRE_DECOMPOSED"


#: The single source of truth mapping every frozen decision to its effect.
#: Total and closed: a new ``DecisionKind`` cannot be added without a mapping.
DECISION_EFFECTS: dict[DecisionKind, DecisionEffect] = {
    DecisionKind.REEXECUTE: DecisionEffect.REOPEN_READY_NEW_ATTEMPT,
    DecisionKind.ROUTE: DecisionEffect.REOPEN_READY_ROUTED_ATTEMPT,
    DecisionKind.EXECUTE_CAPABILITY: DecisionEffect.REOPEN_READY_FOLLOW_UP_CAPABILITY,
    DecisionKind.ESCALATE: DecisionEffect.REOPEN_BLOCKED_INTERVENTION,
    DecisionKind.ABORT: DecisionEffect.RETIRE_ABORTED,
    DecisionKind.DECOMPOSE: DecisionEffect.RETIRE_DECOMPOSED,
}


# -- typed human-intervention effect ---------------------------------------


class InterventionReason(str, Enum):
    """The typed human-intervention reasons (Tactus human/domain coordination).

    Mirrors the vocabulary in ``docs/architecture/HUMAN_INTERVENTION.md``. This
    is a Tactus-owned coordination record, not an Ictus decision.
    """

    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
    SECRET_REQUIRED = "SECRET_REQUIRED"
    EXTERNAL_INPUT_REQUIRED = "EXTERNAL_INPUT_REQUIRED"
    MANUAL_SELECTION_REQUIRED = "MANUAL_SELECTION_REQUIRED"
    UNRESOLVED_RECOVERY = "UNRESOLVED_RECOVERY"
    SCOPE_CHANGE_REQUIRED = "SCOPE_CHANGE_REQUIRED"
    OTHER = "OTHER"


@dataclass(frozen=True, slots=True)
class HumanInterventionRequest:
    """A typed, durable human-intervention request created by an ``ESCALATE``."""

    intervention_id: str
    work_order_id: WorkOrderId
    reason: InterventionReason
    summary: str
    requested_action: str | None = None
    created_at: datetime = field(default_factory=utcnow)


# -- bounded child plan effect ---------------------------------------------


@dataclass(frozen=True, slots=True)
class ChildSpec:
    """One bounded child Work Order to create for a ``DECOMPOSE`` decision."""

    work_order_id: WorkOrderId
    title: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.work_order_id, WorkOrderId):
            object.__setattr__(self, "work_order_id", WorkOrderId(self.work_order_id))


@dataclass(frozen=True, slots=True)
class ChildPlan:
    """A bounded child plan supplied for a ``DECOMPOSE`` decision.

    ``max_children`` is the explicit bound. The plan is structural data, not a
    decomposition policy: Ictus decides *that* the subject should be decomposed;
    the bounded plan itself is produced by the bounded child-plan capability.
    """

    children: tuple[ChildSpec, ...]
    max_children: int

    def __post_init__(self) -> None:
        if isinstance(self.max_children, bool) or not isinstance(self.max_children, int):
            raise DecisionApplicationError("max_children must be an integer")
        if self.max_children < 1:
            raise DecisionApplicationError("max_children must be at least 1")
        children = tuple(self.children)
        if not children:
            raise DecisionApplicationError("a child plan must contain at least one child")
        if len(children) > self.max_children:
            raise DecisionApplicationError(
                f"child plan of {len(children)} exceeds bound {self.max_children}"
            )
        seen: set[WorkOrderId] = set()
        for child in children:
            if not isinstance(child, ChildSpec):
                raise DecisionApplicationError("child plan entries must be ChildSpec")
            if child.work_order_id in seen:
                raise DecisionApplicationError(
                    f"duplicate child id in plan: {child.work_order_id}"
                )
            seen.add(child.work_order_id)
        object.__setattr__(self, "children", children)


@runtime_checkable
class ChildCreationPort(Protocol):
    """Durable creation capability used by ``DECOMPOSE``.

    Tactus owns no persistence. The parent is retired only after this capability
    reports durable child creation.
    """

    def create_children(
        self, parent: WorkOrder, plan: ChildPlan
    ) -> tuple[WorkOrder, ...]:
        ...


class InMemoryChildCreation:
    """Reference durable-creation stand-in: creates ``DRAFT`` child Work Orders.

    Persistence is a separate adapter concern; this reference implementation
    records the created children in-process so the parent-retire-last invariant
    can be exercised deterministically.
    """

    def __init__(self) -> None:
        self._by_parent: dict[WorkOrderId, list[WorkOrder]] = {}
        self._seen: set[WorkOrderId] = set()

    def create_children(
        self, parent: WorkOrder, plan: ChildPlan
    ) -> tuple[WorkOrder, ...]:
        if parent.id in self._by_parent:
            raise ChildCreationError(
                f"children already created durably for {parent.id}"
            )
        created: list[WorkOrder] = []
        for spec in plan.children:
            if spec.work_order_id in self._seen:
                raise ChildCreationError(
                    f"child Work Order {spec.work_order_id} already exists"
                )
            self._seen.add(spec.work_order_id)
            created.append(WorkOrder.create(spec.work_order_id, spec.title))
        # "Durable" commit point: only after every child exists is the parent
        # allowed to retire.
        self._by_parent[parent.id] = created
        return tuple(created)

    def children_of(self, parent_id: WorkOrderId | str) -> tuple[WorkOrder, ...]:
        key = parent_id if isinstance(parent_id, WorkOrderId) else WorkOrderId(parent_id)
        return tuple(self._by_parent.get(key, ()))


# -- auditable application record ------------------------------------------


@dataclass(frozen=True, slots=True)
class DecisionApplicationRecord:
    """Immutable, auditable record of one applied decision.

    It preserves the decision identity, the explicit effect, the lifecycle /
    readiness transition and the additional effect payloads (intervention
    request, preserved route constraints, created children).
    """

    application_id: str
    decision_id: str
    decision_kind: DecisionKind
    effect: DecisionEffect
    work_order_id: WorkOrderId
    applied_at: datetime
    from_state: WorkOrderState
    to_state: WorkOrderState
    from_readiness: OpenStatus | None
    to_readiness: OpenStatus | None
    transitions: tuple[TransitionRecord, ...]
    capability: str | None = None
    route: RouteConstraints | None = None
    intervention: HumanInterventionRequest | None = None
    created_children: tuple[WorkOrderId, ...] = ()


# -- applier ---------------------------------------------------------------


class DecisionApplier:
    """Applies validated generic Ictus decisions to Work Orders.

    The applier is deliberately passive: it maps a *given* decision to its
    explicit effect. It performs no diagnosis and selects nothing.
    """

    def __init__(self, child_creation: ChildCreationPort | None = None) -> None:
        self._child_creation = child_creation
        self._applications: list[DecisionApplicationRecord] = []

    @property
    def applications(self) -> tuple[DecisionApplicationRecord, ...]:
        """The audit ledger of successfully applied decisions."""

        return tuple(self._applications)

    def apply(
        self,
        work_order: WorkOrder,
        decision: SemanticDecision,
        *,
        child_plan: ChildPlan | None = None,
        intervention_reason: InterventionReason = InterventionReason.UNRESOLVED_RECOVERY,
        at: datetime | None = None,
    ) -> DecisionApplicationRecord:
        """Apply ``decision`` to ``work_order`` and emit an audit record.

        Recovery decisions require an ``ACTIVE`` Work Order; the Work Order is
        validated before any mutation so a rejected application leaves it
        unchanged. Guard-checked lifecycle/readiness operations are the only
        mutators.
        """

        if not isinstance(work_order, WorkOrder):
            raise DecisionApplicationError("work_order must be a WorkOrder")
        if not isinstance(decision, SemanticDecision):
            raise DecisionApplicationError("decision must be a SemanticDecision")
        if work_order.state is not WorkOrderState.ACTIVE:
            raise WorkOrderNotActiveError(
                "recovery decisions apply to an ACTIVE Work Order "
                f"(state={work_order.state.value})"
            )

        effect = DECISION_EFFECTS[decision.kind]
        applied_at = at if at is not None else utcnow()
        before_state = work_order.state
        before_readiness = work_order.readiness
        intervention: HumanInterventionRequest | None = None
        created_children: tuple[WorkOrderId, ...] = ()

        if effect is DecisionEffect.REOPEN_READY_NEW_ATTEMPT:
            transition = work_order.reopen(
                OpenStatus.READY,
                reason=f"Ictus REEXECUTE: new semantic attempt ({decision.decision_id})",
                authority=TransitionAuthority.RECOVERY,
                at=applied_at,
            )
        elif effect is DecisionEffect.REOPEN_READY_ROUTED_ATTEMPT:
            transition = work_order.reopen(
                OpenStatus.READY,
                reason=f"Ictus ROUTE: routed attempt ({decision.decision_id})",
                authority=TransitionAuthority.RECOVERY,
                at=applied_at,
            )
        elif effect is DecisionEffect.REOPEN_READY_FOLLOW_UP_CAPABILITY:
            transition = work_order.reopen(
                OpenStatus.READY,
                reason=(
                    "Ictus EXECUTE_CAPABILITY: validated follow-up capability "
                    f"{decision.capability} ({decision.decision_id})"
                ),
                authority=TransitionAuthority.RECOVERY,
                at=applied_at,
            )
        elif effect is DecisionEffect.REOPEN_BLOCKED_INTERVENTION:
            intervention = HumanInterventionRequest(
                intervention_id=uuid.uuid4().hex,
                work_order_id=work_order.id,
                reason=intervention_reason,
                summary=f"Ictus ESCALATE ({decision.decision_id})",
                created_at=applied_at,
            )
            transition = work_order.reopen(
                OpenStatus.BLOCKED,
                reason=f"Ictus ESCALATE: human intervention ({decision.decision_id})",
                authority=TransitionAuthority.RECOVERY,
                at=applied_at,
            )
        elif effect is DecisionEffect.RETIRE_ABORTED:
            transition = work_order.retire(
                reason=f"Ictus ABORT ({decision.decision_id})",
                authority=TransitionAuthority.RECOVERY,
                at=applied_at,
            )
        elif effect is DecisionEffect.RETIRE_DECOMPOSED:
            created_children = self._create_children_durably(work_order, child_plan)
            transition = work_order.retire(
                reason=(
                    "Ictus DECOMPOSE: parent retired after durable child creation "
                    f"({decision.decision_id})"
                ),
                authority=TransitionAuthority.RECOVERY,
                at=applied_at,
            )
        else:  # pragma: no cover - closed enum, defensive fail-closed
            raise DecisionApplicationError(
                f"no Tactus effect for decision {decision.kind.value}"
            )

        record = DecisionApplicationRecord(
            application_id=uuid.uuid4().hex,
            decision_id=decision.decision_id,
            decision_kind=decision.kind,
            effect=effect,
            work_order_id=work_order.id,
            applied_at=applied_at,
            from_state=before_state,
            to_state=work_order.state,
            from_readiness=before_readiness,
            to_readiness=work_order.readiness,
            transitions=(transition,),
            capability=decision.capability,
            route=decision.route,
            intervention=intervention,
            created_children=created_children,
        )
        self._applications.append(record)
        return record

    def _create_children_durably(
        self, work_order: WorkOrder, child_plan: ChildPlan | None
    ) -> tuple[WorkOrderId, ...]:
        if child_plan is None:
            raise MissingChildPlanError(
                "DECOMPOSE requires a bounded child plan"
            )
        if not isinstance(child_plan, ChildPlan):
            raise MissingChildPlanError("child_plan must be a ChildPlan")
        if self._child_creation is None:
            raise ChildCreationUnavailableError(
                "DECOMPOSE requires a durable child-creation capability"
            )

        created = tuple(self._child_creation.create_children(work_order, child_plan))
        expected_ids = tuple(spec.work_order_id for spec in child_plan.children)
        created_ids = tuple(child.id for child in created)
        if created_ids != expected_ids:
            # Creation did not match the bounded plan; do NOT retire the parent.
            raise ChildCreationError(
                "durable child creation did not produce the bounded child plan"
            )
        return created_ids


__all__ = [
    "ChildCreationError",
    "ChildCreationPort",
    "ChildCreationUnavailableError",
    "ChildPlan",
    "ChildSpec",
    "DECISION_EFFECTS",
    "DecisionApplicationError",
    "DecisionApplicationRecord",
    "DecisionApplier",
    "DecisionEffect",
    "HumanInterventionRequest",
    "InMemoryChildCreation",
    "InterventionReason",
    "MissingChildPlanError",
    "WorkOrderNotActiveError",
]
