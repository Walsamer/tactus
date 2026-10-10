"""Work-source provenance and import-identity domain tests (issue #21).

These tests pin the intake contract from
``docs/architecture/MIGRATION_FROM_FLEET.md``: exact-body/specification hashes,
content-derived idempotent import keys, the ``fleet:ready`` authorization gate,
typed claim/dispatch holds and parent-preserving child provenance.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from tactus.domain import (
    ConflictingFleetLabelError,
    ImportKey,
    IssueNotOpenError,
    IssueReference,
    MissingReadyLabelError,
    READY_LABEL,
    RepositoryIdentity,
    SourceHoldReason,
    SourceRevision,
    WorkOrderId,
    WorkOrderSourceProvenance,
    WorkSourceRevision,
    assert_importable,
    build_import_key,
    build_work_order_id,
    compute_body_sha256,
    evaluate_source_freshness,
)

NOW = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
UPDATED = datetime(2026, 10, 10, 14, 35, 38, tzinfo=timezone.utc)

REPOSITORY = RepositoryIdentity(
    owner="walsamer",
    name="tactus",
    repository_id=1404239931,
    url="https://github.com/walsamer/tactus",
)
BODY = "## Objective\n\nImmutable provenance.\n"
DEPENDENCIES = (
    IssueReference(
        repository="walsamer/tactus", number=3, url="https://github.com/walsamer/tactus/issues/3"
    ),
    IssueReference(
        repository="walsamer/tactus", number=4, url="https://github.com/walsamer/tactus/issues/4"
    ),
)


def make_revision(**overrides: object) -> WorkSourceRevision:
    defaults: dict[str, object] = dict(
        repository=REPOSITORY,
        number=21,
        url="https://github.com/walsamer/tactus/issues/21",
        title="Ingest a checked GitHub Issue revision into a Tactus WorkOrder",
        body=BODY,
        source_revision=SourceRevision("2026-10-10T14:35:38Z"),
        updated_at=UPDATED,
        observed_at=NOW,
        labels=("area:control-plane", READY_LABEL, "priority:p0"),
        dependencies=DEPENDENCIES,
        issue_state="open",
        author="walsamer",
    )
    defaults.update(overrides)
    return WorkSourceRevision(**defaults)  # type: ignore[arg-type]


def make_provenance(revision: WorkSourceRevision | None = None) -> WorkOrderSourceProvenance:
    revision = revision if revision is not None else make_revision()
    return WorkOrderSourceProvenance(
        work_order_id=WorkOrderId("WO-1"),
        import_key=revision.import_key,
        revision=revision,
        imported_at=NOW,
    )


# -- hashing --------------------------------------------------------------


def test_body_sha256_hashes_exact_utf8_bytes() -> None:
    assert compute_body_sha256(BODY) == hashlib.sha256(BODY.encode("utf-8")).hexdigest()
    # Different bytes -> different digest; unicode is hashed as UTF-8.
    assert compute_body_sha256("café") == hashlib.sha256("café".encode("utf-8")).hexdigest()
    assert compute_body_sha256("café") != compute_body_sha256("cafe")


def test_specification_digest_is_deterministic_and_ignores_updated_at() -> None:
    first = make_revision()
    second = make_revision(
        updated_at=UPDATED + timedelta(hours=3),
        observed_at=NOW + timedelta(days=1),
        source_revision=SourceRevision("2026-10-11T00:00:00Z"),
    )
    assert first.specification_digest == second.specification_digest
    assert first.import_key == second.import_key


def test_specification_digest_changes_with_content_and_dependencies() -> None:
    base = make_revision().specification_digest
    assert make_revision(body="different").specification_digest != base
    assert make_revision(title="Different title").specification_digest != base
    assert make_revision(dependencies=()).specification_digest != base
    # Authorization labels are tracked separately, not as a rewritten spec.
    assert make_revision(labels=("area:control-plane",)).specification_digest == base


def test_import_key_binds_repository_issue_and_digest() -> None:
    revision = make_revision()
    expected = build_import_key(REPOSITORY, 21, revision.specification_digest)
    assert revision.import_key == expected
    assert revision.import_key.value == f"walsamer/tactus#21@{revision.specification_digest}"


def test_build_work_order_id_is_content_derived_and_distinct_on_edit() -> None:
    first = build_work_order_id(make_revision())
    replayed = build_work_order_id(make_revision())
    edited = build_work_order_id(make_revision(body="edited"))
    assert first == replayed
    assert first != edited


# -- authorization gate ---------------------------------------------------


def test_assert_importable_accepts_open_fleet_ready_issue() -> None:
    assert_importable(make_revision())


def test_assert_importable_rejects_missing_ready_label() -> None:
    with pytest.raises(MissingReadyLabelError):
        assert_importable(make_revision(labels=("area:control-plane",)))


@pytest.mark.parametrize("conflict", ["fleet:blocked", "fleet:human"])
def test_assert_importable_rejects_conflicting_fleet_labels(conflict: str) -> None:
    with pytest.raises(ConflictingFleetLabelError):
        assert_importable(make_revision(labels=(READY_LABEL, conflict)))


def test_assert_importable_rejects_closed_issue() -> None:
    with pytest.raises(IssueNotOpenError):
        assert_importable(make_revision(issue_state="closed"))


# -- freshness / claim-dispatch holds -------------------------------------


def test_same_revision_produces_no_hold() -> None:
    revision = make_revision()
    assert evaluate_source_freshness(make_provenance(revision), revision, at=NOW) == ()


def test_updated_at_only_change_is_not_a_hold() -> None:
    stored = make_revision()
    current = make_revision(updated_at=UPDATED + timedelta(hours=1))
    assert evaluate_source_freshness(make_provenance(stored), current, at=NOW) == ()


def test_body_edit_produces_source_changed_hold() -> None:
    holds = evaluate_source_freshness(
        make_provenance(make_revision()), make_revision(body="edited"), at=NOW
    )
    assert [hold.reason for hold in holds] == [SourceHoldReason.SOURCE_CHANGED]


def test_authorization_removal_produces_typed_hold() -> None:
    holds = evaluate_source_freshness(
        make_provenance(make_revision()),
        make_revision(labels=("area:control-plane",)),
        at=NOW,
    )
    assert [hold.reason for hold in holds] == [SourceHoldReason.AUTHORIZATION_REMOVED]
    blocker = holds[0].to_blocker()
    assert blocker.reason.value == "SOURCE_CHANGED"
    assert blocker.subreason == SourceHoldReason.AUTHORIZATION_REMOVED.value


def test_closed_issue_produces_issue_closed_hold() -> None:
    holds = evaluate_source_freshness(
        make_provenance(make_revision()), make_revision(issue_state="closed"), at=NOW
    )
    assert [hold.reason for hold in holds] == [SourceHoldReason.ISSUE_CLOSED]


def test_unavailable_source_produces_typed_hold() -> None:
    holds = evaluate_source_freshness(make_provenance(make_revision()), None, at=NOW)
    assert [hold.reason for hold in holds] == [SourceHoldReason.SOURCE_UNAVAILABLE]
    authenticated = holds[0].to_authenticated_hold()
    assert authenticated.reason.value == "SOURCE_CHANGED"
    assert authenticated.subreason == SourceHoldReason.SOURCE_UNAVAILABLE.value


# -- child provenance -----------------------------------------------------


def test_child_provenance_preserves_parent_issue_revision() -> None:
    parent = make_provenance()
    child = parent.as_child(WorkOrderId("WO-2"))
    assert child.import_key == parent.import_key
    assert child.revision == parent.revision
    assert child.parent_import_key == parent.import_key
    assert child.parent_work_order_id == parent.work_order_id
    assert child.work_order_id == WorkOrderId("WO-2")


def test_import_key_requires_non_empty_value() -> None:
    with pytest.raises(ValueError):
        ImportKey("")
