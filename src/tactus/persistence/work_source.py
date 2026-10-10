"""SQLite implementation of the durable WorkSource provenance boundary.

This adapter owns only Tactus source-provenance records: immutable checked
source revisions, the content-derived import key and the WorkOrder-to-revision
binding (including parent/child provenance). It contains no GitHub transport,
no intake policy and no WorkOrder lifecycle logic.

Idempotency is enforced by the import key: replaying a byte-identical revision
returns the original record, while binding one WorkOrder to a different revision
fails closed instead of silently rewriting an authorized or running WorkOrder.
One source revision may still yield several bound WorkOrders.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from tactus.domain.admission import SourceRevision
from tactus.domain.work_order import WorkOrderId
from tactus.domain.work_source import (
    ImmutableSourceConflictError,
    ImportKey,
    IssueReference,
    RepositoryIdentity,
    WorkOrderSourceProvenance,
    WorkSourceError,
    WorkSourceRevision,
    build_import_key,
)


_MIGRATION_ID = "0003_work_source"


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _as_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _encode_dependencies(dependencies: tuple[IssueReference, ...]) -> str:
    return json.dumps(
        [
            {"repository": dep.repository, "number": dep.number, "url": dep.url}
            for dep in dependencies
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_dependencies(value: str) -> tuple[IssueReference, ...]:
    return tuple(
        IssueReference(repository=item["repository"], number=item["number"], url=item["url"])
        for item in json.loads(value)
    )


def _encode_labels(labels: tuple[str, ...]) -> str:
    return json.dumps(list(labels), separators=(",", ":"))


def _decode_labels(value: str) -> tuple[str, ...]:
    return tuple(json.loads(value))


def _revision_from_row(row: sqlite3.Row) -> WorkSourceRevision:
    return WorkSourceRevision(
        repository=RepositoryIdentity(
            owner=row["repository_owner"],
            name=row["repository_name"],
            repository_id=row["repository_id"],
            url=row["repository_url"],
        ),
        number=row["issue_number"],
        url=row["issue_url"],
        title=row["issue_title"],
        body=row["issue_body"],
        source_revision=SourceRevision(row["source_revision"]),
        updated_at=_as_datetime(row["source_updated_at"]),
        observed_at=_as_datetime(row["observed_at"]),
        labels=_decode_labels(row["labels"]),
        dependencies=_decode_dependencies(row["dependencies"]),
        issue_state=row["issue_state"],
        author=row["author"],
        milestone=row["milestone"],
    )


class SqliteWorkSource:
    """Transactional SQLite store for source revisions and WorkOrder bindings."""

    def __init__(self, database: str | Path, *, timeout: float = 5.0) -> None:
        self._connection = sqlite3.connect(
            str(database), timeout=timeout, isolation_level=None, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
        self.migrate()

    def close(self) -> None:
        self._connection.close()

    def migrate(self) -> None:
        """Apply the idempotent work-source schema migration."""

        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY)"
        )
        applied = self._connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (_MIGRATION_ID,)
        ).fetchone()
        if applied is not None:
            return
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS work_source_revisions (
                import_key TEXT PRIMARY KEY,
                repository_owner TEXT NOT NULL,
                repository_name TEXT NOT NULL,
                repository_id INTEGER NOT NULL,
                repository_url TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                issue_url TEXT NOT NULL,
                issue_title TEXT NOT NULL,
                issue_body TEXT NOT NULL,
                source_revision TEXT NOT NULL,
                source_updated_at TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                body_sha256 TEXT NOT NULL,
                specification_digest TEXT NOT NULL,
                labels TEXT NOT NULL,
                dependencies TEXT NOT NULL,
                issue_state TEXT NOT NULL,
                author TEXT,
                milestone TEXT
            );
            CREATE TABLE IF NOT EXISTS work_order_source_provenance (
                work_order_id TEXT PRIMARY KEY,
                import_key TEXT NOT NULL
                    REFERENCES work_source_revisions(import_key),
                imported_at TEXT NOT NULL,
                parent_import_key TEXT,
                parent_work_order_id TEXT
            );
            """
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)", (_MIGRATION_ID,)
        )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

    # -- immutable revisions ---------------------------------------------

    def record_revision(self, revision: WorkSourceRevision) -> WorkSourceRevision:
        """Persist one checked revision; identical replay is idempotent.

        Idempotency is keyed by the content-derived import key, so replaying the
        same specification at a later observation time returns the original
        immutable snapshot rather than conflicting on observation metadata.
        """

        if not isinstance(revision, WorkSourceRevision):
            raise WorkSourceError("revision must be a WorkSourceRevision")
        key = revision.import_key
        with self._transaction():
            existing = self._connection.execute(
                "SELECT * FROM work_source_revisions WHERE import_key = ?", (key.value,)
            ).fetchone()
            if existing is not None:
                return _revision_from_row(existing)
            self._persist_revision(revision)
            return revision

    def revision(self, import_key: ImportKey | str) -> WorkSourceRevision:
        key = import_key.value if isinstance(import_key, ImportKey) else str(import_key)
        row = self._connection.execute(
            "SELECT * FROM work_source_revisions WHERE import_key = ?", (key,)
        ).fetchone()
        if row is None:
            raise WorkSourceError(f"source revision {key!r} is not recorded")
        return _revision_from_row(row)

    # -- WorkOrder bindings ----------------------------------------------

    def bind(self, provenance: WorkOrderSourceProvenance) -> WorkOrderSourceProvenance:
        """Bind a WorkOrder to its source revision.

        A byte-identical replay is idempotent. Rebinding the same WorkOrder to a
        different revision is rejected: an edited source must become a reviewed
        new/superseding WorkOrder, never a silent rewrite.
        """

        if not isinstance(provenance, WorkOrderSourceProvenance):
            raise WorkSourceError("provenance must be a WorkOrderSourceProvenance")
        work_order_id = str(provenance.work_order_id)
        with self._transaction():
            # Store the referenced revision in the same transaction so a
            # binding can never dangle.
            self._record_revision_locked(provenance.revision)
            existing = self._connection.execute(
                "SELECT * FROM work_order_source_provenance WHERE work_order_id = ?",
                (work_order_id,),
            ).fetchone()
            if existing is not None:
                if existing["import_key"] != provenance.import_key.value:
                    raise ImmutableSourceConflictError(
                        f"WorkOrder {work_order_id!r} is already bound to "
                        f"{existing['import_key']!r}; refusing to rewrite it with "
                        f"{provenance.import_key.value!r}"
                    )
                return self._provenance_from_row(existing)
            self._connection.execute(
                "INSERT INTO work_order_source_provenance VALUES (?,?,?,?,?)",
                (
                    work_order_id,
                    provenance.import_key.value,
                    _timestamp(provenance.imported_at),
                    provenance.parent_import_key.value
                    if provenance.parent_import_key is not None
                    else None,
                    str(provenance.parent_work_order_id)
                    if provenance.parent_work_order_id is not None
                    else None,
                ),
            )
        return provenance

    def find_provenance(self, work_order_id: WorkOrderId | str) -> WorkOrderSourceProvenance | None:
        row = self._connection.execute(
            "SELECT * FROM work_order_source_provenance WHERE work_order_id = ?",
            (str(work_order_id),),
        ).fetchone()
        return None if row is None else self._provenance_from_row(row)

    def provenance_for(self, work_order_id: WorkOrderId | str) -> WorkOrderSourceProvenance:
        provenance = self.find_provenance(work_order_id)
        if provenance is None:
            raise WorkSourceError(f"WorkOrder {str(work_order_id)!r} has no source provenance")
        return provenance

    def bind_child(
        self,
        parent_work_order_id: WorkOrderId | str,
        child_work_order_id: WorkOrderId | str,
        *,
        imported_at: datetime | None = None,
    ) -> WorkOrderSourceProvenance:
        """Bind a child WorkOrder, preserving the parent Issue provenance."""

        parent = self.provenance_for(parent_work_order_id)
        child = WorkOrderSourceProvenance(
            work_order_id=WorkOrderId(child_work_order_id),
            import_key=parent.import_key,
            revision=parent.revision,
            imported_at=imported_at if imported_at is not None else parent.imported_at,
            parent_import_key=parent.import_key,
            parent_work_order_id=parent.work_order_id,
        )
        return self.bind(child)

    # -- internals --------------------------------------------------------

    def _record_revision_locked(self, revision: WorkSourceRevision) -> None:
        key = revision.import_key
        existing = self._connection.execute(
            "SELECT 1 FROM work_source_revisions WHERE import_key = ?", (key.value,)
        ).fetchone()
        if existing is not None:
            return
        self._persist_revision(revision)

    def _persist_revision(self, revision: WorkSourceRevision) -> None:
        self._connection.execute(
            "INSERT INTO work_source_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                revision.import_key.value,
                revision.repository.owner,
                revision.repository.name,
                revision.repository.repository_id,
                revision.repository.url,
                revision.number,
                revision.url,
                revision.title,
                revision.body,
                revision.source_revision.value,
                _timestamp(revision.updated_at),
                _timestamp(revision.observed_at),
                revision.body_sha256,
                revision.specification_digest,
                _encode_labels(revision.labels),
                _encode_dependencies(revision.dependencies),
                revision.issue_state,
                revision.author,
                revision.milestone,
            ),
        )

    def _provenance_from_row(self, row: sqlite3.Row) -> WorkOrderSourceProvenance:
        revision = self.revision(row["import_key"])
        return WorkOrderSourceProvenance(
            work_order_id=WorkOrderId(row["work_order_id"]),
            import_key=build_import_key(
                revision.repository, revision.number, revision.specification_digest
            ),
            revision=revision,
            imported_at=_as_datetime(row["imported_at"]),
            parent_import_key=(
                ImportKey(row["parent_import_key"])
                if row["parent_import_key"] is not None
                else None
            ),
            parent_work_order_id=(
                WorkOrderId(row["parent_work_order_id"])
                if row["parent_work_order_id"] is not None
                else None
            ),
        )


__all__ = ["SqliteWorkSource"]
