"""Immutable GitHub Issue source provenance and import identity.

Ownership boundary (normative)
------------------------------
* GitHub owns canonical Issue intent. This module never mutates it and contains
  no transport, HTTP or persistence logic.
* Tactus owns the immutable source snapshot, the content-derived import key, the
  authorization/freshness evidence and the WorkOrder-to-source binding.
* An edit to the source never silently rewrites a WorkOrder. A changed
  specification produces a distinct, content-derived import key so that a
  reviewed revision/supersession must be a deliberate new binding.

This module implements the ``WorkSourceRevision`` contract from
``docs/architecture/CROSS_SYSTEM_CONTRACTS.md`` and the intake contract from
``docs/architecture/MIGRATION_FROM_FLEET.md``: persist repository/Issue
identity, title/body, observed revision, import time, an exact-body SHA-256, a
canonical specification digest, the labels/dependencies snapshot and an
idempotent import key.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from ._clock import utcnow
from .admission import SourceRevision
from .blockers import BlockReason, Blocker
from .readiness import AuthenticatedHold
from .work_order import WorkOrderId

#: Deliberate opt-in label; an open Issue alone is not authorization.
READY_LABEL = "fleet:ready"

#: Labels that revoke or override readiness.
BLOCKED_LABEL = "fleet:blocked"
HUMAN_LABEL = "fleet:human"
CONFLICTING_LABELS = frozenset({BLOCKED_LABEL, HUMAN_LABEL})

#: Observed Issue states for which new intake is permitted.
OPEN_ISSUE_STATE = "open"


def _require_non_empty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _require_aware(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be a timezone-aware datetime")


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# -- hashing --------------------------------------------------------------


def compute_body_sha256(body: str) -> str:
    """SHA-256 over the exact UTF-8 body bytes.

    The caller retains the immutable body snapshot alongside this digest; the
    digest is the change detector, never a substitute for the snapshot.
    """

    if not isinstance(body, str):
        raise ValueError("body must be a string")
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def compute_specification_digest(
    *,
    repository: "RepositoryIdentity",
    number: int,
    title: str,
    body: str,
    dependencies: Iterable["IssueReference"],
) -> str:
    """Hash the canonical title, body and relevant execution metadata.

    The digest deliberately excludes ``updated_at`` and other volatile
    timestamps: GitHub can change ``updated_at`` for unrelated metadata, so
    idempotency and change detection are content-derived. Authorization labels
    are recorded in the immutable source snapshot but deliberately excluded
    here so that adding/removing a Fleet status label is evaluated as an
    authorization change (see :func:`evaluate_source_freshness`), not as a
    rewritten specification.
    """

    payload = {
        "repository": repository.full_name,
        "repository_id": repository.repository_id,
        "issue": number,
        "title": title,
        "body": body,
        "dependencies": sorted(dep.url for dep in dependencies),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


# -- value objects --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    """Stable identity of the canonical repository."""

    owner: str
    name: str
    repository_id: int
    url: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner", _require_non_empty(self.owner, "owner"))
        object.__setattr__(self, "name", _require_non_empty(self.name, "name"))
        if isinstance(self.repository_id, bool) or not isinstance(self.repository_id, int):
            raise ValueError("repository_id must be an integer")
        object.__setattr__(self, "url", _require_non_empty(self.url, "url"))

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True, slots=True)
class IssueReference:
    """A referenced Issue (a dependency or parent link)."""

    repository: str
    number: int
    url: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "repository", _require_non_empty(self.repository, "repository")
        )
        if isinstance(self.number, bool) or not isinstance(self.number, int) or self.number < 1:
            raise ValueError("issue number must be a positive integer")
        object.__setattr__(self, "url", _require_non_empty(self.url, "url"))


@dataclass(frozen=True, slots=True)
class ImportKey:
    """Content-derived, idempotent identity for one checked source revision."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _require_non_empty(self.value, "ImportKey"))


