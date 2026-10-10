"""Import one checked GitHub Issue revision into a Tactus WorkOrder.

The application service composes:

* a work-source adapter that fetches the canonical Issue revision;
* the Tactus authorization gate (``fleet:ready`` opt-in);
* the durable source-provenance store (immutable snapshot + import key);
* the WorkOrder lifecycle entity.

Repeated imports of one revision are idempotent. A changed specification yields
a distinct content-derived WorkOrder identity, so an edited source never
silently rewrites a running or approved WorkOrder. Freshness/authorization are
rechecked before claim and dispatch through :func:`recheck_source`, which
returns typed :class:`~tactus.domain.work_source.SourceHold` evidence.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from tactus.domain._clock import utcnow
from tactus.domain.records import TransitionAuthority
from tactus.domain.work_order import WorkOrder, WorkOrderId
from tactus.domain.work_source import (
    SourceHold,
    SourceUnavailableError,
    WorkOrderSourceProvenance,
    WorkSourceStorePort,
    assert_importable,
    build_work_order_id,
    evaluate_source_freshness,
)


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Outcome of one idempotent issue import."""

    created: bool
    work_order: WorkOrder
    provenance: WorkOrderSourceProvenance
    holds: tuple[SourceHold, ...] = ()

    @property
    def work_order_id(self) -> WorkOrderId:
        return self.work_order.id


def import_issue(
    source,
    store: WorkSourceStorePort,
    repository: str,
    number: int,
    *,
    work_order_id: WorkOrderId | str | None = None,
    imported_at: datetime | None = None,
) -> ImportResult:
    """Import a checked Issue revision into a bound ``OPEN`` WorkOrder.

    The caller supplies the authorization context through the source adapter;
    this service fails closed before persisting anything if the revision is not
    an open ``fleet:ready`` Issue with no conflicting Fleet label.
    """

    revision = source.fetch(repository, number)
    assert_importable(revision)

    stored = store.record_revision(revision)
    if work_order_id is None:
        resolved_id = build_work_order_id(stored)
    elif isinstance(work_order_id, WorkOrderId):
        resolved_id = work_order_id
    else:
        resolved_id = WorkOrderId(work_order_id)

    existing = store.find_provenance(resolved_id)
    timestamp = imported_at if imported_at is not None else utcnow()
    provenance = WorkOrderSourceProvenance(
        work_order_id=resolved_id,
        import_key=stored.import_key,
        revision=stored,
        imported_at=timestamp,
    )
    # ``bind`` is idempotent for the same import key and fails closed on a
    # conflicting rebinding of the same WorkOrder.
    persisted = store.bind(provenance)

    work_order = WorkOrder.create(persisted.work_order_id, title=persisted.revision.title)
    work_order.admit(
        reason=f"imported source {persisted.import_key.value}",
        at=persisted.imported_at,
        authority=TransitionAuthority.SYSTEM,
    )
    return ImportResult(
        created=existing is None,
        work_order=work_order,
        provenance=persisted,
    )


def recheck_source(
    source,
    store: WorkSourceStorePort,
    work_order_id: WorkOrderId | str,
    *,
    at: datetime | None = None,
) -> tuple[SourceHold, ...]:
    """Recheck authorization and freshness for claim/dispatch.

    Returns typed holds; an unavailable source is an explicit hold, never an
    implicit success. Holds are evidence only: applying them to an ``OPEN``
    WorkOrder blocks new admission without rewriting an accepted attempt.
    """

    provenance = store.provenance_for(work_order_id)
    try:
        current = source.fetch(
            provenance.revision.repository.full_name, provenance.revision.number
        )
    except SourceUnavailableError:
        current = None
    return evaluate_source_freshness(provenance, current, at=at)


def apply_source_holds(work_order: WorkOrder, holds: Iterable[SourceHold]) -> None:
    """Apply typed source holds to an ``OPEN`` WorkOrder as blockers."""

    for hold in holds:
        work_order.add_blocker(hold.to_blocker())


__all__ = [
    "ImportResult",
    "apply_source_holds",
    "import_issue",
    "recheck_source",
]
