"""SQLite implementation of the durable human-intervention boundary.

This adapter owns only Tactus human-intervention records: typed requests, the
authenticated typed resolutions bound to the current source/domain revision and
the bounded grants they produce. It performs the same fail-closed validation as
the pure domain function, transactionally, so a restart or a concurrent submitter
cannot produce a second effect for one request.

It contains no approval-requirement policy and no grant-sufficiency verdict:
Ictus owns both. Revocation is a Tactus-owned audit fact, not an Ictus decision.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from tactus.domain._clock import utcnow
from tactus.domain.human_intervention import (
    HumanAction,
    HumanGrant,
    HumanInterventionPort,
    HumanInterventionRequest,
    HumanResolution,
    ImmutableRequestConflictError,
    ImmutableResolutionConflictError,
    InterventionReason,
    RequestAlreadyResolvedError,
    UnknownInterventionRequestError,
    UnknownResolutionError,
    validate_resolution,
)
from tactus.domain.admission import SourceRevision, WorkOrderRevision
from tactus.domain.work_order import WorkOrderId


_MIGRATION_ID = "0002_human_intervention"


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _as_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _encode_seq(values: tuple[str, ...] | tuple[HumanAction, ...]) -> str:
    return json.dumps([item.value if isinstance(item, HumanAction) else item for item in values])


def _decode_seq(value: str) -> tuple[str, ...]:
    return tuple(json.loads(value))


def _request_fingerprint(request: HumanInterventionRequest) -> tuple[object, ...]:
    return (
        str(request.work_order_id),
        request.reason.value,
        request.summary,
        request.requested_action,
        tuple(action.value for action in request.permitted_actions),
        tuple(request.scope),
        request.source_revision.value if request.source_revision else None,
        request.work_order_revision.value if request.work_order_revision else None,
        _timestamp(request.created_at),
        _timestamp(request.expires_at) if request.expires_at else None,
    )


def _resolution_fingerprint(resolution: HumanResolution) -> tuple[object, ...]:
    return (
        resolution.intervention_id,
        str(resolution.work_order_id),
        resolution.actor,
        resolution.action.value,
        tuple(resolution.scope),
        resolution.source_revision.value if resolution.source_revision else None,
        resolution.work_order_revision.value if resolution.work_order_revision else None,
        int(resolution.route_change),
        tuple(resolution.evidence),
        _timestamp(resolution.resolved_at),
        _timestamp(resolution.expires_at) if resolution.expires_at else None,
    )


class SqliteHumanIntervention(HumanInterventionPort):
    """Transactional SQLite store for requests, resolutions and grants.

    Each instance owns its connection. Create an independent instance per
    process; ``BEGIN IMMEDIATE`` plus a partial unique index make "exactly one
    active resolution per request" explicit rather than relying on Python locks.
    """

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
        """Apply the idempotent human-intervention schema migration."""

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
            CREATE TABLE IF NOT EXISTS human_intervention_requests (
                intervention_id TEXT PRIMARY KEY,
                work_order_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                summary TEXT NOT NULL,
                requested_action TEXT,
                permitted_actions TEXT NOT NULL,
                scope TEXT NOT NULL,
                source_revision TEXT,
                work_order_revision INTEGER,
                created_at TEXT NOT NULL,
                expires_at TEXT
            );
            CREATE TABLE IF NOT EXISTS human_resolutions (
                resolution_id TEXT PRIMARY KEY,
                intervention_id TEXT NOT NULL
                    REFERENCES human_intervention_requests(intervention_id),
                work_order_id TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                scope TEXT NOT NULL,
                route_change INTEGER NOT NULL DEFAULT 0,
                source_revision TEXT,
                work_order_revision INTEGER,
                resolved_at TEXT NOT NULL,
                expires_at TEXT,
                evidence TEXT NOT NULL,
                revoked_at TEXT,
                revocation_reason TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_resolution_per_request
                ON human_resolutions(intervention_id) WHERE revoked_at IS NULL;
            CREATE TABLE IF NOT EXISTS human_grants (
                grant_id TEXT PRIMARY KEY,
                resolution_id TEXT NOT NULL UNIQUE
                    REFERENCES human_resolutions(resolution_id),
                intervention_id TEXT NOT NULL,
                work_order_id TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                scope TEXT NOT NULL,
                source_revision TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                expires_at TEXT,
                revoked_at TEXT
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

    # -- requests ----------------------------------------------------------

    def record_request(self, request: HumanInterventionRequest) -> HumanInterventionRequest:
        if not isinstance(request, HumanInterventionRequest):
            raise UnknownInterventionRequestError("request must be a HumanInterventionRequest")
        with self._transaction():
            return self._record_request_in(self._connection, request)

    def record_request_in(
        self, connection: sqlite3.Connection, request: HumanInterventionRequest
    ) -> HumanInterventionRequest:
        """Record a request on a caller-supplied transaction.

        The execution cycle persists the intervention produced by an
        ``ESCALATE`` decision in the *same* transaction as the guarded Work
        Order projection and the decision ledger row (the same primitive style
        as ``SqliteExecutionAdmission.apply_transition_in``). A restart can
        therefore never observe a blocked Work Order without the typed request
        that justifies it, and a rolled-back decision leaves no orphan request.

        The store must share the caller's SQLite database file; the caller owns
        the transaction.
        """
        if not isinstance(request, HumanInterventionRequest):
            raise UnknownInterventionRequestError("request must be a HumanInterventionRequest")
        return self._record_request_in(connection, request)

    def _record_request_in(
        self, connection: sqlite3.Connection, request: HumanInterventionRequest
    ) -> HumanInterventionRequest:
        existing = connection.execute(
            "SELECT * FROM human_intervention_requests WHERE intervention_id = ?",
            (request.intervention_id,),
        ).fetchone()
        if existing is not None:
            persisted = _request_from_row(existing)
            if _request_fingerprint(persisted) != _request_fingerprint(request):
                raise ImmutableRequestConflictError(
                    f"request {request.intervention_id!r} replayed with different content"
                )
            return persisted
        connection.execute(
            """INSERT INTO human_intervention_requests VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                request.intervention_id,
                str(request.work_order_id),
                request.reason.value,
                request.summary,
                request.requested_action,
                _encode_seq(request.permitted_actions),
                _encode_seq(request.scope),
                request.source_revision.value if request.source_revision else None,
                request.work_order_revision.value if request.work_order_revision else None,
                _timestamp(request.created_at),
                _timestamp(request.expires_at) if request.expires_at else None,
            ),
        )
        return request

    def request(self, intervention_id: str) -> HumanInterventionRequest:
        row = self._connection.execute(
            "SELECT * FROM human_intervention_requests WHERE intervention_id = ?",
            (intervention_id,),
        ).fetchone()
        if row is None:
            raise UnknownInterventionRequestError(
                f"request {intervention_id!r} is not recorded"
            )
        return _request_from_row(row)

    # -- resolutions and grants -------------------------------------------

    def resolve(
        self,
        resolution: HumanResolution,
        *,
        current_source_revision: SourceRevision,
        current_work_order_revision: WorkOrderRevision,
    ) -> tuple[HumanResolution, HumanGrant]:
        if not isinstance(resolution, HumanResolution):
            raise UnknownResolutionError("resolution must be a HumanResolution")
        with self._transaction():
            request_row = self._connection.execute(
                "SELECT * FROM human_intervention_requests WHERE intervention_id = ?",
                (resolution.intervention_id,),
            ).fetchone()
            if request_row is None:
                raise UnknownInterventionRequestError(
                    f"request {resolution.intervention_id!r} is not recorded"
                )
            request = _request_from_row(request_row)

            existing = self._connection.execute(
                "SELECT * FROM human_resolutions WHERE resolution_id = ?",
                (resolution.resolution_id,),
            ).fetchone()
            if existing is not None:
                persisted = _resolution_from_row(existing)
                if _resolution_fingerprint(persisted) != _resolution_fingerprint(resolution):
                    raise ImmutableResolutionConflictError(
                        f"resolution {resolution.resolution_id!r} replayed with different content"
                    )
                return persisted, self._grant_for_resolution(persisted.resolution_id)

            active = self._connection.execute(
                "SELECT 1 FROM human_resolutions WHERE intervention_id = ? AND revoked_at IS NULL",
                (resolution.intervention_id,),
            ).fetchone()
            if active is not None:
                raise RequestAlreadyResolvedError(
                    f"request {resolution.intervention_id!r} already has an active resolution"
                )

            validate_resolution(
                request,
                resolution,
                current_source_revision=current_source_revision,
                current_work_order_revision=current_work_order_revision,
            )

            grant = HumanGrant(
                grant_id=uuid.uuid4().hex,
                resolution_id=resolution.resolution_id,
                intervention_id=resolution.intervention_id,
                work_order_id=resolution.work_order_id,
                actor=resolution.actor,
                action=resolution.action,
                scope=resolution.scope,
                source_revision=current_source_revision,
                issued_at=resolution.resolved_at,
                expires_at=resolution.expires_at,
            )
            self._connection.execute(
                """INSERT INTO human_resolutions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    resolution.resolution_id,
                    resolution.intervention_id,
                    str(resolution.work_order_id),
                    resolution.actor,
                    resolution.action.value,
                    _encode_seq(resolution.scope),
                    int(resolution.route_change),
                    resolution.source_revision.value if resolution.source_revision else None,
                    resolution.work_order_revision.value
                    if resolution.work_order_revision
                    else None,
                    _timestamp(resolution.resolved_at),
                    _timestamp(resolution.expires_at) if resolution.expires_at else None,
                    _encode_seq(resolution.evidence),
                    None,
                    None,
                ),
            )
            self._connection.execute(
                """INSERT INTO human_grants VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    grant.grant_id,
                    grant.resolution_id,
                    grant.intervention_id,
                    str(grant.work_order_id),
                    grant.actor,
                    grant.action.value,
                    _encode_seq(grant.scope),
                    grant.source_revision.value,
                    _timestamp(grant.issued_at),
                    _timestamp(grant.expires_at) if grant.expires_at else None,
                    None,
                ),
            )
        return resolution, grant

    def resolution(self, resolution_id: str) -> HumanResolution:
        row = self._connection.execute(
            "SELECT * FROM human_resolutions WHERE resolution_id = ?", (resolution_id,)
        ).fetchone()
        if row is None:
            raise UnknownResolutionError(f"resolution {resolution_id!r} is not recorded")
        return _resolution_from_row(row)

    def resolutions_for(self, intervention_id: str) -> tuple[HumanResolution, ...]:
        rows = self._connection.execute(
            "SELECT * FROM human_resolutions WHERE intervention_id = ? ORDER BY resolved_at, rowid",
            (intervention_id,),
        ).fetchall()
        return tuple(_resolution_from_row(row) for row in rows)

    def grants_for(
        self, work_order_id: WorkOrderId | str, *, include_revoked: bool = False
    ) -> tuple[HumanGrant, ...]:
        query = "SELECT * FROM human_grants WHERE work_order_id = ?"
        if not include_revoked:
            query += " AND revoked_at IS NULL"
        query += " ORDER BY issued_at, rowid"
        rows = self._connection.execute(query, (str(work_order_id),)).fetchall()
        return tuple(_grant_from_row(row) for row in rows)

    def revoke(
        self, resolution_id: str, *, at: datetime | None = None, reason: str = ""
    ) -> tuple[HumanResolution, HumanGrant]:
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM human_resolutions WHERE resolution_id = ?", (resolution_id,)
            ).fetchone()
            if row is None:
                raise UnknownResolutionError(f"resolution {resolution_id!r} is not recorded")
            resolution = _resolution_from_row(row)
            if resolution.is_revoked:
                return resolution, self._grant_for_resolution(resolution_id)
            revoked_at = at if at is not None else utcnow()
            self._connection.execute(
                "UPDATE human_resolutions SET revoked_at = ?, revocation_reason = ? WHERE resolution_id = ?",
                (_timestamp(revoked_at), reason or None, resolution_id),
            )
            self._connection.execute(
                "UPDATE human_grants SET revoked_at = ? WHERE resolution_id = ?",
                (_timestamp(revoked_at), resolution_id),
            )
        return self.resolution(resolution_id), self._grant_for_resolution(resolution_id)

    # -- helpers -----------------------------------------------------------

    def _grant_for_resolution(self, resolution_id: str) -> HumanGrant:
        row = self._connection.execute(
            "SELECT * FROM human_grants WHERE resolution_id = ?", (resolution_id,)
        ).fetchone()
        if row is None:
            raise UnknownResolutionError(
                f"resolution {resolution_id!r} has no recorded grant"
            )
        return _grant_from_row(row)


