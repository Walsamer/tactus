"""Durable WorkSource persistence tests (issue #21).

Pin restart safety, idempotent revision replay, fail-closed rebinding and
parent-preserving child provenance for the SQLite adapter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tactus.domain import (
    ImmutableSourceConflictError,
    IssueReference,
    RepositoryIdentity,
    SourceRevision,
    WorkOrderId,
    WorkOrderSourceProvenance,
    WorkSourceError,
    WorkSourceRevision,
    WorkSourceStorePort,
)
from tactus.persistence import SqliteWorkSource

NOW = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
UPDATED = datetime(2026, 10, 10, 14, 35, 38, tzinfo=timezone.utc)

REPOSITORY = RepositoryIdentity(
    owner="walsamer",
    name="tactus",
    repository_id=1404239931,
    url="https://github.com/walsamer/tactus",
)


def make_revision(**overrides: object) -> WorkSourceRevision:
    defaults: dict[str, object] = dict(
        repository=REPOSITORY,
        number=21,
        url="https://github.com/walsamer/tactus/issues/21",
        title="Ingest a checked GitHub Issue revision into a Tactus WorkOrder",
        body="## Objective\n\nImmutable provenance.\n",
        source_revision=SourceRevision("2026-10-10T14:35:38Z"),
        updated_at=UPDATED,
        observed_at=NOW,
        labels=("area:control-plane", "fleet:ready"),
        dependencies=(
            IssueReference(
                repository="walsamer/tactus",
                number=3,
                url="https://github.com/walsamer/tactus/issues/3",
            ),
        ),
        issue_state="open",
    )
    defaults.update(overrides)
    return WorkSourceRevision(**defaults)  # type: ignore[arg-type]


def make_provenance(
    revision: WorkSourceRevision | None = None,
    *,
    work_order_id: str = "WO-1",
) -> WorkOrderSourceProvenance:
    revision = revision if revision is not None else make_revision()
    return WorkOrderSourceProvenance(
        work_order_id=WorkOrderId(work_order_id),
        import_key=revision.import_key,
        revision=revision,
        imported_at=NOW,
    )


def store(tmp_path, name: str = "work_source.sqlite") -> SqliteWorkSource:
    return SqliteWorkSource(tmp_path / name)


def test_adapter_satisfies_store_port(tmp_path) -> None:
    adapter = store(tmp_path)
    assert isinstance(adapter, WorkSourceStorePort)
    adapter.close()


# -- revisions ------------------------------------------------------------


def test_revision_and_binding_survive_restart(tmp_path) -> None:
    first = store(tmp_path)
    first.record_revision(make_revision())
    first.bind(make_provenance())
    first.close()

    restarted = store(tmp_path)
    assert restarted.revision(make_revision().import_key) == make_revision()
    provenance = restarted.provenance_for("WO-1")
    assert provenance.import_key == make_revision().import_key
    assert provenance.revision == make_revision()
    restarted.close()


def test_revision_replay_is_idempotent(tmp_path) -> None:
    adapter = store(tmp_path)
    recorded = adapter.record_revision(make_revision())
    assert adapter.record_revision(make_revision()) == recorded
    adapter.close()


def test_revision_replay_with_changed_observation_metadata_is_idempotent(tmp_path) -> None:
    adapter = store(tmp_path)
    original = adapter.record_revision(make_revision())
    changed = make_revision(
        updated_at=UPDATED + timedelta(hours=1),
        observed_at=NOW + timedelta(days=1),
        source_revision=SourceRevision("2026-10-11T00:00:00Z"),
    )
    assert changed.import_key == make_revision().import_key
    # Same content: the original immutable snapshot is returned unchanged.
    assert adapter.record_revision(changed) == original
    assert adapter.revision(original.import_key).observed_at == NOW
    adapter.close()


def test_unknown_revision_lookup_fails_closed(tmp_path) -> None:
    adapter = store(tmp_path)
    with pytest.raises(WorkSourceError):
        adapter.revision("missing")
    adapter.close()


# -- bindings -------------------------------------------------------------


def test_binding_is_idempotent(tmp_path) -> None:
    adapter = store(tmp_path)
    assert adapter.bind(make_provenance()) == make_provenance()
    assert adapter.bind(make_provenance()) == make_provenance()
    adapter.close()


def test_rebinding_same_work_order_to_different_revision_is_rejected(tmp_path) -> None:
    adapter = store(tmp_path)
    adapter.bind(make_provenance())
    edited = make_revision(body="edited body")
    with pytest.raises(ImmutableSourceConflictError):
        adapter.bind(make_provenance(edited))
    # The original binding is untouched.
    assert adapter.provenance_for("WO-1").revision.body != "edited body"
    adapter.close()


def test_one_issue_can_bind_several_work_orders(tmp_path) -> None:
    adapter = store(tmp_path)
    revision = make_revision()
    adapter.bind(make_provenance(revision, work_order_id="WO-1"))
    adapter.bind(make_provenance(revision, work_order_id="WO-2"))
    assert adapter.provenance_for("WO-1").import_key == revision.import_key
    assert adapter.provenance_for("WO-2").import_key == revision.import_key
    adapter.close()


def test_unknown_work_order_lookup_fails_closed(tmp_path) -> None:
    adapter = store(tmp_path)
    with pytest.raises(WorkSourceError):
        adapter.provenance_for("WO-missing")
    adapter.close()


# -- children -------------------------------------------------------------


def test_child_binding_preserves_parent_issue_provenance(tmp_path) -> None:
    adapter = store(tmp_path)
    parent = adapter.bind(make_provenance(work_order_id="WO-1"))
    child = adapter.bind_child("WO-1", "WO-1-child")

    assert child.import_key == parent.import_key
    assert child.revision == parent.revision
    assert child.parent_import_key == parent.import_key
    assert child.parent_work_order_id == parent.work_order_id
    assert child.work_order_id == WorkOrderId("WO-1-child")

    # The parent binding is unchanged by creating a child.
    assert adapter.provenance_for("WO-1") == parent
    adapter.close()
