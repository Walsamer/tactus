"""First-class Work Order dependency relations.

The dependency graph is a separate domain subsystem from the Work Order
lifecycle entity. It stores **edges only**; the upstream and downstream views
are derived, so the two directions can never drift apart.

Edge direction::

    upstream -> downstream

means the downstream Work Order depends on the upstream Work Order. For
``WO-101 -> WO-102 -> WO-103``:

* ``upstream_of(WO-102)`` is ``(WO-101,)``
* ``downstream_of(WO-102)`` is ``(WO-103,)``

v1 exposes a single semantic relation (implicitly ``REQUIRES``). A richer
dependency-kind taxonomy (``PRODUCES_INPUT_FOR``, ``SUPERSEDES``, ...) is
deliberately not introduced until concrete behavior needs it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from ._clock import utcnow
from .records import TransitionAuthority
from .work_order import WorkOrderId


class DependencyError(ValueError):
    """Base class for invalid dependency-relation operations."""


class SelfDependencyError(DependencyError):
    """A Work Order cannot depend on itself."""


class DuplicateDependencyError(DependencyError):
    """The same directed edge was added twice."""


class MissingDependencyError(DependencyError):
    """An operation referenced an edge that is not present."""


class CyclicDependencyError(DependencyError):
    """The candidate graph would contain a dependency cycle.

    Cycles are rejected rather than deferred: an acyclic dependency graph is a
    v1 invariant, so a cyclic candidate fails closed.
    """


@dataclass(frozen=True, slots=True)
class WorkOrderDependency:
    """A directed dependency edge between two Work Orders."""

    upstream: WorkOrderId
    downstream: WorkOrderId

    def __post_init__(self) -> None:
        if self.upstream == self.downstream:
            raise SelfDependencyError(
                f"self-dependency is not allowed: {self.upstream}"
            )


@dataclass(frozen=True, slots=True)
class DependencyChangeRecord:
    """Reserved provenance record for a future audited graph rewrite.

    Not emitted in v1. It is defined now so the atomic rewrite API can later
    gain audit/history provenance (authority, reason, timestamp, change id)
    without reshaping the graph interface.
    """

    removed: tuple[WorkOrderDependency, ...]
    added: tuple[WorkOrderDependency, ...]
    authority: TransitionAuthority
    reason: str
    occurred_at: datetime
    change_id: str = field(default_factory=lambda: uuid.uuid4().hex)


def _sort_key(work_order_id: WorkOrderId) -> str:
    return work_order_id.value


def _edge_sort_key(dependency: "WorkOrderDependency") -> tuple[str, str]:
    return (dependency.upstream.value, dependency.downstream.value)


class DependencyGraph:
    """Canonical adjacency over Work Order dependency edges.

    Edges are the only stored topology. ``_upstream`` and ``_downstream`` are
    derived indexes rebuilt on commit; they are never independently mutable.
    """

    def __init__(self, edges: Iterable[WorkOrderDependency] = ()) -> None:
        materialized = list(edges)
        if len(materialized) != len(set(materialized)):
            raise DuplicateDependencyError("duplicate dependency edge in initial graph")
        candidate = set(materialized)
        self._validate(candidate)
        self._commit(candidate)

    # -- read-only views --------------------------------------------------

    def __len__(self) -> int:
        return len(self._edges)

    def __contains__(self, dependency: object) -> bool:
        return dependency in self._edges

    def edges(self) -> tuple[WorkOrderDependency, ...]:
        """All edges in deterministic (upstream, downstream) order."""

        return tuple(
            sorted(
                self._edges,
                key=lambda d: (_sort_key(d.upstream), _sort_key(d.downstream)),
            )
        )

    def upstream_of(self, work_order: WorkOrderId | str) -> tuple[WorkOrderId, ...]:
        """Direct upstream dependencies of ``work_order`` (deterministic)."""

        node = _as_id(work_order)
        return tuple(sorted(self._upstream.get(node, ()), key=_sort_key))

    def downstream_of(self, work_order: WorkOrderId | str) -> tuple[WorkOrderId, ...]:
        """Direct downstream dependents of ``work_order`` (deterministic)."""

        node = _as_id(work_order)
        return tuple(sorted(self._downstream.get(node, ()), key=_sort_key))

    # -- atomic primitive rewrites ----------------------------------------

    def add(self, dependency: WorkOrderDependency) -> None:
        """Add one edge, validating the resulting graph before committing."""

        if dependency in self._edges:
            raise DuplicateDependencyError(f"duplicate dependency edge: {dependency}")
        candidate = set(self._edges)
        candidate.add(dependency)
        self._validate(candidate)
        self._commit(candidate)

    def remove(self, dependency: WorkOrderDependency) -> None:
        """Remove one existing edge."""

        if dependency not in self._edges:
            raise MissingDependencyError(f"dependency edge not present: {dependency}")
        candidate = set(self._edges)
        candidate.discard(dependency)
        self._commit(candidate)

    def replace_edges(
        self,
        *,
        remove: Iterable[WorkOrderDependency] = (),
        add: Iterable[WorkOrderDependency] = (),
    ) -> None:
        """Atomically replace a set of edges.

        Removals and additions are applied to a candidate graph as one
        transaction. The candidate is validated as a whole (so cycle detection
        runs against the final topology, never an intermediate state); on any
        failure the original graph is left completely unchanged.
        """

        candidate = set(self._edges)

        for dependency in remove:
            if dependency not in candidate:
                raise MissingDependencyError(
                    f"cannot remove absent dependency edge: {dependency}"
                )
            candidate.discard(dependency)

        for dependency in add:
            if dependency in candidate:
                raise DuplicateDependencyError(
                    f"cannot add duplicate dependency edge: {dependency}"
                )
            candidate.add(dependency)

        self._validate(candidate)
        self._commit(candidate)

    # -- high-level rewrites (future extension points) --------------------

    def split_work_order(
        self,
        original: WorkOrderId,
        replacements: tuple[WorkOrderId, ...],
        *,
        authority: TransitionAuthority = TransitionAuthority.RECOVERY,
        reason: str = "",
        occurred_at: datetime | None = None,
    ) -> DependencyChangeRecord:
        """Replace one node with several replacements, atomically.

        Used by bounded ``DECOMPOSE`` materialization (issue #10). The rewrite
        preserves the original node's topology: every former upstream node now
        feeds **each** replacement, and every replacement feeds each former
        downstream node. Nothing is invented outside the edges the graph
        already authorised.

        All validation runs against the complete candidate topology before the
        single commit, so a rejected split leaves the graph untouched. Identities
        are never mutated: the split node's edges are removed and new nodes are
        added, rather than rewriting the node in place.
        """

        original_id = (
            original if isinstance(original, WorkOrderId) else WorkOrderId(original)
        )
        normalised: list[WorkOrderId] = []
        for replacement in replacements:
            replacement_id = (
                replacement
                if isinstance(replacement, WorkOrderId)
                else WorkOrderId(replacement)
            )
            if replacement_id in normalised:
                raise DuplicateDependencyError(
                    f"duplicate split replacement: {replacement_id}"
                )
            normalised.append(replacement_id)

        if not normalised:
            raise MissingDependencyError(
                f"split of {original_id} requires at least one replacement"
            )
        if original_id in normalised:
            raise DuplicateDependencyError(
                f"a split replacement may not reuse the split identity: {original_id}"
            )

        replacements_sorted = tuple(sorted(normalised, key=_sort_key))
        upstream = self.upstream_of(original_id)
        downstream = self.downstream_of(original_id)

        removed: list[WorkOrderDependency] = []
        for node in upstream:
            edge = WorkOrderDependency(upstream=node, downstream=original_id)
            if edge in self._edges:
                removed.append(edge)
        for node in downstream:
            edge = WorkOrderDependency(upstream=original_id, downstream=node)
            if edge in self._edges:
                removed.append(edge)

        added: list[WorkOrderDependency] = []
        for node in upstream:
            for replacement in replacements_sorted:
                edge = WorkOrderDependency(upstream=node, downstream=replacement)
                if edge not in self._edges:
                    added.append(edge)
        for replacement in replacements_sorted:
            for node in downstream:
                edge = WorkOrderDependency(upstream=replacement, downstream=node)
                if edge not in self._edges:
                    added.append(edge)

        if not removed and not added:
            raise MissingDependencyError(
                f"cannot split {original_id}: it has no dependency relations to rewrite"
            )

        self.replace_edges(remove=removed, add=added)

        return DependencyChangeRecord(
            removed=tuple(sorted(removed, key=_edge_sort_key)),
            added=tuple(sorted(added, key=_edge_sort_key)),
            authority=authority,
            reason=reason or f"split {original_id} into {len(replacements_sorted)} children",
            occurred_at=occurred_at or utcnow(),
        )

    def merge_work_orders(
        self,
        originals: tuple[WorkOrderId, ...],
        replacement: WorkOrderId,
    ) -> None:
        """TODO: merge multiple nodes into one replacement node.

        This must reconstruct upstream/downstream relations without drift,
        duplicates, self-dependencies or cycles, and eventually emit a
        ``DependencyChangeRecord``. Exact merge semantics remain intentionally
        undefined in v1.
        """

        raise NotImplementedError

    def replace_node(
        self,
        original: WorkOrderId,
        replacement: WorkOrderId,
    ) -> None:
        """TODO: replace one node while preserving its valid topology.

        This should eventually be implemented in terms of ``replace_edges()``
        so the rewrite stays atomic and validated as a whole.
        """

        raise NotImplementedError

    # -- internals --------------------------------------------------------

    def _validate(self, edges: set[WorkOrderDependency]) -> None:
        for dependency in edges:
            if dependency.upstream == dependency.downstream:
                raise SelfDependencyError(
                    f"self-dependency is not allowed: {dependency.upstream}"
                )
        self._check_acyclic(edges)

    @staticmethod
    def _check_acyclic(edges: set[WorkOrderDependency]) -> None:
        adjacency: dict[WorkOrderId, list[WorkOrderId]] = {}
        indegree: dict[WorkOrderId, int] = {}
        nodes: set[WorkOrderId] = set()

        for dependency in edges:
            nodes.add(dependency.upstream)
            nodes.add(dependency.downstream)
            adjacency.setdefault(dependency.upstream, []).append(dependency.downstream)
            indegree[dependency.downstream] = indegree.get(dependency.downstream, 0) + 1
            indegree.setdefault(dependency.upstream, indegree.get(dependency.upstream, 0))

        ready = [node for node in nodes if indegree.get(node, 0) == 0]
        visited = 0
        while ready:
            node = ready.pop()
            visited += 1
            for dependent in adjacency.get(node, ()):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)

        if visited != len(nodes):
            raise CyclicDependencyError("dependency graph would contain a cycle")

    def _commit(self, edges: set[WorkOrderDependency]) -> None:
        upstream: dict[WorkOrderId, set[WorkOrderId]] = {}
        downstream: dict[WorkOrderId, set[WorkOrderId]] = {}
        for dependency in edges:
            downstream.setdefault(dependency.upstream, set()).add(dependency.downstream)
            upstream.setdefault(dependency.downstream, set()).add(dependency.upstream)

        self._edges = set(edges)
        self._upstream = upstream
        self._downstream = downstream


def _as_id(work_order: WorkOrderId | str) -> WorkOrderId:
    return work_order if isinstance(work_order, WorkOrderId) else WorkOrderId(work_order)
