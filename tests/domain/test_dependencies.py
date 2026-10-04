from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tactus.domain import (
    CyclicDependencyError,
    DependencyChangeRecord,
    DependencyGraph,
    DuplicateDependencyError,
    MissingDependencyError,
    SelfDependencyError,
    TransitionAuthority,
    WorkOrderDependency,
    WorkOrderId,
)

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def dep(upstream: str, downstream: str) -> WorkOrderDependency:
    return WorkOrderDependency(WorkOrderId(upstream), WorkOrderId(downstream))


# -- edge invariants ------------------------------------------------------


def test_self_dependency_is_rejected() -> None:
    with pytest.raises(SelfDependencyError):
        dep("WO-1", "WO-1")


def test_duplicate_edge_is_rejected() -> None:
    with pytest.raises(DuplicateDependencyError):
        DependencyGraph([dep("WO-1", "WO-2"), dep("WO-1", "WO-2")])


def test_adding_duplicate_edge_is_rejected() -> None:
    graph = DependencyGraph([dep("WO-1", "WO-2")])

    with pytest.raises(DuplicateDependencyError):
        graph.add(dep("WO-1", "WO-2"))


def test_cycle_on_add_is_rejected_and_graph_unchanged() -> None:
    graph = DependencyGraph([dep("WO-1", "WO-2")])

    with pytest.raises(CyclicDependencyError):
        graph.add(dep("WO-2", "WO-1"))

    assert graph.edges() == (dep("WO-1", "WO-2"),)


def test_longer_cycle_is_rejected() -> None:
    graph = DependencyGraph([dep("A", "B"), dep("B", "C")])

    with pytest.raises(CyclicDependencyError):
        graph.add(dep("C", "A"))


# -- derived views --------------------------------------------------------


def test_views_are_derived_and_deterministic() -> None:
    graph = DependencyGraph(
        [dep("WO-101", "WO-102"), dep("WO-100", "WO-102"), dep("WO-102", "WO-103")]
    )

    assert graph.upstream_of(WorkOrderId("WO-102")) == (
        WorkOrderId("WO-100"),
        WorkOrderId("WO-101"),
    )
    assert graph.downstream_of(WorkOrderId("WO-102")) == (WorkOrderId("WO-103"),)
    assert graph.upstream_of("WO-103") == (WorkOrderId("WO-102"),)
    assert graph.downstream_of("WO-100") == (WorkOrderId("WO-102"),)
    assert graph.upstream_of("UNKNOWN") == ()
    assert graph.downstream_of("UNKNOWN") == ()


def test_edges_are_deterministically_ordered() -> None:
    graph = DependencyGraph([dep("B", "C"), dep("A", "C"), dep("A", "B")])

    assert graph.edges() == (dep("A", "B"), dep("A", "C"), dep("B", "C"))


def test_remove_edge() -> None:
    graph = DependencyGraph([dep("A", "B"), dep("B", "C")])

    graph.remove(dep("A", "B"))

    assert graph.edges() == (dep("B", "C"),)
    assert graph.upstream_of("B") == ()


def test_remove_absent_edge_is_rejected() -> None:
    graph = DependencyGraph()

    with pytest.raises(MissingDependencyError):
        graph.remove(dep("A", "B"))


# -- atomic replace_edges -------------------------------------------------


def test_replace_edges_can_reverse_an_edge_atomically() -> None:
    graph = DependencyGraph([dep("A", "B")])

    graph.replace_edges(remove=[dep("A", "B")], add=[dep("B", "A")])

    assert graph.edges() == (dep("B", "A"),)
    assert graph.upstream_of("A") == (WorkOrderId("B"),)
    assert graph.downstream_of("B") == (WorkOrderId("A"),)


def test_replace_edges_validates_final_graph_not_intermediate_state() -> None:
    # Applied edge-by-edge this would transiently create a cycle; as one
    # transaction the final graph {A->B, C->D} is acyclic and must succeed.
    graph = DependencyGraph([dep("A", "B")])

    graph.replace_edges(add=[dep("C", "D")])

    assert graph.edges() == (dep("A", "B"), dep("C", "D"))


def test_replace_edges_cycle_failure_leaves_graph_unchanged() -> None:
    graph = DependencyGraph([dep("A", "B")])
    before = graph.edges()

    with pytest.raises(CyclicDependencyError):
        graph.replace_edges(remove=[dep("A", "B")], add=[dep("B", "A"), dep("A", "B")])

    assert graph.edges() == before


def test_replace_edges_duplicate_add_leaves_graph_unchanged() -> None:
    graph = DependencyGraph([dep("A", "B")])
    before = graph.edges()

    with pytest.raises(DuplicateDependencyError):
        graph.replace_edges(add=[dep("A", "B")])

    assert graph.edges() == before


def test_replace_edges_absent_remove_leaves_graph_unchanged() -> None:
    graph = DependencyGraph([dep("A", "B")])
    before = graph.edges()

    with pytest.raises(MissingDependencyError):
        graph.replace_edges(remove=[dep("X", "Y")])

    assert graph.edges() == before


# -- future extension points ----------------------------------------------


def test_high_level_rewrites_are_explicit_todos() -> None:
    graph = DependencyGraph([dep("A", "B")])

    with pytest.raises(NotImplementedError):
        graph.split_work_order(WorkOrderId("A"), (WorkOrderId("A-1"),))
    with pytest.raises(NotImplementedError):
        graph.merge_work_orders((WorkOrderId("A"),), WorkOrderId("A-merged"))
    with pytest.raises(NotImplementedError):
        graph.replace_node(WorkOrderId("A"), WorkOrderId("A-new"))


def test_dependency_change_record_is_constructible() -> None:
    record = DependencyChangeRecord(
        removed=(dep("A", "B"),),
        added=(dep("B", "A"),),
        authority=TransitionAuthority.OPERATOR,
        reason="re-parent",
        occurred_at=_NOW,
    )

    assert record.change_id
    assert record.removed == (dep("A", "B"),)
    assert record.added == (dep("B", "A"),)