def build_import_key(
    repository: RepositoryIdentity, number: int, specification_digest: str
) -> ImportKey:
    """Derive the import key from repository/Issue identity and content digest."""

    return ImportKey(f"{repository.full_name}#{number}@{specification_digest}")


@dataclass(frozen=True, slots=True)
class WorkSourceRevision:
    """An immutable, checked GitHub Issue revision.

    Everything needed to reconstruct the source specification and detect a
    later edit is captured here. ``updated_at`` is retained as observed source
    revision evidence but is not part of the change detector.
    """

    repository: RepositoryIdentity
    number: int
    url: str
    title: str
    body: str
    source_revision: SourceRevision
    updated_at: datetime
    observed_at: datetime
    labels: tuple[str, ...] = ()
    dependencies: tuple[IssueReference, ...] = ()
    issue_state: str = OPEN_ISSUE_STATE
    author: str | None = None
    milestone: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.number, bool) or not isinstance(self.number, int) or self.number < 1:
            raise ValueError("issue number must be a positive integer")
        object.__setattr__(self, "url", _require_non_empty(self.url, "url"))
        if not isinstance(self.title, str):
            raise ValueError("title must be a string")
        if not isinstance(self.body, str):
            raise ValueError("body must be a string")
        _require_aware(self.updated_at, "updated_at")
        _require_aware(self.observed_at, "observed_at")
        object.__setattr__(self, "labels", tuple(str(label) for label in self.labels))
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        object.__setattr__(
            self, "issue_state", _require_non_empty(self.issue_state, "issue_state").lower()
        )

    @property
    def body_sha256(self) -> str:
        return compute_body_sha256(self.body)

    @property
    def specification_digest(self) -> str:
        return compute_specification_digest(
            repository=self.repository,
            number=self.number,
            title=self.title,
            body=self.body,
            dependencies=self.dependencies,
        )

    @property
    def import_key(self) -> ImportKey:
        return build_import_key(self.repository, self.number, self.specification_digest)

    @property
    def is_open(self) -> bool:
        return self.issue_state == OPEN_ISSUE_STATE

    @property
    def is_authorized(self) -> bool:
        """``fleet:ready`` present with no conflicting Fleet status label."""

        return READY_LABEL in self.labels and not (CONFLICTING_LABELS & set(self.labels))


@dataclass(frozen=True, slots=True)
class WorkOrderSourceProvenance:
    """Immutable binding of one WorkOrder to one checked source revision.

    ``parent_import_key``/``parent_work_order_id`` are set for decomposed
    children so they preserve the parent Issue provenance; completing a child
    therefore still refers to the same Issue revision.
    """

    work_order_id: WorkOrderId
    import_key: ImportKey
    revision: WorkSourceRevision
    imported_at: datetime
    parent_import_key: ImportKey | None = None
    parent_work_order_id: WorkOrderId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.work_order_id, WorkOrderId):
            object.__setattr__(self, "work_order_id", WorkOrderId(self.work_order_id))
        if not isinstance(self.import_key, ImportKey):
            object.__setattr__(self, "import_key", ImportKey(self.import_key))
        _require_aware(self.imported_at, "imported_at")
        if self.parent_work_order_id is not None and not isinstance(
            self.parent_work_order_id, WorkOrderId
        ):
            object.__setattr__(
                self, "parent_work_order_id", WorkOrderId(self.parent_work_order_id)
            )
        if self.parent_import_key is not None and not isinstance(
            self.parent_import_key, ImportKey
        ):
            object.__setattr__(self, "parent_import_key", ImportKey(self.parent_import_key))

    def as_child(self, child_work_order_id: WorkOrderId | str) -> "WorkOrderSourceProvenance":
        """Return child provenance that preserves this Issue revision."""

        return WorkOrderSourceProvenance(
            work_order_id=WorkOrderId(child_work_order_id)
            if not isinstance(child_work_order_id, WorkOrderId)
            else child_work_order_id,
            import_key=self.import_key,
            revision=self.revision,
            imported_at=self.imported_at,
            parent_import_key=self.import_key,
            parent_work_order_id=self.work_order_id,
        )


