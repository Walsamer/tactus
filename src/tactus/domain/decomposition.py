"""Bounded child materialization for ``DECOMPOSE`` (issue #10).

Ictus decides *that* a subject must be decomposed; Tactus owns the Work Order
graph, so Tactus owns the explicit, validated materialization of the resulting
children. This module is the domain half of that boundary:

* it validates a bounded child plan **before any mutation** (scope widening,
  child-count, duplicate identity, unknown/cyclic intra-child dependencies);
* it derives deterministic child identity so applying the same validated
  decision twice converges instead of duplicating;
* it exposes dependency-satisfaction semantics in which a ``RETIRED``
  prerequisite is explicitly **not** satisfied.

It contains no persistence, no scheduling, no recovery policy and no Ictus
decision logic. The durable, atomic commit lives in the application/persistence
adapters that consume these value objects.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .dependency import DependencyGraph, WorkOrderDependency
from .work_order import WorkOrderId, WorkOrderState

# -- errors ---------------------------------------------------------------


class DecompositionError(ValueError):
    """Base class for bounded child-materialization failures."""


class ScopeExpansionError(DecompositionError):
    """A child asserted scope outside the parent's authorized scope."""


class DuplicateChildError(DecompositionError):
    """A child plan repeats an identity (or reuses the parent's)."""


class ChildCountError(DecompositionError):
    """A child plan violates its explicit child bound."""


class ChildDependencyError(DecompositionError):
    """A child plan's intra-child dependency edges are invalid."""


class ChildScopeViolationError(DecompositionError):
    """A child carries no usable scope entry."""


# -- scope ----------------------------------------------------------------


def _normalize_scope_entry(entry: str) -> str:
    """Normalize one scope entry to a bare path prefix.

    ``src/**``, ``src/`` and ``src`` all normalize to ``src``. Glob suffixes are
    stripped because the domain compares *authorization prefixes*, not globs;
    turning ``src/**`` into a deeper grant than ``src`` would be a silent
    widening.
    """

    if not isinstance(entry, str) or not entry.strip():
        raise ChildScopeViolationError("scope entries must be non-empty strings")
    normalized = entry.strip()
    while normalized.endswith("/**"):
        normalized = normalized[: -len("/**")]
    normalized = normalized.rstrip("/")
    if not normalized or set(normalized) == {"*"}:
        # A bare ``**``/``*``/``/**`` would authorize the whole tree; refuse
        # rather than guess at an intended bound.
        raise ChildScopeViolationError(f"unbounded scope entry: {entry!r}")
    return normalized


def normalize_scope(entries: Iterable[str]) -> tuple[str, ...]:
    """Return a sorted, de-duplicated, normalized scope."""

    return tuple(sorted({_normalize_scope_entry(entry) for entry in entries}))


def scope_covers(parent_scope: Iterable[str], child_scope: Iterable[str]) -> bool:
    """Whether ``parent_scope`` authorizes every entry in ``child_scope``.

    Coverage is prefix containment on normalized path segments, so ``src``
    covers ``src/api/pure.py`` but never covers ``src2/x.py``.
    """

    parent = normalize_scope(parent_scope)
    for child_entry in normalize_scope(child_scope):
        if not any(
            child_entry == granted or child_entry.startswith(granted + "/")
            for granted in parent
        ):
            return False
    return True


def assert_no_scope_expansion(
    parent_scope: Iterable[str], child: "ChildMaterializationSpec"
) -> None:
    """Fail if a child widens the parent's authorized scope."""

    if not scope_covers(parent_scope, child.scope):
        raise ScopeExpansionError(
            f"child {child.work_order_id} widens scope beyond the parent grant: "
            f"{list(normalize_scope(child.scope))} not within "
            f"{list(normalize_scope(parent_scope))}"
        )


