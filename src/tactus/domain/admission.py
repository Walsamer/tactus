"""Domain execution-admission boundary and execution-attempt correlation.

This module owns the narrow Tactus boundary that admits an execution attempt for
an ``OPEN + READY`` Work Order. It answers only domain/authorization questions:

* is the Work Order still domain-eligible (``OPEN + READY``)?
* are its domain dependencies satisfied?
* is an Ictus-validated execution intent present?
* can the authoritative execution-attempt correlation be created atomically?

It deliberately does **not** schedule execution. There is no run queue, worker
selection, slot/concurrency accounting, run ordering, schedule/sensor, retry
policy or Dagster run/step state here — those are Dagster's. :class:`DagsterRunId`
is an opaque correlation handle owned and interpreted only by Dagster; Tactus
stores it but derives no execution semantics from it.

Ownership:

* Tactus owns WorkOrder/domain state and the acceptance decision.
* Ictus owns the validated ``ExecutionIntent`` referenced by an
  :class:`ExecutionRequest`. This boundary consumes that validated reference and
  embeds no routing/ranking policy.
* Dagster owns temporal execution, run identity and all run/step state.

Atomic invariant::

    OPEN + READY
        -> create authoritative execution-attempt correlation
        -> ACTIVE

Duplicate execution attempts (a second active attempt for the same Work Order,
or a reused execution-intent id / Dagster run id) are rejected.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Protocol, runtime_checkable

from ._clock import utcnow
from .records import TransitionAuthority
from .work_order import OpenStatus, WorkOrder, WorkOrderId, WorkOrderState


def _require_non_empty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


# -- value objects --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExecutionIntentId:
    """Identity of an Ictus-validated execution intent.

    Tactus never evaluates the intent's routing/ranking policy; it only records
    the validated reference. Ictus owns the intent's content.
    """

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "value", _require_non_empty(self.value, "ExecutionIntentId")
        )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class DagsterRunId:
    """Opaque correlation handle for a Dagster-owned execution run.

    Tactus stores this as an opaque reference only. It must not validate a
    format, parse step state, or otherwise embed Dagster execution mechanics.
    """

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _require_non_empty(self.value, "DagsterRunId"))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ExecutionAttemptId:
    """Identity of one authoritative Tactus execution attempt."""

    value: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "value", _require_non_empty(self.value, "ExecutionAttemptId")
        )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ClaimToken:
    """Opaque identity supplied by the process coordinating an admission.

    A token is not authorization to execute.  It is paired with a durable,
    monotonically increasing fence by the persistence adapter and only proves
    that this caller owns a short-lived coordination claim.
    """

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _require_non_empty(self.value, "ClaimToken"))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class WorkOrderRevision:
    """Optimistic revision of Tactus-owned WorkOrder state."""

    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 0:
            raise ValueError("WorkOrderRevision must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class SourceRevision:
    """Immutable source revision to which an accepted intent is bound."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _require_non_empty(self.value, "SourceRevision"))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class SubmissionId:
    """Stable identity delivered to the execution bridge exactly as recorded."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _require_non_empty(self.value, "SubmissionId"))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ExecutionAttempt:
    """Authoritative correlation for one accepted execution attempt.

    Links ``WorkOrderId`` -> ``ExecutionIntentId`` -> ``DagsterRunId``. It
    intentionally carries **no** Dagster run/step status, queue position, worker
    or retry state: that state stays Dagster-owned and is never mirrored here.
    """

    attempt_id: ExecutionAttemptId
    work_order_id: WorkOrderId
    intent_id: ExecutionIntentId
    dagster_run_id: DagsterRunId
    accepted_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """An Ictus-validated request to execute one Work Order.

    The legacy in-memory seam includes a Dagster run id.  Durable admission uses
    :class:`DurableAdmissionRequest` instead: it commits a stable submission
    before dispatch and attaches the opaque run id only after a bridge receipt.
    The caller must obtain validated intent evidence through the Ictus boundary;
    constructing this value is not proof of authorization.
    """

    work_order_id: WorkOrderId
    intent_id: ExecutionIntentId
    dagster_run_id: DagsterRunId


@dataclass(frozen=True, slots=True)
class AdmissionFacts:
    """Domain facts that gate execution admission.

    Each fact is produced by its owning subsystem (for example the readiness
    engine for dependency satisfaction); the admission boundary consumes them
    and does not compute policy. Defaults represent "no blocker known".
    """

    dependencies_satisfied: bool = True
    capabilities_present: bool = True
    system_paused: bool = False
    budget_available: bool = True


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """Outcome of a domain admission/eligibility evaluation."""

    work_order_id: WorkOrderId
    eligible: bool
    reason: str


# -- errors ---------------------------------------------------------------


class AdmissionError(ValueError):
    """Base class for invalid execution-admission operations."""


class ExecutionRequestMismatchError(AdmissionError):
    """The request targets a different Work Order than the one being admitted."""


class IneligibleWorkOrderError(AdmissionError):
    """The Work Order is not domain-eligible for execution admission."""


class DuplicateExecutionAttemptError(AdmissionError):
    """An execution attempt already exists for this Work Order/intent/run."""


class UnknownExecutionAttemptError(AdmissionError):
    """A correlation lookup referenced an attempt that is not recorded."""


class ClaimUnavailableError(AdmissionError):
    """Another unexpired coordinator currently owns the WorkOrder claim."""


class FencingError(AdmissionError):
    """A stale, expired, or superseded claim attempted a durable write."""


class StaleWorkOrderRevisionError(AdmissionError):
    """The claimed WorkOrder/source revision no longer matches durable state."""


class ImmutableRecordConflictError(AdmissionError):
    """A stable record identity was replayed with different immutable content."""


class ResultApplicationError(AdmissionError):
    """A result cannot be applied to the referenced durable attempt."""


# -- durable admission contracts -----------------------------------------


@dataclass(frozen=True, slots=True)
class DomainClaim:
    """A persisted, fenced coordination claim for one WorkOrder revision."""

    work_order_id: WorkOrderId
    expected_revision: WorkOrderRevision
    token: ClaimToken
    fence: int
    expires_at: datetime

    def __post_init__(self) -> None:
        if isinstance(self.fence, bool) or not isinstance(self.fence, int) or self.fence < 1:
            raise ValueError("fence must be a positive integer")


@dataclass(frozen=True, slots=True)
class DurableAdmissionRequest:
    """Immutable input accepted under a fenced claim.

    ``intent_digest`` identifies the exact validated intent envelope.  It is
    deliberately opaque to Tactus: verifying the envelope is an application
    boundary responsibility, while this port records and protects its identity.
    """

    work_order_id: WorkOrderId
    expected_revision: WorkOrderRevision
    source_revision: SourceRevision
    claim_token: ClaimToken
    intent_id: ExecutionIntentId
    intent_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_digest", _require_non_empty(self.intent_digest, "intent_digest"))


@dataclass(frozen=True, slots=True)
class DurableAttempt:
    """A semantic attempt persisted independently of a Dagster run receipt."""

    attempt_id: ExecutionAttemptId
    work_order_id: WorkOrderId
    intent_id: ExecutionIntentId
    source_revision: SourceRevision
    fence: int
    accepted_at: datetime
    closed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SubmissionRecord:
    """Immutable dispatch identity committed before any external send."""

    submission_id: SubmissionId
    attempt_id: ExecutionAttemptId
    intent_digest: str
    created_at: datetime
    dagster_run_id: DagsterRunId | None = None


@dataclass(frozen=True, slots=True)
class ResultRecord:
    """Inbound result/decision identity and immutable payload digest."""

    result_id: str
    attempt_id: ExecutionAttemptId
    payload_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_id", _require_non_empty(self.result_id, "result_id"))
        object.__setattr__(self, "payload_digest", _require_non_empty(self.payload_digest, "payload_digest"))


@runtime_checkable
class DurableExecutionAdmissionPort(Protocol):
    """Persistence boundary for fenced admission and restart-safe handoff.

    It is intentionally not a scheduler: it contains no worker slots, queue
    ordering, routing, retry selection, or execution policy.
    """

    def acquire_claim(
        self,
        work_order_id: WorkOrderId | str,
        *,
        expected_revision: WorkOrderRevision,
        claim_token: ClaimToken,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> DomainClaim: ...

    def accept_durable(
        self,
        claim: DomainClaim,
        request: DurableAdmissionRequest,
        *,
        at: datetime | None = None,
    ) -> tuple[DurableAttempt, SubmissionRecord]: ...

    def attach_receipt(
        self,
        submission_id: SubmissionId | str,
        dagster_run_id: DagsterRunId | str,
    ) -> SubmissionRecord: ...

    def pending_submissions(self) -> tuple[SubmissionRecord, ...]:
        """Active submissions eligible for delivery or receipt reconciliation."""
        ...

    def reconciliation_submissions(self) -> tuple[SubmissionRecord, ...]:
        """Closed submissions that may only be queried for a missing receipt."""
        ...

    def mark_dispatched(
        self, submission_id: SubmissionId | str, *, at: datetime | None = None
    ) -> None: ...

    def apply_result(
        self,
        result: ResultRecord,
        *,
        apply_domain_effect: Callable[[object], None] | None = None,
        at: datetime | None = None,
    ) -> bool: ...


# -- port contract --------------------------------------------------------


@runtime_checkable
class ExecutionAdmissionPort(Protocol):
    """Narrow domain admission + execution-request boundary.

    The port answers only:

    * is this ``OPEN + READY`` Work Order still domain-eligible?
    * are its domain dependencies (and configured gates) satisfied?
    * is a validated execution intent present?
    * can an execution attempt be created atomically?

    It must not answer which worker slot is free, when a queued execution should
    start, which queued run gets capacity next, when a step should retry, or how
    run queues are persisted. Those are Dagster concerns.
    """

    def evaluate(
        self,
        work_order: WorkOrder,
        *,
        facts: AdmissionFacts | None = None,
    ) -> AdmissionDecision:
        """Return whether ``work_order`` may be admitted to execution now."""

        ...

    def accept(
        self,
        work_order: WorkOrder,
        request: ExecutionRequest,
        *,
        facts: AdmissionFacts | None = None,
        at: datetime | None = None,
    ) -> ExecutionAttempt:
        """Atomically accept an execution request for an eligible Work Order.

        On success the Work Order transitions ``OPEN + READY -> ACTIVE`` and an
        authoritative :class:`ExecutionAttempt` correlation is recorded. On any
        failure the Work Order and the correlation ledger are left unchanged.
        """

        ...

    def active_attempt(self, work_order_id: WorkOrderId | str) -> ExecutionAttempt | None:
        """Return the current authoritative attempt for a Work Order, if any."""

        ...

    def attempt_for_intent(self, intent_id: ExecutionIntentId | str) -> ExecutionAttempt:
        """Return the attempt correlated with a validated execution intent."""

        ...

    def attempt_for_dagster_run(self, dagster_run_id: DagsterRunId | str) -> ExecutionAttempt:
        """Return the attempt correlated with a Dagster run id."""

        ...

    def close_attempt(self, attempt_id: ExecutionAttemptId | str) -> ExecutionAttempt:
        """Conclude an attempt's correlation (bookkeeping only; no recovery)."""

        ...


