"""Atomic, idempotent child materialization (issue #10, criteria 2/3/4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tactus.application.decomposition import (
    ChildMaterializer,
    InMemoryMaterializationStore,
    LedgerConflictError,
    MaterializationError,
)
from tactus.domain.admission import SourceRevision
from tactus.domain.decomposition import (
    ChildMaterializationPlan,
    ChildMaterializationSpec,
)
from tactus.domain.dependency import DependencyGraph, WorkOrderDependency
from tactus.domain.work_order import OpenStatus, WorkOrder, WorkOrderId, WorkOrderState
from tactus.domain.work_source import (
    RepositoryIdentity,
    WorkOrderSourceProvenance,
    WorkSourceRevision,
)

W = WorkOrderId
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)

REPOSITORY = RepositoryIdentity(
    owner="walsamer",
    name="tactus",
    repository_id=1404239931,
    url="https://github.com/walsamer/tactus",
)


def make_parent() -> WorkOrder:
    parent = WorkOrder.create(W("WO-PARENT"), "parent")
    parent.admit(reason="admitted", at=NOW)
    parent.set_readiness(OpenStatus.READY)
    parent.claim(reason="claimed", at=NOW)
    assert parent.state is WorkOrderState.ACTIVE
    return parent


def make_provenance(work_order_id: str = "WO-PARENT") -> WorkOrderSourceProvenance:
    revision = WorkSourceRevision(
        repository=REPOSITORY,
        number=10,
        url="https://github.com/walsamer/tactus/issues/10",
        title="Materialize validated split/replan children",
        body="## Objective\n\nAtomic children.\n",
        source_revision=SourceRevision("2026-10-10T14:35:38Z"),
        updated_at=NOW,
        observed_at=NOW,
        labels=("area:control-plane",),
        dependencies=(),
        issue_state="open",
        author="walsamer",
    )
    return WorkOrderSourceProvenance(
        work_order_id=W(work_order_id),
        import_key=revision.import_key,
        revision=revision,
        imported_at=NOW,
    )


def make_plan() -> ChildMaterializationPlan:
    return ChildMaterializationPlan(
        parent_work_order_id=W("WO-PARENT"),
        parent_scope=("src/**",),
        children=(
            ChildMaterializationSpec(
                work_order_id=W("C1"), title="first half", scope=("src/first.py",)
            ),
            ChildMaterializationSpec(
                work_order_id=W("C2"),
                title="second half",
                scope=("src/second.py",),
                depends_on=(W("C1"),),
            ),
        ),
        max_children=3,
    )


def make_graph() -> DependencyGraph:
    return DependencyGraph(
        [
            WorkOrderDependency(upstream=W("UP"), downstream=W("WO-PARENT")),
            WorkOrderDependency(upstream=W("WO-PARENT"), downstream=W("DOWN")),
        ]
    )


# -- criterion 2: atomic commit ------------------------------------------


def test_materialization_writes_children_provenance_edges_and_parent_effect() -> None:
    store = InMemoryMaterializationStore()
    parent = make_parent()
    graph = make_graph()

    outcome = ChildMaterializer(store).apply(
        decision_id="DEC-1",
        plan=make_plan(),
        parent=parent,
        graph=graph,
        parent_provenance=make_provenance(),
        applied_at=NOW,
    )

    assert outcome.replayed is False
    assert outcome.generation.child_ids == (W("C1"), W("C2"))
    assert parent.state is WorkOrderState.RETIRED

    # Children exist, are non-executable, and carry the parent's Issue revision.
    assert set(store.children) == {W("C1"), W("C2")}
    assert all(child.state is WorkOrderState.DRAFT for child in store.children.values())
    assert store.executable_children() == ()
    assert store.provenance[W("C1")].revision == store.provenance[W("C2")].revision
    assert store.provenance[W("C1")].parent_work_order_id == W("WO-PARENT")
    assert store.provenance[W("C1")].parent_import_key is not None

    # Dependency rewrite preserved the surrounding topology.
    assert graph.upstream_of(W("C1")) == (W("UP"),)
    assert graph.upstream_of(W("C2")) == (W("UP"),)
    assert graph.downstream_of(W("C1")) == (W("DOWN"),)
    assert graph.downstream_of(W("C2")) == (W("DOWN"),)
    assert graph.upstream_of(W("WO-PARENT")) == ()


@pytest.mark.parametrize(
    "failpoint",
    ["after_children", "after_provenance", "after_edges", "before_parent_effect"],
)
def test_fault_injection_leaves_no_executable_partial_children(failpoint: str) -> None:
    store = InMemoryMaterializationStore()
    parent = make_parent()
    graph = make_graph()
    before_edges = graph.edges()

    def fault(name: str) -> None:
        if name == failpoint:
            raise RuntimeError(f"injected fault at {name}")

    with pytest.raises(RuntimeError):
        ChildMaterializer(store).apply(
            decision_id="DEC-1",
            plan=make_plan(),
            parent=parent,
            graph=graph,
            parent_provenance=make_provenance(),
            applied_at=NOW,
            fault=fault,
        )

    # Nothing survives the failed transaction.
    assert store.children == {}
    assert store.provenance == {}
    assert store.retired_parents == ()
    assert graph.edges() == before_edges
    assert parent.state is WorkOrderState.ACTIVE
    # And the decision can be retried cleanly.
    assert store.lookup(make_plan().materialization_key("DEC-1")) is None


# -- criterion 3: idempotent replay --------------------------------------


def test_replaying_the_same_decision_applies_no_second_effect() -> None:
    store = InMemoryMaterializationStore()
    parent = make_parent()
    graph = make_graph()
    materializer = ChildMaterializer(store)

    first = materializer.apply(
        decision_id="DEC-1",
        plan=make_plan(),
        parent=parent,
        graph=graph,
        parent_provenance=make_provenance(),
        applied_at=NOW,
    )
    edges_after_first = graph.edges()

    second = materializer.apply(
        decision_id="DEC-1",
        plan=make_plan(),
        parent=parent,
        graph=graph,
        parent_provenance=make_provenance(),
        applied_at=NOW + timedelta(minutes=5),
    )

    assert first.replayed is False
    assert second.replayed is True
    assert second.generation.child_ids == first.generation.child_ids
    assert len(store.children) == 2
    assert graph.edges() == edges_after_first
    assert store.retired_parents == (W("WO-PARENT"),)


def test_repeated_plan_construction_yields_the_same_child_identities() -> None:
    first = make_plan()
    second = make_plan()
    assert first.child_ids == second.child_ids
    assert first.materialization_key("DEC-1") == second.materialization_key("DEC-1")


# -- guarded preconditions ------------------------------------------------


def test_materialization_requires_an_active_parent() -> None:
    store = InMemoryMaterializationStore()
    draft = WorkOrder.create(W("WO-PARENT"), "parent")
    with pytest.raises(MaterializationError):
        ChildMaterializer(store).apply(
            decision_id="DEC-1", plan=make_plan(), parent=draft, graph=make_graph()
        )
    assert store.children == {}


def test_plan_must_target_the_supplied_parent() -> None:
    store = InMemoryMaterializationStore()
    parent = make_parent()
    plan = ChildMaterializationPlan(
        parent_work_order_id=W("OTHER-PARENT"),
        parent_scope=("src/**",),
        children=(
            ChildMaterializationSpec(
                work_order_id=W("C1"), title="x", scope=("src/x.py",)
            ),
        ),
        max_children=2,
    )
    with pytest.raises(MaterializationError):
        ChildMaterializer(store).apply(
            decision_id="DEC-1", plan=plan, parent=parent, graph=make_graph()
        )


def test_reusing_a_key_with_different_children_is_rejected() -> None:
    """A ledger entry that disagrees with the plan must fail closed."""

    from tactus.application.decomposition import MaterializedGeneration
    from tactus.domain.dependency import DependencyChangeRecord
    from tactus.domain.records import TransitionAuthority

    seeded = MaterializedGeneration(
        materialization_key=make_plan().materialization_key("DEC-1"),
        parent_work_order_id=W("WO-PARENT"),
        child_ids=(W("C9"),),
        dependency_change=DependencyChangeRecord(
            removed=(),
            added=(),
            authority=TransitionAuthority.RECOVERY,
            reason="seed",
            occurred_at=NOW,
        ),
        parent_state=WorkOrderState.RETIRED,
        applied_at=NOW,
    )

    class SeededStore:
        def lookup(self, key: str):
            return seeded

        def materialize(self, **kwargs: object):
            raise AssertionError("materialize must not run for a conflicting key")

    with pytest.raises(LedgerConflictError):
        ChildMaterializer(SeededStore()).apply(  # type: ignore[arg-type]
            decision_id="DEC-1",
            plan=make_plan(),
            parent=make_parent(),
            graph=make_graph(),
        )