def _request_from_row(row: sqlite3.Row) -> HumanInterventionRequest:
    return HumanInterventionRequest(
        intervention_id=row["intervention_id"],
        work_order_id=WorkOrderId(row["work_order_id"]),
        reason=InterventionReason(row["reason"]),
        summary=row["summary"],
        requested_action=row["requested_action"],
        permitted_actions=tuple(HumanAction(item) for item in _decode_seq(row["permitted_actions"])),
        scope=_decode_seq(row["scope"]),
        source_revision=SourceRevision(row["source_revision"])
        if row["source_revision"] is not None
        else None,
        work_order_revision=WorkOrderRevision(row["work_order_revision"])
        if row["work_order_revision"] is not None
        else None,
        created_at=_as_datetime(row["created_at"]),
        expires_at=_as_datetime(row["expires_at"]) if row["expires_at"] is not None else None,
    )


def _resolution_from_row(row: sqlite3.Row) -> HumanResolution:
    return HumanResolution(
        resolution_id=row["resolution_id"],
        intervention_id=row["intervention_id"],
        work_order_id=WorkOrderId(row["work_order_id"]),
        actor=row["actor"],
        action=HumanAction(row["action"]),
        resolved_at=_as_datetime(row["resolved_at"]),
        scope=_decode_seq(row["scope"]),
        source_revision=SourceRevision(row["source_revision"])
        if row["source_revision"] is not None
        else None,
        work_order_revision=WorkOrderRevision(row["work_order_revision"])
        if row["work_order_revision"] is not None
        else None,
        route_change=bool(row["route_change"]),
        evidence=_decode_seq(row["evidence"]),
        expires_at=_as_datetime(row["expires_at"]) if row["expires_at"] is not None else None,
        revoked_at=_as_datetime(row["revoked_at"]) if row["revoked_at"] is not None else None,
        revocation_reason=row["revocation_reason"],
    )


def _grant_from_row(row: sqlite3.Row) -> HumanGrant:
    return HumanGrant(
        grant_id=row["grant_id"],
        resolution_id=row["resolution_id"],
        intervention_id=row["intervention_id"],
        work_order_id=WorkOrderId(row["work_order_id"]),
        actor=row["actor"],
        action=HumanAction(row["action"]),
        scope=_decode_seq(row["scope"]),
        source_revision=SourceRevision(row["source_revision"]),
        issued_at=_as_datetime(row["issued_at"]),
        expires_at=_as_datetime(row["expires_at"]) if row["expires_at"] is not None else None,
        revoked_at=_as_datetime(row["revoked_at"]) if row["revoked_at"] is not None else None,
    )


__all__ = ["SqliteHumanIntervention"]