# -- typed holds ----------------------------------------------------------


class SourceHoldReason(str, Enum):
    """Why a WorkOrder must be held against its source revision."""

    SOURCE_CHANGED = "SOURCE_CHANGED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    AUTHORIZATION_REMOVED = "AUTHORIZATION_REMOVED"
    ISSUE_CLOSED = "ISSUE_CLOSED"


@dataclass(frozen=True, slots=True)
class SourceHold:
    """A typed claim/dispatch hold caused by source provenance.

    This is evidence, not an action selector: it never mutates lifecycle state
    or rewrites an already accepted authorization.
    """

    reason: SourceHoldReason
    detail: str
    held_at: datetime = field(default_factory=utcnow)
    evidence: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.reason, SourceHoldReason):
            raise ValueError("hold reason must be a SourceHoldReason")
        _require_aware(self.held_at, "held_at")
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))

    def to_blocker(self) -> Blocker:
        return Blocker(
            reason=BlockReason.SOURCE_CHANGED,
            detail=self.detail,
            subreason=self.reason.value,
            evidence=self.evidence,
            blocked_at=self.held_at,
        )

    def to_authenticated_hold(self, *, authenticated_by: str = "work_source") -> AuthenticatedHold:
        return AuthenticatedHold(
            reason=BlockReason.SOURCE_CHANGED,
            detail=self.detail,
            subreason=self.reason.value,
            evidence=self.evidence,
            held_at=self.held_at,
            authenticated_by=authenticated_by,
        )


# -- errors ---------------------------------------------------------------


class WorkSourceError(ValueError):
    """Base class for invalid work-source operations."""


class SourceUnavailableError(WorkSourceError):
    """The canonical source could not be checked (transport/absence error)."""


class SourceNotAuthorizedError(WorkSourceError):
    """The Issue is not deliberately authorized for intake."""


class IssueNotOpenError(SourceNotAuthorizedError):
    """A closed Issue never authorizes new intake."""


class MissingReadyLabelError(SourceNotAuthorizedError):
    """The required ``fleet:ready`` label is absent."""


class ConflictingFleetLabelError(SourceNotAuthorizedError):
    """A conflicting Fleet status label is present."""


class ImmutableSourceConflictError(WorkSourceError):
    """A WorkOrder was rebound to a different source revision."""


# -- admission / authorization -------------------------------------------


def assert_importable(revision: WorkSourceRevision) -> None:
    """Fail closed unless the checked revision is authorized for new intake.

    Only an open Issue with ``fleet:ready`` and no conflicting
    ``fleet:blocked``/``fleet:human`` label authorizes a new import.
    """

    if not revision.is_open:
        raise IssueNotOpenError(
            f"issue {revision.url} is {revision.issue_state!r}; new intake requires an open issue"
        )
    if READY_LABEL not in revision.labels:
        raise MissingReadyLabelError(
            f"issue {revision.url} is missing the {READY_LABEL!r} label"
        )
    conflicting = sorted(CONFLICTING_LABELS & set(revision.labels))
    if conflicting:
        raise ConflictingFleetLabelError(
            f"issue {revision.url} has conflicting Fleet labels: {', '.join(conflicting)}"
        )


# -- freshness / dispatch recheck ----------------------------------------


