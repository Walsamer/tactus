"""Atomic, idempotent materialization of validated ``DECOMPOSE`` children (#10).

Design boundary:

* The **domain** (``tactus.domain.decomposition``) validates the plan and owns
  the graph rewrite arithmetic. It fails before any mutation.
* The **store** (this module's port, plus the SQLite adapter in
  ``tactus.persistence.decomposition``) owns the single atomic commit: child
  Work Orders, their Issue provenance, the parent/child dependency rewrite and
  the parent effect all land together, or none of them do.
* The **service** (:class:`ChildMaterializer`) is the only caller and enforces
  idempotency: re-applying the same validated decision converges on the
  children it already materialized instead of creating a second generation.

Children are always created ``DRAFT`` — never ``OPEN`` — so a partially written
generation can never be executed, and each child must pass independent
admission/readiness on its own merits.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from ..domain._clock import utcnow
from ..domain.decomposition import ChildMaterializationPlan, plan_split
from ..domain.dependency import DependencyChangeRecord, DependencyGraph
from ..domain.records import TransitionAuthority
from ..domain.work_order import WorkOrder, WorkOrderId, WorkOrderState
from ..domain.work_source import WorkOrderSourceProvenance


class MaterializationError(RuntimeError):
    """Raised when a durable child materialization cannot be completed."""


class LedgerConflictError(MaterializationError):
    """An idempotency key was reused with different content."""


#: Failpoints a store must honour so failure injection can prove atomicity.
#: ``after_children``/``after_provenance``/``after_edges`` fire *inside* the
#: transaction, immediately before the parent effect.
FAILPOINTS = (
    "after_children",
    "after_provenance",
    "after_edges",
    "before_parent_effect",
)

FaultHook = Callable[[str], None]


def _no_fault(_name: str) -> None:
    return None


@dataclass(frozen=True, slots=True)
class MaterializedGeneration:
    """The durable result of one child materialization."""

    materialization_key: str
    parent_work_order_id: WorkOrderId
    child_ids: tuple[WorkOrderId, ...]
    dependency_change: DependencyChangeRecord
    parent_state: WorkOrderState
    applied_at: datetime

    def __post_init__(self) -> None:
        if not self.child_ids:
            raise MaterializationError("a materialized generation must contain children")


@dataclass(frozen=True, slots=True)
class MaterializationOutcome:
    """Service result: distinguishes a fresh apply from an idempotent replay."""

    generation: MaterializedGeneration
    replayed: bool


@runtime_checkable
class ChildMaterializationStore(Protocol):
    """Durable, transactional child-materialization capability."""

    def materialize(
        self,
        *,
        key: str,
        plan: ChildMaterializationPlan,
        parent: WorkOrder,
        parent_provenance: WorkOrderSourceProvenance | None,
        graph: DependencyGraph,
        authority: TransitionAuthority,
        reason: str,
        applied_at: datetime,
        fault: FaultHook = _no_fault,
    ) -> MaterializedGeneration:
        ...

    def lookup(self, key: str) -> MaterializedGeneration | None:
        ...


class ChildMaterializer:
    """Validated, idempotent entry point for ``DECOMPOSE`` materialization."""

    def __init__(self, store: ChildMaterializationStore) -> None:
        self._store = store

    def apply(
        self,
        *,
        decision_id: str,
        plan: ChildMaterializationPlan,
        parent: WorkOrder,
        graph: DependencyGraph,
        parent_provenance: WorkOrderSourceProvenance | None = None,
        authority: TransitionAuthority = TransitionAuthority.RECOVERY,
        reason: str = "",
        applied_at: datetime | None = None,
        fault: FaultHook = _no_fault,
    ) -> MaterializationOutcome:
        if parent.id != plan.parent_work_order_id:
            raise MaterializationError(
                f"child plan targets {plan.parent_work_order_id}, not {parent.id}"
            )

        key = plan.materialization_key(decision_id)

        # Replay is resolved *before* the lifecycle precondition: applying the
        # same decision again is idempotent even though the first application
        # already retired the parent.
        existing = self._store.lookup(key)
        if existing is not None:
            if existing.child_ids != plan.child_ids:
                raise LedgerConflictError(
                    f"idempotency key {key} already materialized different children"
                )
            return MaterializationOutcome(generation=existing, replayed=True)

        if parent.state is not WorkOrderState.ACTIVE:
            raise MaterializationError(
                "DECOMPOSE child materialization requires an ACTIVE parent "
                f"(state={parent.state.value})"
            )
        if parent_provenance is not None and parent_provenance.work_order_id != parent.id:
            raise MaterializationError(
                "parent provenance belongs to a different Work Order"
            )

        # Plan-level rewiring is derived from the live graph and validated by
        # ``replace_edges`` inside the store's transaction.
        plan_split(graph, plan)

        generation = self._store.materialize(
            key=key,
            plan=plan,
            parent=parent,
            parent_provenance=parent_provenance,
            graph=graph,
            authority=authority,
            reason=reason or f"Ictus DECOMPOSE ({decision_id})",
            applied_at=applied_at or utcnow(),
            fault=fault,
        )
        return MaterializationOutcome(generation=generation, replayed=False)


class InMemoryMaterializationStore:
    """Reference transactional store with real snapshot/rollback semantics.

    Every failpoint fires *inside* the transaction, after the corresponding
    write has been staged. A raised fault therefore proves that no partially
    written generation survives: the parent stays ``ACTIVE``, the graph is
    unchanged, and no child exists.
    """

    def __init__(self) -> None:
        self._ledger: dict[str, MaterializedGeneration] = {}
        self._children: dict[WorkOrderId, WorkOrder] = {}
        self._provenance: dict[WorkOrderId, WorkOrderSourceProvenance] = {}
        self._parents_retired: list[WorkOrderId] = []

    # -- read-only views for assertions -----------------------------------

    @property
    def children(self) -> Mapping[WorkOrderId, WorkOrder]:
        return dict(self._children)

    @property
    def provenance(self) -> Mapping[WorkOrderId, WorkOrderSourceProvenance]:
        return dict(self._provenance)

    @property
    def retired_parents(self) -> tuple[WorkOrderId, ...]:
        return tuple(self._parents_retired)

    def executable_children(self) -> tuple[WorkOrder, ...]:
        """Children that could actually run: anything not ``DRAFT``."""

        return tuple(
            child for child in self._children.values() if child.state is not WorkOrderState.DRAFT
        )

    # -- port --------------------------------------------------------------

    def lookup(self, key: str) -> MaterializedGeneration | None:
        return self._ledger.get(key)

    def materialize(
        self,
        *,
        key: str,
        plan: ChildMaterializationPlan,
        parent: WorkOrder,
        parent_provenance: WorkOrderSourceProvenance | None,
        graph: DependencyGraph,
        authority: TransitionAuthority,
        reason: str,
        applied_at: datetime,
        fault: FaultHook = _no_fault,
    ) -> MaterializedGeneration:
        children_before = dict(self._children)
        provenance_before = dict(self._provenance)
        parents_before = list(self._parents_retired)
        graph_before = graph.edges()

        try:
            for spec in plan.children:
                existing = self._children.get(spec.work_order_id)
                if existing is not None:
                    raise MaterializationError(
                        f"child Work Order {spec.work_order_id} already exists"
                    )
                # DRAFT is non-executable by construction: a child cannot run
                # until it is independently admitted and made ready.
                self._children[spec.work_order_id] = WorkOrder.create(
                    spec.work_order_id, spec.title
                )
            fault("after_children")

            if parent_provenance is not None:
                for spec in plan.children:
                    self._provenance[spec.work_order_id] = parent_provenance.as_child(
                        spec.work_order_id
                    )
            fault("after_provenance")

            removed, added = plan_split(graph, plan)
            graph.replace_edges(remove=removed, add=added)
            fault("after_edges")

            fault("before_parent_effect")
            # The parent effect is deliberately the last mutation and cannot
            # partially fail: ``retire`` validates the state it already has, and
            # the ledger write below is an in-memory assignment. Every
            # failpoint therefore fires while the parent is still ACTIVE, which
            # keeps rollback possible (RETIRED is terminal and irreversible).
            parent.retire(reason=reason, authority=authority, at=applied_at)
            self._parents_retired.append(parent.id)

            generation = MaterializedGeneration(
                materialization_key=key,
                parent_work_order_id=plan.parent_work_order_id,
                child_ids=plan.child_ids,
                dependency_change=DependencyChangeRecord(
                    removed=removed,
                    added=added,
                    authority=authority,
                    reason=reason,
                    occurred_at=applied_at,
                ),
                parent_state=parent.state,
                applied_at=applied_at,
            )
            self._ledger[key] = generation
            return generation
        except BaseException:
            # Roll back every staged effect so no partially materialized
            # generation can be observed, let alone executed.
            self._children = children_before
            self._provenance = provenance_before
            self._parents_retired = parents_before
            graph.replace_edges(
                remove=tuple(edge for edge in graph.edges() if edge not in graph_before),
                add=tuple(edge for edge in graph_before if edge not in graph.edges()),
            )
            if parent.state is WorkOrderState.RETIRED:
                raise MaterializationError(
                    "child materialization rolled back after the parent effect; "
                    "the parent is RETIRED and cannot be restored"
                )
            raise



@dataclass(frozen=True, slots=True)
class MaterializationEvidence:
    """Compact audit projection of one materialization."""

    key: str
    parent_work_order_id: WorkOrderId
    child_ids: tuple[WorkOrderId, ...]
    removed_edges: tuple[tuple[str, str], ...]
    added_edges: tuple[tuple[str, str], ...]

    @classmethod
    def from_generation(cls, generation: MaterializedGeneration) -> "MaterializationEvidence":
        return cls(
            key=generation.materialization_key,
            parent_work_order_id=generation.parent_work_order_id,
            child_ids=generation.child_ids,
            removed_edges=tuple(
                (edge.upstream.value, edge.downstream.value)
                for edge in generation.dependency_change.removed
            ),
            added_edges=tuple(
                (edge.upstream.value, edge.downstream.value)
                for edge in generation.dependency_change.added
            ),
        )


def executable_children(children: Iterable[WorkOrder]) -> tuple[WorkOrder, ...]:
    """Assertion helper: children that are not ``DRAFT`` would be executable."""

    return tuple(child for child in children if child.state is not WorkOrderState.DRAFT)


__all__ = [
    "ChildMaterializationStore",
    "ChildMaterializer",
    "FAILPOINTS",
    "FaultHook",
    "InMemoryMaterializationStore",
    "LedgerConflictError",
    "MaterializationError",
    "MaterializationEvidence",
    "MaterializationOutcome",
    "MaterializedGeneration",
    "executable_children",
]
