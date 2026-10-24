"""Bounded child-materialization validation (issue #10, criteria 1/3/4)."""

from __future__ import annotations

import pytest

from tactus.domain.decomposition import (
    ChildCountError,
    ChildDependencyError,
    ChildMaterializationPlan,
    ChildMaterializationSpec,
    ChildScopeViolationError,
    DuplicateChildError,
    ScopeExpansionError,
    deterministic_child_id,
    normalize_scope,
    plan_split,
    prerequisites_satisfied,
    scope_covers,
    unsatisfied_prerequisites,
)
from tactus.domain.dependency import (
    CyclicDependencyError,
    DependencyGraph,
    DuplicateDependencyError,
    MissingDependencyError,
    WorkOrderDependency,
)
from tactus.domain.work_order import WorkOrderId, WorkOrderState

W = WorkOrderId
PARENT_SCOPE = ("src/**", "tests/**", "docs/**")


def child(
    work_order_id: str,
    scope: tuple[str, ...] = ("src/part.py",),
    depends_on: tuple[str, ...] = (),
) -> ChildMaterializationSpec:
    return ChildMaterializationSpec(
        work_order_id=W(work_order_id),
        title=f"child {work_order_id}",
        scope=scope,
        depends_on=tuple(W(dependency) for dependency in depends_on),
    )


def plan(*children: ChildMaterializationSpec, max_children: int = 4) -> ChildMaterializationPlan:
    return ChildMaterializationPlan(
        parent_work_order_id=W("WO-PARENT"),
        parent_scope=PARENT_SCOPE,
        children=children,
        max_children=max_children,
    )


# -- criterion 1: everything fails before mutation ------------------------


def test_child_count_violation_is_rejected() -> None:
    with pytest.raises(ChildCountError):
        plan(child("C1"), child("C2"), child("C3"), max_children=2)


def test_empty_plan_is_rejected() -> None:
    with pytest.raises(ChildCountError):
        plan(max_children=4)


def test_duplicate_child_ids_are_rejected() -> None:
    with pytest.raises(DuplicateChildError):
        plan(child("C1"), child("C1"))


def test_child_may_not_reuse_the_parent_identity() -> None:
    with pytest.raises(DuplicateChildError):
        plan(child("WO-PARENT"))


def test_scope_expansion_is_rejected() -> None:
    with pytest.raises(ScopeExpansionError):
        plan(child("C1", scope=("src/part.py", "deploy/prod.yaml")))


def test_scope_expansion_is_rejected_even_with_a_similar_prefix() -> None:
    # ``src2`` is not under ``src``; a prefix check on raw strings would pass.
    with pytest.raises(ScopeExpansionError):
        plan(child("C1", scope=("src2/part.py",)))


def test_child_without_scope_is_rejected() -> None:
    with pytest.raises(ChildScopeViolationError):
        plan(child("C1", scope=()))


def test_intra_child_cycle_is_rejected() -> None:
    with pytest.raises(ChildDependencyError):
        plan(child("C1", depends_on=("C2",)), child("C2", depends_on=("C1",)))


def test_self_dependency_is_rejected() -> None:
    with pytest.raises(ChildDependencyError):
        plan(child("C1", depends_on=("C1",)))


def test_dependency_outside_the_plan_is_rejected() -> None:
    with pytest.raises(ChildDependencyError):
        plan(child("C1", depends_on=("C9",)))


def test_valid_plan_accepts_intra_child_dependencies() -> None:
    materialized = plan(child("C2", depends_on=("C1",)), child("C1"))
    assert materialized.child_ids == (W("C1"), W("C2"))


# -- scope arithmetic -----------------------------------------------------


def test_scope_covers_respects_path_segments() -> None:
    assert scope_covers(["src"], ["src/api/pure.py"])
    assert scope_covers(["src/**"], ["src/api/pure.py"])
    assert scope_covers(["src/api"], ["src/api"])
    assert not scope_covers(["src"], ["src2/x.py"])
    assert not scope_covers(["src/api"], ["src"])


def test_normalize_scope_strips_glob_and_separator_noise() -> None:
    assert normalize_scope(["src/**", "src/", "src"]) == ("src",)
    with pytest.raises(ChildScopeViolationError):
        normalize_scope(["**"])


# -- criterion 3: deterministic identity ----------------------------------


def test_repeated_plan_derives_identical_identity_and_key() -> None:
    first = plan(child("C2", depends_on=("C1",)), child("C1"))
    second = plan(child("C1"), child("C2", depends_on=("C1",)))
    assert first.child_ids == second.child_ids
    assert first.digest() == second.digest()
    assert first.materialization_key("DEC-1") == second.materialization_key("DEC-1")