# -- plan value objects ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChildMaterializationSpec:
    """One bounded child Work Order to materialize.

    ``depends_on`` names *sibling* children only. Cross-graph rewiring (former
    upstream/downstream of the parent) is derived from the dependency graph, not
    asserted by the plan, so a plan cannot smuggle in arbitrary edges.
    """

    work_order_id: WorkOrderId
    title: str
    scope: tuple[str, ...] = ()
    depends_on: tuple[WorkOrderId, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.work_order_id, WorkOrderId):
            object.__setattr__(self, "work_order_id", WorkOrderId(self.work_order_id))
        if not isinstance(self.title, str) or not self.title.strip():
            raise DecompositionError("child title must be a non-empty string")
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "scope", normalize_scope(self.scope))
        depends_on = tuple(
            dependency
            if isinstance(dependency, WorkOrderId)
            else WorkOrderId(dependency)
            for dependency in self.depends_on
        )
        if len(set(depends_on)) != len(depends_on):
            raise ChildDependencyError(
                f"child {self.work_order_id} repeats an intra-child dependency"
            )
        if self.work_order_id in depends_on:
            raise ChildDependencyError(
                f"child {self.work_order_id} cannot depend on itself"
            )
        # Deterministic ordering keeps the plan's digest stable.
        object.__setattr__(
            self, "depends_on", tuple(sorted(depends_on, key=lambda item: item.value))
        )


