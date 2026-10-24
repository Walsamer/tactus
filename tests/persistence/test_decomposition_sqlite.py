"""Durable child materialization: real SQLite transaction + fault injection."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tactus.application.decomposition import ChildMaterializer, MaterializedGeneration
from tactus.domain.admission import SourceRevision
from tactus.domain.decomposition import (
    ChildMaterializationPlan,
    ChildMaterializationSpec,
)
from tactus.domain.dependency import DependencyGraph
from tactus.domain.work_order import OpenStatus, WorkOrder, WorkOrderId, WorkOrderState
from tactus.domain.work_source import (
    RepositoryIdentity,
    WorkOrderSourceProvenance,
    WorkSourceRevision,
)
from tactus.persistence.decomposition import SqliteChildMaterialization

W = WorkOrderId
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)

REPOSITORY = RepositoryIdentity(
    owner="walsamer",
    name="tactus",
    repository_id=1404239931,
    url="https://github.com/walsamer/tactus",
)


@pytest.fixture()
def store(tmp_path: Path) -> SqliteChildMaterialization:
    materialization = SqliteChildMaterialization(tmp_path / "decomposition.db")
    materialization.add_edges([("UP", "WO-PARENT"), ("WO-PARENT", "DOWN")])
    yield materialization
    materialization.close()


def make_parent() -> WorkOrder:
    parent = WorkOrder.create(W("WO-PARENT"), "parent")
    parent.admit(reason="admitted", at=NOW)
    parent.set_readiness(OpenStatus.READY)
    parent.claim(reason="claimed", at=NOW)
    return parent


def make_plan() -> ChildMaterializationPlan:
    return ChildMaterializationPlan(
        parent_work_order_id=W("WO-PARENT"),
        parent_scope=("src/**",),
        children=(
            ChildMaterializationSpec(
                work_order_id=W("C1"), title="first", scope=("src/first.py",)
            ),
            ChildMaterializationSpec(
                work_order_id=W("C2"),
                title="second",
                scope=("src/second.py",),
                depends_on=(W("C1"),),
            ),
        ),
        max_children=3,
    )


def make_provenance() -> WorkOrderSourceProvenance:
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
        work_order_id=W("WO-PARENT"),
        import_key=revision.import_key,
        revision=revision,
        imported_at=NOW,
    )


def test_durable_materialization_commits_every_effect_together(
    store: SqliteChildMaterialization,
) -> None:
    parent = make_parent()
    outcome = ChildMaterializer(store).apply(
        decision_id="DEC-1",
        plan=make_plan(),
        parent=parent,
        graph=DependencyGraph(),
        parent_provenance=make_provenance(),
        applied_at=NOW,
    )

    assert outcome.replayed is False
    assert parent.state is WorkOrderState.RETIRED
    assert [row["work_order_id"] for row in store.child_rows()] == ["C1", "C2"]
    assert store.executable_children() == ()
    assert [row["work_order_id"] for row in store.provenance_rows()] == ["C1", "C2"]
    # The rewrite is durable and preserves the surrounding topology.
    assert store.edges() == (
        ("C1", "DOWN"),
        ("C2", "DOWN"),
        ("UP", "C1"),
        ("UP", "C2"),
    )


@pytest.mark.parametrize(
    "failpoint",
    ["after_children", "after_provenance", "after_edges", "before_parent_effect"],
)
def test_durable_fault_rolls_back_the_whole_application(
    store: SqliteChildMaterialization, failpoint: str
) -> None:
    parent = make_parent()
    edges_before = store.edges()

    def fault(name: str) -> None:
        if name == failpoint:
            raise RuntimeError(f"injected fault at {name}")

    with pytest.raises(RuntimeError):
        ChildMaterializer(store).apply(
            decision_id="DEC-1",
            plan=make_plan(),
            parent=parent,
            graph=DependencyGraph(),
            parent_provenance=make_provenance(),
            applied_at=NOW,
            fault=fault,
        )

    # SQLite rolled the whole transaction back: no partial generation at all.
    assert store.child_rows() == ()
    assert store.provenance_rows() == ()
    assert store.edges() == edges_before
    assert store.lookup(make_plan().materialization_key("DEC-1")) is None
    assert parent.state is WorkOrderState.ACTIVE


def test_durable_replay_applies_no_second_generation(
    store: SqliteChildMaterialization,
) -> None:
    parent = make_parent()
    materializer = ChildMaterializer(store)
    first = materializer.apply(
        decision_id="DEC-1",
        plan=make_plan(),
        parent=parent,
        graph=DependencyGraph(),
        parent_provenance=make_provenance(),
        applied_at=NOW,
    )
    edges_after_first = store.edges()

    second = materializer.apply(
        decision_id="DEC-1",
        plan=make_plan(),
        parent=parent,
        graph=DependencyGraph(),
        parent_provenance=make_provenance(),
        applied_at=NOW,
    )

    assert first.replayed is False
    assert second.replayed is True
    assert isinstance(second.generation, MaterializedGeneration)
    assert second.generation.child_ids == first.generation.child_ids
    assert len(store.child_rows()) == 2
    assert store.edges() == edges_after_first