def test_different_decisions_derive_different_keys() -> None:
    materialized = plan(child("C1"))
    assert materialized.materialization_key("DEC-1") != materialized.materialization_key("DEC-2")


def test_deterministic_child_id_is_stable_and_input_sensitive() -> None:
    assert deterministic_child_id("WO-PARENT", "DEC-1", 0) == deterministic_child_id(
        "WO-PARENT", "DEC-1", 0
    )
    assert deterministic_child_id("WO-PARENT", "DEC-1", 0) != deterministic_child_id(
        "WO-PARENT", "DEC-1", 1
    )
    assert deterministic_child_id("WO-PARENT", "DEC-1", 0) != deterministic_child_id(
        "WO-PARENT", "DEC-2", 0
    )


# -- dependency rewiring --------------------------------------------------


def base_graph() -> DependencyGraph:
    return DependencyGraph(
        [
            WorkOrderDependency(upstream=W("UP1"), downstream=W("WO-PARENT")),
            WorkOrderDependency(upstream=W("UP2"), downstream=W("WO-PARENT")),
            WorkOrderDependency(upstream=W("WO-PARENT"), downstream=W("DOWN1")),
        ]
    )


def test_split_work_order_preserves_upstream_and_downstream() -> None:
    graph = base_graph()
    record = graph.split_work_order(W("WO-PARENT"), (W("C1"), W("C2")))

    assert graph.upstream_of(W("C1")) == (W("UP1"), W("UP2"))
    assert graph.upstream_of(W("C2")) == (W("UP1"), W("UP2"))
    assert graph.downstream_of(W("C1")) == (W("DOWN1"),)
    assert graph.downstream_of(W("C2")) == (W("DOWN1"),)
    assert graph.downstream_of(W("WO-PARENT")) == ()
    assert len(record.removed) == 3
    assert len(record.added) == 6


def test_split_work_order_rejects_a_cycle_before_mutating() -> None:
    graph = DependencyGraph(
        [
            WorkOrderDependency(upstream=W("LEAF"), downstream=W("UP")),
            WorkOrderDependency(upstream=W("UP"), downstream=W("WO-PARENT")),
            WorkOrderDependency(upstream=W("WO-PARENT"), downstream=W("DOWN")),
        ]
    )
    before = graph.edges()
    # Replacing the parent with one of its own upstream nodes re-introduces
    # UP -> LEAF alongside the existing LEAF -> UP, which closes a cycle.
    with pytest.raises(CyclicDependencyError):
        graph.split_work_order(W("WO-PARENT"), (W("LEAF"),))
    assert graph.edges() == before


def test_split_work_order_rejects_identity_misuse() -> None:
    graph = base_graph()
    with pytest.raises(MissingDependencyError):
        graph.split_work_order(W("WO-PARENT"), ())
    with pytest.raises(DuplicateDependencyError):
        graph.split_work_order(W("WO-PARENT"), (W("C1"), W("C1")))
    with pytest.raises(DuplicateDependencyError):
        graph.split_work_order(W("WO-PARENT"), (W("WO-PARENT"),))


def test_plan_split_matches_the_rewrite_arithmetic() -> None:
    materialized = plan(child("C1"), child("C2"))
    removed, added = plan_split(base_graph(), materialized)
    assert {(e.upstream.value, e.downstream.value) for e in removed} == {
        ("UP1", "WO-PARENT"),
        ("UP2", "WO-PARENT"),
        ("WO-PARENT", "DOWN1"),
    }
    assert len(added) == 6


# -- criterion 4: retired prerequisites are never satisfied ---------------


def test_retired_prerequisite_is_not_satisfied() -> None:
    graph = DependencyGraph([WorkOrderDependency(upstream=W("DEP"), downstream=W("WO"))])
    retired = {W("WO"): WorkOrderState.OPEN, W("DEP"): WorkOrderState.RETIRED}
    assert prerequisites_satisfied(graph, retired) is False
    assert unsatisfied_prerequisites(graph, retired) == (W("DEP"),)


def test_missing_prerequisite_state_fails_closed() -> None:
    graph = DependencyGraph([WorkOrderDependency(upstream=W("DEP"), downstream=W("WO"))])
    assert prerequisites_satisfied(graph, {W("WO"): WorkOrderState.OPEN}) is False


def test_only_implemented_prerequisite_satisfies() -> None:
    graph = DependencyGraph([WorkOrderDependency(upstream=W("DEP"), downstream=W("WO"))])
    satisfied = {W("WO"): WorkOrderState.OPEN, W("DEP"): WorkOrderState.IMPLEMENTED}
    assert prerequisites_satisfied(graph, satisfied) is True