@dataclass(frozen=True, slots=True)
class ChildMaterializationPlan:
    """A validated bounded plan: every failure mode fires before mutation."""

    parent_work_order_id: WorkOrderId
    parent_scope: tuple[str, ...]
    children: tuple[ChildMaterializationSpec, ...]
    max_children: int

    def __post_init__(self) -> None:
        parent = self.parent_work_order_id
        if not isinstance(parent, WorkOrderId):
            parent = WorkOrderId(parent)
            object.__setattr__(self, "parent_work_order_id", parent)
        object.__setattr__(self, "parent_scope", normalize_scope(self.parent_scope))

        if isinstance(self.max_children, bool) or not isinstance(self.max_children, int):
            raise ChildCountError("max_children must be an integer")
        if self.max_children < 1:
            raise ChildCountError("max_children must be at least 1")

        children = tuple(self.children)
        if not children:
            raise ChildCountError("a child plan must contain at least one child")
        if len(children) > self.max_children:
            raise ChildCountError(
                f"child plan of {len(children)} exceeds bound {self.max_children}"
            )

        seen: set[WorkOrderId] = set()
        for child in children:
            if not isinstance(child, ChildMaterializationSpec):
                raise DecompositionError(
                    "child plan entries must be ChildMaterializationSpec"
                )
            if child.work_order_id == parent:
                raise DuplicateChildError(
                    f"child plan reuses the parent identity: {parent}"
                )
            if child.work_order_id in seen:
                raise DuplicateChildError(f"duplicate child id in plan: {child.work_order_id}")
            seen.add(child.work_order_id)
            if not child.scope:
                raise ChildScopeViolationError(
                    f"child {child.work_order_id} declares no scope"
                )
            assert_no_scope_expansion(self.parent_scope, child)

        # Intra-child edges must resolve to siblings inside this plan.
        for child in children:
            for dependency in child.depends_on:
                if dependency not in seen:
                    raise ChildDependencyError(
                        f"child {child.work_order_id} depends on "
                        f"{dependency}, which is not in the plan"
                    )

        # A cycle among siblings is a whole-plan property: validate the exact
        # topology that is about to be written, never an intermediate state.
        try:
            DependencyGraph(
                WorkOrderDependency(upstream=dependency, downstream=child.work_order_id)
                for child in children
                for dependency in child.depends_on
            )
        except ValueError as error:  # CyclicDependencyError / DuplicateDependencyError
            raise ChildDependencyError(
                f"intra-child dependency graph is invalid: {error}"
            ) from error

        object.__setattr__(
            self,
            "children",
            tuple(sorted(children, key=lambda item: item.work_order_id.value)),
        )

    # -- derived identity -------------------------------------------------

    @property
    def child_ids(self) -> tuple[WorkOrderId, ...]:
        return tuple(child.work_order_id for child in self.children)

    def digest(self) -> str:
        """Stable content digest of the plan (identity, scope, intra-edges)."""

        payload = {
            "parent": self.parent_work_order_id.value,
            "max_children": self.max_children,
            "children": [
                {
                    "id": child.work_order_id.value,
                    "title": child.title,
                    "scope": list(child.scope),
                    "depends_on": [dependency.value for dependency in child.depends_on],
                }
                for child in self.children
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def materialization_key(self, decision_id: str) -> str:
        """Deterministic idempotency key for one applied decision.

        Two applications of the same validated decision therefore derive the
        same key, and a replay converges on the already-materialized children
        instead of creating a second generation.
        """

        if not isinstance(decision_id, str) or not decision_id.strip():
            raise DecompositionError("decision_id must be a non-empty string")
        return f"{self.parent_work_order_id.value}:{decision_id.strip()}:{self.digest()}"


def deterministic_child_id(
    parent_work_order_id: WorkOrderId | str, decision_id: str, index: int
) -> WorkOrderId:
    """Derive a stable child identity when the plan does not name one.

    Purely a helper for plan *construction*; a plan that already carries
    explicit child ids keeps them verbatim, so identity never depends on call
    order or wall-clock time.
    """

    parent = (
        parent_work_order_id
        if isinstance(parent_work_order_id, WorkOrderId)
        else WorkOrderId(parent_work_order_id)
    )
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise DecompositionError("index must be a non-negative integer")
    seed = f"{parent.value}\x00{decision_id.strip() if isinstance(decision_id, str) else decision_id}\x00{index}"
    suffix = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    return WorkOrderId(f"{parent.value}-child-{suffix}")


# -- dependency rewiring --------------------------------------------------


def plan_split(
    graph: DependencyGraph, plan: ChildMaterializationPlan
) -> tuple[tuple[WorkOrderDependency, ...], tuple[WorkOrderDependency, ...]]:
    """Return the exact ``(removed, added)`` edge delta for a parent split.

    Former upstream nodes now feed **every** child; every child now feeds each
    former downstream node. Derived from the live graph, so the rewrite can
    never invent an edge the graph did not already authorize.
    """

    parent = plan.parent_work_order_id
    upstream = graph.upstream_of(parent)
    downstream = graph.downstream_of(parent)

    removed = tuple(
        [WorkOrderDependency(upstream=node, downstream=parent) for node in upstream]
        + [WorkOrderDependency(upstream=parent, downstream=node) for node in downstream]
    )
    added = tuple(
        [WorkOrderDependency(upstream=node, downstream=child) for node in upstream for child in plan.child_ids]
        + [WorkOrderDependency(upstream=child, downstream=node) for child in plan.child_ids for node in downstream]
    )
    return removed, added


# -- readiness ------------------------------------------------------------


def unsatisfied_prerequisites(
    graph: DependencyGraph,
    states: Mapping[WorkOrderId, WorkOrderState],
) -> tuple[WorkOrderId, ...]:
    """Upstream Work Orders that do **not** satisfy a prerequisite.

    Only ``IMPLEMENTED`` counts. ``RETIRED`` is explicitly *not* satisfiable: a
    retired prerequisite may have been abandoned, superseded or decomposed, and
    silently treating it as complete would let dependent work run against
    output that was never produced. Missing/unknown states also fail closed.
    """

    unsatisfied: list[WorkOrderId] = []
    for node, upstream in states.items():
        for dependency in graph.upstream_of(node):
            if states.get(dependency) is not WorkOrderState.IMPLEMENTED:
                unsatisfied.append(dependency)
    return tuple(sorted(set(unsatisfied), key=lambda item: item.value))


def prerequisites_satisfied(
    graph: DependencyGraph, states: Mapping[WorkOrderId, WorkOrderState]
) -> bool:
    """Whether every graph prerequisite is ``IMPLEMENTED`` (never merely retired)."""

    return not unsatisfied_prerequisites(graph, states)


def prerequisite_satisfied(state: WorkOrderState | None) -> bool:
    """Prerequisite semantics for a single upstream node."""

    return state is WorkOrderState.IMPLEMENTED


__all__ = [
    "ChildCountError",
    "ChildDependencyError",
    "ChildMaterializationPlan",
    "ChildMaterializationSpec",
    "ChildScopeViolationError",
    "DecompositionError",
    "DuplicateChildError",
    "ScopeExpansionError",
    "assert_no_scope_expansion",
    "deterministic_child_id",
    "normalize_scope",
    "plan_split",
    "prerequisite_satisfied",
    "prerequisites_satisfied",
    "scope_covers",
    "unsatisfied_prerequisites",
]