# -- pure-domain implementation -------------------------------------------


class ExecutionAdmission:
    """In-memory reference implementation of :class:`ExecutionAdmissionPort`.

    The correlation ledger is deliberately in-process and I/O-free; durable
    persistence is a separate adapter concern. It records only Tactus-owned
    correlation values, never Dagster execution state.
    """

    def __init__(self) -> None:
        self._attempts: dict[ExecutionAttemptId, ExecutionAttempt] = {}
        self._active_by_work_order: dict[WorkOrderId, ExecutionAttempt] = {}
        self._by_intent: dict[ExecutionIntentId, ExecutionAttempt] = {}
        self._by_dagster_run: dict[DagsterRunId, ExecutionAttempt] = {}

    # -- admission evaluation ---------------------------------------------

    def evaluate(
        self,
        work_order: WorkOrder,
        *,
        facts: AdmissionFacts | None = None,
    ) -> AdmissionDecision:
        facts = facts if facts is not None else AdmissionFacts()
        work_order_id = work_order.id

        if work_order.state is not WorkOrderState.OPEN:
            return AdmissionDecision(
                work_order_id, False, f"WorkOrder is {work_order.state.value}, not OPEN"
            )

        readiness = work_order.readiness
        if readiness is not OpenStatus.READY:
            label = readiness.value if readiness is not None else "None"
            return AdmissionDecision(
                work_order_id, False, f"WorkOrder is OPEN but {label}, not READY"
            )

        if not facts.dependencies_satisfied:
            return AdmissionDecision(
                work_order_id, False, "domain dependencies are not satisfied"
            )

        if not facts.capabilities_present:
            return AdmissionDecision(
                work_order_id, False, "required capabilities are not present"
            )

        if facts.system_paused:
            return AdmissionDecision(
                work_order_id, False, "project/system is paused"
            )

        if not facts.budget_available:
            return AdmissionDecision(
                work_order_id, False, "runaway budget guard is engaged"
            )

        return AdmissionDecision(work_order_id, True, "eligible for execution admission")

    # -- atomic acceptance ------------------------------------------------

    def accept(
        self,
        work_order: WorkOrder,
        request: ExecutionRequest,
        *,
        facts: AdmissionFacts | None = None,
        at: datetime | None = None,
    ) -> ExecutionAttempt:
        if request.work_order_id != work_order.id:
            raise ExecutionRequestMismatchError(
                "execution request targets "
                f"{request.work_order_id}, not {work_order.id}"
            )

        # Reject everything before mutating any state so the operation is atomic.
        self._reject_duplicates(work_order.id, request)

        decision = self.evaluate(work_order, facts=facts)
        if not decision.eligible:
            raise IneligibleWorkOrderError(decision.reason)

        accepted_at = at if at is not None else utcnow()
        attempt = ExecutionAttempt(
            attempt_id=ExecutionAttemptId(),
            work_order_id=work_order.id,
            intent_id=request.intent_id,
            dagster_run_id=request.dagster_run_id,
            accepted_at=accepted_at,
        )

        # The lifecycle transition is the only mutating domain step; the
        # correlation is recorded only after it succeeds.
        work_order.claim(
            reason=f"accepted execution attempt {attempt.attempt_id}",
            authority=TransitionAuthority.EXECUTION_ADMISSION,
            at=accepted_at,
        )

        self._attempts[attempt.attempt_id] = attempt
        self._active_by_work_order[attempt.work_order_id] = attempt
        self._by_intent[attempt.intent_id] = attempt
        self._by_dagster_run[attempt.dagster_run_id] = attempt
        return attempt

    def _reject_duplicates(
        self,
        work_order_id: WorkOrderId,
        request: ExecutionRequest,
    ) -> None:
        existing = self._active_by_work_order.get(work_order_id)
        if existing is not None:
            raise DuplicateExecutionAttemptError(
                f"WorkOrder {work_order_id} already has active execution attempt "
                f"{existing.attempt_id}"
            )

        if request.intent_id in self._by_intent:
            raise DuplicateExecutionAttemptError(
                f"execution intent {request.intent_id} is already correlated "
                "with an attempt"
            )

        if request.dagster_run_id in self._by_dagster_run:
            raise DuplicateExecutionAttemptError(
                f"Dagster run {request.dagster_run_id} is already correlated "
                "with an attempt"
            )

    # -- correlation lookups ----------------------------------------------

    def active_attempt(self, work_order_id: WorkOrderId | str) -> ExecutionAttempt | None:
        return self._active_by_work_order.get(_as_work_order_id(work_order_id))

    def attempt(self, attempt_id: ExecutionAttemptId | str) -> ExecutionAttempt:
        key = _as_attempt_id(attempt_id)
        attempt = self._attempts.get(key)
        if attempt is None:
            raise UnknownExecutionAttemptError(f"no execution attempt {key}")
        return attempt

    def attempt_for_intent(self, intent_id: ExecutionIntentId | str) -> ExecutionAttempt:
        key = _as_intent_id(intent_id)
        attempt = self._by_intent.get(key)
        if attempt is None:
            raise UnknownExecutionAttemptError(
                f"no execution attempt for intent {key}"
            )
        return attempt

    def attempt_for_dagster_run(self, dagster_run_id: DagsterRunId | str) -> ExecutionAttempt:
        key = _as_dagster_run_id(dagster_run_id)
        attempt = self._by_dagster_run.get(key)
        if attempt is None:
            raise UnknownExecutionAttemptError(
                f"no execution attempt for Dagster run {key}"
            )
        return attempt

    def close_attempt(self, attempt_id: ExecutionAttemptId | str) -> ExecutionAttempt:
        """Close the active attempt's correlation without deciding recovery.

        This is bookkeeping for the boundary only: it does not choose a recovery
        action, change lifecycle state, or inspect Dagster state. A concluded
        attempt stays in the ledger (preserving intent/run uniqueness) while the
        Work Order may accept a new attempt after a recovery reopen.
        """

        attempt = self.attempt(attempt_id)
        if self._active_by_work_order.get(attempt.work_order_id) is attempt:
            del self._active_by_work_order[attempt.work_order_id]
        return attempt


# -- coercion helpers -----------------------------------------------------


def _as_work_order_id(work_order_id: WorkOrderId | str) -> WorkOrderId:
    return work_order_id if isinstance(work_order_id, WorkOrderId) else WorkOrderId(work_order_id)


def _as_intent_id(intent_id: ExecutionIntentId | str) -> ExecutionIntentId:
    return intent_id if isinstance(intent_id, ExecutionIntentId) else ExecutionIntentId(intent_id)


def _as_dagster_run_id(dagster_run_id: DagsterRunId | str) -> DagsterRunId:
    return dagster_run_id if isinstance(dagster_run_id, DagsterRunId) else DagsterRunId(dagster_run_id)


def _as_attempt_id(attempt_id: ExecutionAttemptId | str) -> ExecutionAttemptId:
    return attempt_id if isinstance(attempt_id, ExecutionAttemptId) else ExecutionAttemptId(attempt_id)
