"""SQLite implementation of the durable execution-admission boundary.

This adapter owns only Tactus domain records.  In particular, its outbox is a
delivery ledger, not a worker queue: it has no capacity, ordering, route, or
retry policy.  The transaction boundaries below intentionally make a crash
before commit indistinguishable from no admission at all.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from tactus.domain.admission import (
    ClaimToken,
    ClaimUnavailableError,
    DagsterRunId,
    DomainClaim,
    DurableAdmissionRequest,
    DurableAttempt,
    DurableExecutionAdmissionPort,
    ExecutionAttemptId,
    ExecutionIntentId,
    FencingError,
    ImmutableRecordConflictError,
    IneligibleWorkOrderError,
    ResultApplicationError,
    ResultRecord,
    SourceRevision,
    StaleWorkOrderRevisionError,
    SubmissionId,
    SubmissionRecord,
    UnknownExecutionAttemptError,
    WorkOrderId,
    WorkOrderRevision,
)
from tactus.domain._clock import utcnow
from tactus.domain.work_order import OpenStatus, WorkOrderState


_MIGRATION_ID = "0001_execution_admission"


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _as_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


class SqliteExecutionAdmission(DurableExecutionAdmissionPort):
    """Transactional local-domain persistence adapter.

    Each instance owns its connection.  Create independent instances for
    independent processes; SQLite's ``BEGIN IMMEDIATE`` and constraints make
    claim/accept serialization explicit rather than relying on Python locks.
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
        """Apply the initial idempotent schema migration."""

        # ``executescript`` controls its own transaction in sqlite3, so it must
        # not be nested in ``_transaction``.  DDL is idempotent and the version
        # row prevents a second adapter opening the same file from changing it.
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
                CREATE TABLE IF NOT EXISTS work_orders (
                    work_order_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL CHECK (revision >= 0),
                    source_revision TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('DRAFT','OPEN','ACTIVE','IMPLEMENTED','RETIRED')),
                    readiness TEXT CHECK (readiness IN ('UNKNOWN','READY','BLOCKED') OR readiness IS NULL),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS domain_claims (
                    work_order_id TEXT PRIMARY KEY REFERENCES work_orders(work_order_id),
                    claim_token TEXT NOT NULL,
                    expected_revision INTEGER NOT NULL,
                    fence INTEGER NOT NULL CHECK (fence > 0),
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    work_order_id TEXT NOT NULL REFERENCES work_orders(work_order_id),
                    intent_id TEXT NOT NULL UNIQUE,
                    source_revision TEXT NOT NULL,
                    fence INTEGER NOT NULL CHECK (fence > 0),
                    accepted_at TEXT NOT NULL,
                    closed_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_open_attempt_per_work_order
                    ON execution_attempts(work_order_id) WHERE closed_at IS NULL;
                CREATE TABLE IF NOT EXISTS submissions (
                    submission_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL UNIQUE REFERENCES execution_attempts(attempt_id),
                    intent_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    dagster_run_id TEXT UNIQUE
                );
                CREATE TABLE IF NOT EXISTS submission_outbox (
                    submission_id TEXT PRIMARY KEY REFERENCES submissions(submission_id),
                    dispatched_at TEXT
                );
                CREATE TABLE IF NOT EXISTS result_inbox (
                    result_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL REFERENCES execution_attempts(attempt_id),
                    payload_digest TEXT NOT NULL,
                    applied_at TEXT NOT NULL
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

    # WorkOrder persistence is deliberately small: richer source provenance and
    # blockers are owned by their respective application slices.
    def register_work_order(
        self,
        work_order_id: WorkOrderId | str,
        *,
        revision: WorkOrderRevision,
        source_revision: SourceRevision,
        state: WorkOrderState = WorkOrderState.OPEN,
        readiness: OpenStatus | None = OpenStatus.READY,
        at: datetime | None = None,
    ) -> None:
        identifier = _work_order_id(work_order_id)
        if state is WorkOrderState.OPEN and readiness is None:
            raise ValueError("an OPEN WorkOrder requires a readiness value")
        if state is not WorkOrderState.OPEN and readiness is not None:
            raise ValueError("readiness is meaningful only while OPEN")
        now = at if at is not None else utcnow()
        with self._transaction():
            try:
                self._connection.execute(
                    """INSERT INTO work_orders
                    (work_order_id, revision, source_revision, state, readiness, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        str(identifier), revision.value, str(source_revision), state.value,
                        readiness.value if readiness is not None else None, _timestamp(now),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ImmutableRecordConflictError(f"WorkOrder {identifier} is already registered") from exc

    def acquire_claim(
        self,
        work_order_id: WorkOrderId | str,
        *,
        expected_revision: WorkOrderRevision,
        claim_token: ClaimToken,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> DomainClaim:
        identifier = _work_order_id(work_order_id)
        current_time = now if now is not None else utcnow()
        if expires_at <= current_time:
            raise ValueError("claim expiry must be in the future")
        with self._transaction():
            work_order = self._work_order(identifier)
            if work_order["revision"] != expected_revision.value:
                raise StaleWorkOrderRevisionError(f"WorkOrder {identifier} revision has changed")
            row = self._connection.execute(
                "SELECT * FROM domain_claims WHERE work_order_id = ?", (str(identifier),)
            ).fetchone()
            if row is not None and _as_datetime(row["expires_at"]) > current_time:
                if row["claim_token"] == claim_token.value and row["expected_revision"] == expected_revision.value:
                    return _claim_from_row(identifier, row)
                raise ClaimUnavailableError(f"WorkOrder {identifier} has an unexpired claim")
            fence = 1 if row is None else row["fence"] + 1
            self._connection.execute(
                """INSERT INTO domain_claims(work_order_id, claim_token, expected_revision, fence, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(work_order_id) DO UPDATE SET
                  claim_token=excluded.claim_token,
                  expected_revision=excluded.expected_revision,
                  fence=excluded.fence,
                  expires_at=excluded.expires_at""",
                (str(identifier), claim_token.value, expected_revision.value, fence, _timestamp(expires_at)),
            )
            return DomainClaim(identifier, expected_revision, claim_token, fence, expires_at)

    def accept_durable(
        self,
        claim: DomainClaim,
        request: DurableAdmissionRequest,
        *,
        at: datetime | None = None,
    ) -> tuple[DurableAttempt, SubmissionRecord]:
        if request.work_order_id != claim.work_order_id:
            raise FencingError("claim and request target different WorkOrders")
        if request.expected_revision != claim.expected_revision or request.claim_token != claim.token:
            raise FencingError("request does not carry the persisted claim identity")
        accepted_at = at if at is not None else utcnow()
        with self._transaction():
            persisted_claim = self._connection.execute(
                "SELECT * FROM domain_claims WHERE work_order_id = ?", (str(claim.work_order_id),)
            ).fetchone()
            if (
                persisted_claim is None
                or persisted_claim["claim_token"] != claim.token.value
                or persisted_claim["fence"] != claim.fence
                or persisted_claim["expected_revision"] != claim.expected_revision.value
                or _as_datetime(persisted_claim["expires_at"]) <= accepted_at
            ):
                raise FencingError("claim is expired or has been fenced by a newer holder")
            # A retry after a lost local acknowledgement returns the original
            # committed identity.  Changed intent content under that identity
            # is a hard failure, never a second submission.
            existing = self._connection.execute(
                """SELECT a.*, s.submission_id, s.intent_digest, s.created_at, s.dagster_run_id
                FROM execution_attempts a JOIN submissions s ON s.attempt_id = a.attempt_id
                WHERE a.work_order_id = ? AND a.closed_at IS NULL""",
                (str(claim.work_order_id),),
            ).fetchone()
            if existing is not None:
                if existing["intent_id"] == request.intent_id.value and existing["intent_digest"] == request.intent_digest:
                    return _attempt_from_row(existing), _submission_from_row(existing)
                raise ImmutableRecordConflictError("an unclosed semantic attempt already exists")
            order = self._work_order(claim.work_order_id)
            if order["revision"] != request.expected_revision.value or order["source_revision"] != request.source_revision.value:
                raise StaleWorkOrderRevisionError("WorkOrder or source revision has changed")
            if order["state"] != WorkOrderState.OPEN.value or order["readiness"] != OpenStatus.READY.value:
                raise IneligibleWorkOrderError("durable WorkOrder is not OPEN + READY")

            attempt_id = ExecutionAttemptId(uuid.uuid4().hex)
            submission_id = SubmissionId(_submission_identity(attempt_id, request.intent_id))
            try:
                self._connection.execute(
                    """INSERT INTO execution_attempts
                    (attempt_id, work_order_id, intent_id, source_revision, fence, accepted_at, closed_at)
                    VALUES (?, ?, ?, ?, ?, ?, NULL)""",
                    (str(attempt_id), str(claim.work_order_id), str(request.intent_id), str(request.source_revision), claim.fence, _timestamp(accepted_at)),
                )
                self._connection.execute(
                    "INSERT INTO submissions(submission_id, attempt_id, intent_digest, created_at) VALUES (?, ?, ?, ?)",
                    (str(submission_id), str(attempt_id), request.intent_digest, _timestamp(accepted_at)),
                )
                self._connection.execute(
                    "INSERT INTO submission_outbox(submission_id) VALUES (?)", (str(submission_id),)
                )
                updated = self._connection.execute(
                    """UPDATE work_orders SET state = ?, readiness = NULL, revision = revision + 1, updated_at = ?
                    WHERE work_order_id = ? AND revision = ? AND state = ? AND readiness = ?""",
                    (WorkOrderState.ACTIVE.value, _timestamp(accepted_at), str(claim.work_order_id), request.expected_revision.value, WorkOrderState.OPEN.value, OpenStatus.READY.value),
                )
                if updated.rowcount != 1:  # defensive: all checks and effects roll back together
                    raise StaleWorkOrderRevisionError("WorkOrder changed during durable admission")
            except sqlite3.IntegrityError as exc:
                raise ImmutableRecordConflictError("duplicate immutable attempt or submission identity") from exc
            return (
                DurableAttempt(attempt_id, claim.work_order_id, request.intent_id, request.source_revision, claim.fence, accepted_at),
                SubmissionRecord(submission_id, attempt_id, request.intent_digest, accepted_at),
            )

    def attach_receipt(
        self,
        submission_id: SubmissionId | str,
        dagster_run_id: DagsterRunId | str,
    ) -> SubmissionRecord:
        identifier = _submission_id(submission_id)
        run_id = _run_id(dagster_run_id)
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM submissions WHERE submission_id = ?", (str(identifier),)
            ).fetchone()
            if row is None:
                raise UnknownExecutionAttemptError(f"no submission {identifier}")
            if row["dagster_run_id"] is not None:
                if row["dagster_run_id"] == run_id.value:
                    return _submission_from_row(row)
                raise ImmutableRecordConflictError("submission already has a different Dagster receipt")
            try:
                self._connection.execute(
                    "UPDATE submissions SET dagster_run_id = ? WHERE submission_id = ?",
                    (run_id.value, str(identifier)),
                )
            except sqlite3.IntegrityError as exc:
                raise ImmutableRecordConflictError("Dagster run is already attached to another submission") from exc
            return SubmissionRecord(identifier, ExecutionAttemptId(row["attempt_id"]), row["intent_digest"], _as_datetime(row["created_at"]), run_id)

    def pending_submissions(self) -> tuple[SubmissionRecord, ...]:
        """Return every unreceipted identity for send *or reconciliation*.

        A previous send timestamp never removes an item from recovery.  A crash
        after send/before acknowledgement must query or resend this exact
        identity, never synthesize a replacement run.
        """

        rows = self._connection.execute(
            """SELECT s.* FROM submissions s JOIN submission_outbox o ON o.submission_id = s.submission_id
            WHERE s.dagster_run_id IS NULL ORDER BY s.created_at, s.submission_id"""
        ).fetchall()
        return tuple(_submission_from_row(row) for row in rows)

    def mark_dispatched(self, submission_id: SubmissionId | str, *, at: datetime | None = None) -> None:
        identifier = _submission_id(submission_id)
        sent_at = at if at is not None else utcnow()
        with self._transaction():
            updated = self._connection.execute(
                "UPDATE submission_outbox SET dispatched_at = COALESCE(dispatched_at, ?) WHERE submission_id = ?",
                (_timestamp(sent_at), str(identifier)),
            )
            if updated.rowcount != 1:
                raise UnknownExecutionAttemptError(f"no submission outbox record {identifier}")

    def apply_result(
        self,
        result: ResultRecord,
        *,
        apply_domain_effect: Callable[[sqlite3.Connection], None] | None = None,
        at: datetime | None = None,
    ) -> bool:
        """Apply a deduplicated result and close its attempt in one transaction.

        ``apply_domain_effect`` receives this transaction's connection, so a
        future result/recovery application can write its domain effects in the
        same atomic commit.  ``True`` means the effect ran; ``False`` is an
        identical replay.  A repeated key with a changed payload never runs it.
        """

        applied_at = at if at is not None else utcnow()
        with self._transaction():
            previous = self._connection.execute(
                "SELECT * FROM result_inbox WHERE result_id = ?", (result.result_id,)
            ).fetchone()
            if previous is not None:
                if previous["attempt_id"] == result.attempt_id.value and previous["payload_digest"] == result.payload_digest:
                    return False
                raise ImmutableRecordConflictError("result identity was replayed with a changed payload")
            attempt = self._connection.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id = ?", (result.attempt_id.value,)
            ).fetchone()
            if attempt is None:
                raise ResultApplicationError("result references an unknown execution attempt")
            if attempt["closed_at"] is not None:
                raise ResultApplicationError("result references an already closed execution attempt")
            if apply_domain_effect is not None:
                apply_domain_effect(self._connection)
            self._connection.execute(
                "UPDATE execution_attempts SET closed_at = ? WHERE attempt_id = ?",
                (_timestamp(applied_at), result.attempt_id.value),
            )
            self._connection.execute(
                "INSERT INTO result_inbox(result_id, attempt_id, payload_digest, applied_at) VALUES (?, ?, ?, ?)",
                (result.result_id, result.attempt_id.value, result.payload_digest, _timestamp(applied_at)),
            )
            return True

    def attempt(self, attempt_id: ExecutionAttemptId | str) -> DurableAttempt:
        identifier = _attempt_id(attempt_id)
        row = self._connection.execute(
            "SELECT * FROM execution_attempts WHERE attempt_id = ?", (str(identifier),)
        ).fetchone()
        if row is None:
            raise UnknownExecutionAttemptError(f"no execution attempt {identifier}")
        return _attempt_from_row(row)

    def submission(self, submission_id: SubmissionId | str) -> SubmissionRecord:
        identifier = _submission_id(submission_id)
        row = self._connection.execute(
            "SELECT * FROM submissions WHERE submission_id = ?", (str(identifier),)
        ).fetchone()
        if row is None:
            raise UnknownExecutionAttemptError(f"no submission {identifier}")
        return _submission_from_row(row)

    def work_order_snapshot(self, work_order_id: WorkOrderId | str) -> tuple[WorkOrderRevision, WorkOrderState, OpenStatus | None]:
        row = self._work_order(_work_order_id(work_order_id))
        return WorkOrderRevision(row["revision"]), WorkOrderState(row["state"]), (OpenStatus(row["readiness"]) if row["readiness"] is not None else None)

    def _work_order(self, work_order_id: WorkOrderId) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM work_orders WHERE work_order_id = ?", (str(work_order_id),)
        ).fetchone()
        if row is None:
            raise UnknownExecutionAttemptError(f"no persisted WorkOrder {work_order_id}")
        return row


def _submission_identity(attempt_id: ExecutionAttemptId, intent_id: ExecutionIntentId) -> str:
    # The random semantic attempt id makes this an immutable identity while the
    # durable row lets a retry recover it rather than recomputing a replacement.
    return hashlib.sha256(f"{attempt_id.value}\0{intent_id.value}".encode()).hexdigest()


def _claim_from_row(work_order_id: WorkOrderId, row: sqlite3.Row) -> DomainClaim:
    return DomainClaim(work_order_id, WorkOrderRevision(row["expected_revision"]), ClaimToken(row["claim_token"]), row["fence"], _as_datetime(row["expires_at"]))


def _attempt_from_row(row: sqlite3.Row) -> DurableAttempt:
    return DurableAttempt(ExecutionAttemptId(row["attempt_id"]), WorkOrderId(row["work_order_id"]), ExecutionIntentId(row["intent_id"]), SourceRevision(row["source_revision"]), row["fence"], _as_datetime(row["accepted_at"]), _as_datetime(row["closed_at"]) if row["closed_at"] else None)


def _submission_from_row(row: sqlite3.Row) -> SubmissionRecord:
    return SubmissionRecord(SubmissionId(row["submission_id"]), ExecutionAttemptId(row["attempt_id"]), row["intent_digest"], _as_datetime(row["created_at"]), DagsterRunId(row["dagster_run_id"]) if row["dagster_run_id"] else None)


def _work_order_id(value: WorkOrderId | str) -> WorkOrderId:
    return value if isinstance(value, WorkOrderId) else WorkOrderId(value)


def _attempt_id(value: ExecutionAttemptId | str) -> ExecutionAttemptId:
    return value if isinstance(value, ExecutionAttemptId) else ExecutionAttemptId(value)


def _submission_id(value: SubmissionId | str) -> SubmissionId:
    return value if isinstance(value, SubmissionId) else SubmissionId(value)


def _run_id(value: DagsterRunId | str) -> DagsterRunId:
    return value if isinstance(value, DagsterRunId) else DagsterRunId(value)
