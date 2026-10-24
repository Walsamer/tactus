"""Durable, single-transaction child materialization for ``DECOMPOSE`` (#10).

This adapter is the persistence half of bounded child materialization. Every
effect of one application — child Work Orders, their Issue provenance, the
dependency rewrite and the materialization ledger entry — is written inside one
``BEGIN IMMEDIATE`` transaction. A fault at any failpoint rolls the whole
application back, so a partially materialized generation can never be observed
and therefore can never be executed.

The ledger makes re-applying the same validated decision converge on the
children it already created instead of producing a second generation.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ..application.decomposition import MaterializedGeneration
from ..domain.decomposition import ChildMaterializationPlan, plan_split
from ..domain.dependency import (
    DependencyChangeRecord,
    DependencyGraph,
    WorkOrderDependency,
)
from ..domain.records import TransitionAuthority
from ..domain.work_order import WorkOrder, WorkOrderId, WorkOrderState
from ..domain.work_source import WorkOrderSourceProvenance

#: Distinct migration version: an existing execution ledger is upgraded in place.
_DECOMPOSITION_MIGRATION_ID = "0004_bounded_child_materialization"

FaultHook = Callable[[str], None]


def _default_fault(_name: str) -> None:
    return None


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _parse(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError("stored timestamps must be timezone-aware")
    return parsed


def _encode_edges(edges: tuple[WorkOrderDependency, ...]) -> str:
    return ";".join(f"{edge.upstream.value}>{edge.downstream.value}" for edge in edges)


def _decode_edges(raw: str) -> tuple[WorkOrderDependency, ...]:
    if not raw:
        return ()
    decoded: list[WorkOrderDependency] = []
    for item in raw.split(";"):
        upstream, _, downstream = item.partition(">")
        decoded.append(
            WorkOrderDependency(
                upstream=WorkOrderId(upstream), downstream=WorkOrderId(downstream)
            )
        )
    return tuple(decoded)


class SqliteChildMaterialization:
    """Reference durable child-materialization store."""

    def __init__(self, database: str | Path, *, timeout: float = 5.0) -> None:
        self._connection = sqlite3.connect(
            str(database), timeout=timeout, isolation_level=None, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def close(self) -> None:
        self._connection.close()

    # -- schema -----------------------------------------------------------

    def _migrate(self) -> None:
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY)"
        )
        applied = self._connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (_DECOMPOSITION_MIGRATION_ID,),
        ).fetchone()
        if applied is not None:
            return
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS decomposition_children (
                work_order_id TEXT PRIMARY KEY,
                parent_work_order_id TEXT NOT NULL,
                materialization_key TEXT NOT NULL,
                title TEXT NOT NULL,
                scope TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('DRAFT','OPEN','ACTIVE','IMPLEMENTED','RETIRED'))
            );
            CREATE TABLE IF NOT EXISTS decomposition_child_dependencies (
                work_order_id TEXT NOT NULL,
                depends_on_id TEXT NOT NULL,
                PRIMARY KEY (work_order_id, depends_on_id)
            );
            CREATE TABLE IF NOT EXISTS decomposition_provenance (
                work_order_id TEXT PRIMARY KEY,
                parent_work_order_id TEXT NOT NULL,
                import_key TEXT NOT NULL,
                parent_import_key TEXT,
                revision_reference TEXT NOT NULL,
                imported_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decomposition_edges (
                upstream TEXT NOT NULL,
                downstream TEXT NOT NULL,
                PRIMARY KEY (upstream, downstream),
                CHECK (upstream <> downstream)
            );
            CREATE TABLE IF NOT EXISTS decomposition_applications (
                materialization_key TEXT PRIMARY KEY,
                parent_work_order_id TEXT NOT NULL,
                child_ids TEXT NOT NULL,
                removed_edges TEXT NOT NULL,
                added_edges TEXT NOT NULL,
                parent_state TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
            """
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
            (_DECOMPOSITION_MIGRATION_ID,),
        )

    # -- transaction ------------------------------------------------------

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    # -- durable graph ----------------------------------------------------

    def _graph(self) -> DependencyGraph:
        rows = self._connection.execute(
            "SELECT upstream, downstream FROM decomposition_edges"
        ).fetchall()
        return DependencyGraph(
            WorkOrderDependency(
                upstream=WorkOrderId(row["upstream"]),
                downstream=WorkOrderId(row["downstream"]),
            )
            for row in rows
        )

    def add_edges(self, edges: list[tuple[str, str]]) -> None:
        """Seed the durable dependency graph (bootstrap/test seam)."""

        with self._transaction():
            for upstream, downstream in edges:
                self._connection.execute(
                    "INSERT OR IGNORE INTO decomposition_edges(upstream, downstream) "
                    "VALUES (?,?)",
                    (upstream, downstream),
                )

    def edges(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (row["upstream"], row["downstream"])
            for row in self._connection.execute(
                "SELECT upstream, downstream FROM decomposition_edges "
                "ORDER BY upstream, downstream"
            ).fetchall()
        )

    def child_rows(self) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self._connection.execute(
                "SELECT * FROM decomposition_children ORDER BY work_order_id"
            ).fetchall()
        )

    def provenance_rows(self) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self._connection.execute(
                "SELECT * FROM decomposition_provenance ORDER BY work_order_id"
            ).fetchall()
        )

    def executable_children(self) -> tuple[sqlite3.Row, ...]:
        return tuple(
            row
            for row in self.child_rows()
            if row["state"] != WorkOrderState.DRAFT.value
        )

    # -- port --------------------------------------------------------------

    def lookup(self, key: str) -> MaterializedGeneration | None:
        row = self._connection.execute(
            "SELECT * FROM decomposition_applications WHERE materialization_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return MaterializedGeneration(
            materialization_key=row["materialization_key"],
            parent_work_order_id=WorkOrderId(row["parent_work_order_id"]),
            child_ids=tuple(WorkOrderId(item) for item in row["child_ids"].split(",")),
            dependency_change=DependencyChangeRecord(
                removed=_decode_edges(row["removed_edges"]),
                added=_decode_edges(row["added_edges"]),
                authority=TransitionAuthority.RECOVERY,
                reason="replayed from the durability ledger",
                occurred_at=_parse(row["applied_at"]),
            ),
            parent_state=WorkOrderState(row["parent_state"]),
            applied_at=_parse(row["applied_at"]),
        )

    def materialize(
        self,
        *,
        key: str,
        plan: ChildMaterializationPlan,
        parent: WorkOrder,
        parent_provenance: WorkOrderSourceProvenance | None,
        graph: DependencyGraph,
        authority: TransitionAuthority,
        reason: str,
        applied_at: datetime,
        fault: FaultHook | None = None,
    ) -> MaterializedGeneration:
        hook = fault if fault is not None else _default_fault

        with self._transaction():
            # The rewrite is derived from the durable graph when that graph owns
            # the parent's edges, so the persisted topology stays authoritative.
            durable_graph = self._graph()
            source_graph = (
                durable_graph if durable_graph.edges() or not graph.edges() else graph
            )
            removed, added = plan_split(source_graph, plan)

            for spec in plan.children:
                self._connection.execute(
                    """INSERT INTO decomposition_children
                       (work_order_id, parent_work_order_id, materialization_key,
                        title, scope, state)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        spec.work_order_id.value,
                        plan.parent_work_order_id.value,
                        key,
                        spec.title,
                        ",".join(spec.scope),
                        # DRAFT: never executable on creation. Each child must
                        # pass independent admission/readiness on its own merits.
                        WorkOrderState.DRAFT.value,
                    ),
                )
                for dependency in spec.depends_on:
                    self._connection.execute(
                        """INSERT INTO decomposition_child_dependencies
                           (work_order_id, depends_on_id) VALUES (?,?)""",
                        (spec.work_order_id.value, dependency.value),
                    )
            hook("after_children")

            if parent_provenance is not None:
                for spec in plan.children:
                    child = parent_provenance.as_child(spec.work_order_id)
                    self._connection.execute(
                        """INSERT INTO decomposition_provenance
                           (work_order_id, parent_work_order_id, import_key,
                            parent_import_key, revision_reference, imported_at)
                           VALUES (?,?,?,?,?,?)""",
                        (
                            child.work_order_id.value,
                            spec.work_order_id.value,
                            child.import_key.value,
                            child.parent_import_key.value
                            if child.parent_import_key is not None
                            else None,
                            child.revision.source_revision.value,
                            _timestamp(child.imported_at),
                        ),
                    )
            hook("after_provenance")

            for edge in removed:
                self._connection.execute(
                    "DELETE FROM decomposition_edges WHERE upstream=? AND downstream=?",
                    (edge.upstream.value, edge.downstream.value),
                )
            for edge in added:
                self._connection.execute(
                    "INSERT OR IGNORE INTO decomposition_edges(upstream, downstream) "
                    "VALUES (?,?)",
                    (edge.upstream.value, edge.downstream.value),
                )
            # The durable topology must satisfy the same invariant as the domain
            # graph; a cycle here aborts the transaction.
            self._graph()
            hook("after_edges")

            hook("before_parent_effect")
            self._connection.execute(
                """INSERT INTO decomposition_applications
                   (materialization_key, parent_work_order_id, child_ids,
                    removed_edges, added_edges, parent_state, applied_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    key,
                    plan.parent_work_order_id.value,
                    ",".join(child.value for child in plan.child_ids),
                    _encode_edges(removed),
                    _encode_edges(added),
                    WorkOrderState.RETIRED.value,
                    _timestamp(applied_at),
                ),
            )

        # The committed ledger row and the domain effect agree; the entity owns
        # the lifecycle transition itself.
        parent.retire(reason=reason, authority=authority, at=applied_at)

        return MaterializedGeneration(
            materialization_key=key,
            parent_work_order_id=plan.parent_work_order_id,
            child_ids=plan.child_ids,
            dependency_change=DependencyChangeRecord(
                removed=removed,
                added=added,
                authority=authority,
                reason=reason,
                occurred_at=applied_at,
            ),
            parent_state=parent.state,
            applied_at=applied_at,
        )


__all__ = ["SqliteChildMaterialization"]