def evaluate_source_freshness(
    provenance: WorkOrderSourceProvenance,
    current: WorkSourceRevision | None,
    *,
    at: datetime | None = None,
) -> tuple[SourceHold, ...]:
    """Recheck authorization and freshness against the canonical source.

    Called before claim and immediately before dispatch/publication. ``None``
    means the source could not be checked and therefore produces an
    unavailable hold. Content is compared through the exact-body and
    specification digests, so an ``updated_at``-only change is not a stale
    source. The original authorization of an already-running attempt is never
    rewritten by this function: it only returns holds for the caller to apply.
    """

    held_at = at if at is not None else utcnow()
    stored = provenance.revision

    if current is None:
        return (
            SourceHold(
                reason=SourceHoldReason.SOURCE_UNAVAILABLE,
                detail="the WorkOrder source could not be rechecked",
                held_at=held_at,
                evidence={
                    "import_key": provenance.import_key.value,
                    "source_revision": stored.source_revision.value,
                },
            ),
        )

    holds: list[SourceHold] = []
    evidence = {
        "import_key": provenance.import_key.value,
        "stored_source_revision": stored.source_revision.value,
        "current_source_revision": current.source_revision.value,
    }

    if not current.is_open:
        holds.append(
            SourceHold(
                reason=SourceHoldReason.ISSUE_CLOSED,
                detail="the source Issue is no longer open",
                held_at=held_at,
                evidence={**evidence, "issue_state": current.issue_state},
            )
        )

    if not current.is_authorized:
        holds.append(
            SourceHold(
                reason=SourceHoldReason.AUTHORIZATION_REMOVED,
                detail="the source no longer carries deliberate fleet:ready authorization",
                held_at=held_at,
                evidence=evidence,
            )
        )

    if (
        current.body_sha256 != stored.body_sha256
        or current.specification_digest != stored.specification_digest
    ):
        holds.append(
            SourceHold(
                reason=SourceHoldReason.SOURCE_CHANGED,
                detail="the source specification changed since it was imported",
                held_at=held_at,
                evidence={
                    **evidence,
                    "stored_body_sha256": stored.body_sha256,
                    "current_body_sha256": current.body_sha256,
                    "stored_specification_digest": stored.specification_digest,
                    "current_specification_digest": current.specification_digest,
                },
            )
        )

    # An updated_at-only change is unrelated metadata and deliberately ignored.
    return tuple(holds)


@runtime_checkable
class WorkSourceStorePort(Protocol):
    """Persistence boundary for immutable source revisions and bindings.

    Implementations must be idempotent for one import key and must never
    silently rewrite a WorkOrder bound to a different revision.
    """

    def record_revision(self, revision: WorkSourceRevision) -> WorkSourceRevision: ...

    def revision(self, import_key: ImportKey | str) -> WorkSourceRevision: ...

    def bind(self, provenance: WorkOrderSourceProvenance) -> WorkOrderSourceProvenance: ...

    def provenance_for(
        self, work_order_id: WorkOrderId | str
    ) -> WorkOrderSourceProvenance: ...

    def find_provenance(
        self, work_order_id: WorkOrderId | str
    ) -> WorkOrderSourceProvenance | None: ...

    def bind_child(
        self,
        parent_work_order_id: WorkOrderId | str,
        child_work_order_id: WorkOrderId | str,
        *,
        imported_at: datetime | None = None,
    ) -> WorkOrderSourceProvenance: ...


def build_work_order_id(revision: WorkSourceRevision) -> WorkOrderId:
    """Deterministic id for the base WorkOrder derived from one source revision.

    Content-derived so that re-importing the same revision yields the same
    WorkOrder, while an edited specification yields a distinct identity instead
    of rewriting an existing WorkOrder.
    """

    slug = revision.repository.full_name.replace("/", "-").lower()
    return WorkOrderId(
        f"{slug}-issue-{revision.number}-{revision.specification_digest[:12]}"
    )


__all__ = [
    "BLOCKED_LABEL",
    "CONFLICTING_LABELS",
    "ConflictingFleetLabelError",
    "HUMAN_LABEL",
    "ImmutableSourceConflictError",
    "ImportKey",
    "IssueNotOpenError",
    "IssueReference",
    "MissingReadyLabelError",
    "OPEN_ISSUE_STATE",
    "READY_LABEL",
    "RepositoryIdentity",
    "SourceHold",
    "SourceHoldReason",
    "SourceNotAuthorizedError",
    "SourceUnavailableError",
    "WorkOrderSourceProvenance",
    "WorkSourceError",
    "WorkSourceRevision",
    "WorkSourceStorePort",
    "assert_importable",
    "build_import_key",
    "build_work_order_id",
    "compute_body_sha256",
    "compute_specification_digest",
    "evaluate_source_freshness",
]
