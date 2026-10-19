"""Compose one durable Tactus execution cycle across the system boundaries.

This module is the missing application seam between the pure Work Order domain,
the durable admission ledger and the already-versioned Ictus contracts. It owns
only *composition*:

* an Ictus ``ALLOW`` decision envelope plus its ``ExecutionIntent`` is the only
  input that may be admitted; Tactus does not evaluate policy or synthesize an
  intent;
* the accepted intent is committed through the fenced durable admission port
  before any external send;
* an inbound ``ExecutionObservation`` v1 is parsed strictly and correlated with
  the exact recorded ``subject/intent/attempt`` before any outcome is applied;
* success is applied as one transactional result effect (close attempt + mark
  Work Order ``IMPLEMENTED``), while a failure observation closes the attempt
  but leaves the Work Order ``ACTIVE`` for a later validated recovery decision;
* a validated Ictus recovery decision is applied through the existing closed
  decision mapping, and the resulting Work Order projection may be persisted.

It does **not** execute Dagster, call a runtime adapter, make recovery policy,
map Dagster run state into a failure taxonomy, retry a step, or choose a route.
Those remain Ictus/Dagster/runtime-adapter responsibilities. The concrete
adapter is supplied by the caller (the bounded validation script uses the
pinned Ictus Dagster bridge).
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from tactus.domain import (
    ClaimToken,
    DagsterRunId,
    DecisionApplicationRecord,
    DecisionApplier,
    DecisionKind,
    DomainClaim,
    DurableAdmissionRequest,
    DurableAttempt,
    DurableExecutionAdmissionPort,
    ExecutionAttempt,
    ExecutionAttemptId,
    ExecutionIntentId,
    FailureObservation,
    OpenStatus,
    RecoveryDecisionRecord,
    ResultRecord,
    SemanticDecision,
    SourceRevision,
    SubmissionId,
    SubmissionRecord,
    TransitionAuthority,
    UnknownDecisionError,
    WorkOrder,
    WorkOrderId,
    WorkOrderRevision,
    WorkOrderState,
)
from tactus.integrations.ictus import (
    AuthorizationGrant,
    BackendFacts,
    EvidenceRef,
    IctusObservationCategory,
    SemanticBudget,
    SnapshotPhase,
    StepRetryDiagnostic,
    TactusObservation,
    build_state_snapshot,
    parse_decision,
    parse_observation,
)


class ExecutionCycleError(ValueError):
    """Base class for a refused execution-cycle operation."""


class IntentDecisionError(ExecutionCycleError):
    """The inbound Ictus decision cannot authorize this execution."""


class ObservationCorrelationError(ExecutionCycleError):
    """An observation does not match the exact accepted attempt."""


class WorkOrderProjectionError(ExecutionCycleError):
    """The durable Work Order projection cannot accept the requested effect."""


@runtime_checkable
class ExecutionCycleAdmissionPort(DurableExecutionAdmissionPort, Protocol):
    """Durable ledger operations needed to compose a full execution cycle.

    The base admission port deliberately exposes only the fenced write path.
    Composition also needs to read back the durable identities it just wrote so
    an observation can be correlated without trusting caller-supplied values.
    """

    def attempt(self, attempt_id: ExecutionAttemptId | str) -> DurableAttempt: ...

    def submission(self, submission_id: SubmissionId | str) -> SubmissionRecord: ...

    def work_order_snapshot(
        self, work_order_id: WorkOrderId | str
    ) -> tuple[WorkOrderRevision, WorkOrderState, OpenStatus | None]: ...


@runtime_checkable
class DurableWorkOrderStatePort(Protocol):
    """Persistence seam for the Work Order state projection after a decision."""

    def apply_work_order_transition(
        self,
        work_order_id: WorkOrderId | str,
        *,
        expected_revision: WorkOrderRevision,
        state: WorkOrderState,
        readiness: OpenStatus | None,
        at: datetime | None = None,
    ) -> WorkOrderRevision: ...


# -- inbound Ictus decision -------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValidatedExecutionIntent:
    """A validated ``ALLOW`` decision and the exact intent it authorizes.

    ``payload`` is the original Ictus-owned intent mapping. ``digest`` is the
    same canonical SHA-256 digest the Ictus Dagster bridge uses for receipt
    idempotency, so Tactus can bind the durable admission to the exact wire
    object without interpreting its routing content.
    """

    intent_id: ExecutionIntentId
    decision_id: str
    decision_kind: str
    capability: str
    work_order_id: WorkOrderId
    provider: str
    payload: Mapping[str, Any]
    digest: str

    @property
    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self.payload))


def canonical_digest(payload: Mapping[str, Any]) -> str:
    """Return the stable digest used by the Ictus durable submission boundary."""

    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def parse_execution_decision(
    envelope: Mapping[str, Any],
    *,
    expected_work_order_id: WorkOrderId | str | None = None,
) -> ValidatedExecutionIntent:
    """Validate the execution-relevant part of an Ictus decision envelope.

    The real boundary emits ``{proposal, policy_decision, execution_intent}``.
    Tactus accepts it only when:

    * the policy verdict is ``ALLOW``;
    * an ``ExecutionIntent`` v1 is present and structurally valid;
    * the intent target, proposal subject and (when supplied) Tactus Work Order
      all identify the same subject;
    * the intent's ``requested_by.decision_id`` is the proposal that was allowed.

    No policy, routing, capability or recovery decision is re-evaluated here.
    """

    if not isinstance(envelope, Mapping):
        raise IntentDecisionError("Ictus decision envelope must be a mapping")
    policy = _required_mapping(envelope, "policy_decision")
    if policy.get("decision") != "ALLOW":
        raise IntentDecisionError(
            "Tactus admits only an Ictus ALLOW decision "
            f"(got {policy.get('decision')!r})"
        )
    proposal = _required_mapping(envelope, "proposal")
    raw_intent = envelope.get("execution_intent")
    if not isinstance(raw_intent, Mapping):
        raise IntentDecisionError("Ictus ALLOW decision carries no ExecutionIntent")

    intent = _validate_intent_payload(raw_intent)
    intent_id = _required_str(intent, "intent_id")
    capability = _required_str(intent, "capability")
    target = _required_mapping(intent, "target")
    target_type = _required_str(target, "type")
    target_id = _required_str(target, "id")
    if target_type != "work_order":
        raise IntentDecisionError(
            f"ExecutionIntent target type must be 'work_order', got {target_type!r}"
        )

    proposal_id = _required_str(proposal, "proposal_id")
    if policy.get("proposal_id") != proposal_id:
        raise IntentDecisionError(
            "policy decision does not belong to the supplied proposal"
        )
    subject = _required_mapping(proposal, "subject")
    if subject.get("type") != target_type or subject.get("id") != target_id:
        raise IntentDecisionError(
            "ExecutionIntent target does not match the proposal subject"
        )
    if expected_work_order_id is not None:
        expected = _work_order_id(expected_work_order_id)
        if target_id != expected.value:
            raise IntentDecisionError(
                f"ExecutionIntent targets {target_id!r}, not Work Order {expected}"
            )

    requested_by = _required_mapping(intent, "requested_by")
    provider = _required_str(requested_by, "provider")
    if requested_by.get("decision_id") != proposal_id:
        raise IntentDecisionError(
            "ExecutionIntent is not requested by the allowed proposal"
        )
    decision_kind = _required_str(proposal, "decision")
    try:
        kind = DecisionKind.from_token(decision_kind)
    except UnknownDecisionError as exc:
        raise IntentDecisionError(str(exc)) from exc
    if not kind.requires_capability:
        raise IntentDecisionError(
            f"proposal decision {decision_kind!r} cannot carry an execution intent"
        )
    decision_kind = kind.value
    proposal_capability = proposal.get("capability")
    if proposal_capability is not None and proposal_capability != capability:
        raise IntentDecisionError(
            "ExecutionIntent capability does not match the allowed proposal"
        )

    return ValidatedExecutionIntent(
        intent_id=ExecutionIntentId(intent_id),
        decision_id=proposal_id,
        decision_kind=decision_kind,
        capability=capability,
        work_order_id=WorkOrderId(target_id),
        provider=provider,
        payload=copy.deepcopy(dict(intent)),
        digest=canonical_digest(intent),
    )


def _validate_intent_payload(intent: Mapping[str, Any]) -> Mapping[str, Any]:
    version = intent.get("schema_version")
    if isinstance(version, bool) or version != 1:
        raise IntentDecisionError(
            f"unsupported ExecutionIntent schema_version {version!r}; supported is 1"
        )
    for field_name in ("intent_id", "capability"):
        _required_str(intent, field_name)
    for field_name in ("target", "requested_by"):
        _required_mapping(intent, field_name)
    arguments = intent.get("arguments")
    if arguments is not None and not isinstance(arguments, Mapping):
        raise IntentDecisionError("ExecutionIntent arguments must be an object")
    policy_context = intent.get("policy_context")
    if policy_context is not None and not isinstance(policy_context, Mapping):
        raise IntentDecisionError("ExecutionIntent policy_context must be an object")
    return intent


# -- accepted attempt and observation correlation ---------------------------


@dataclass(frozen=True, slots=True)
class AcceptedExecution:
    """The durable identities committed for one validated intent."""

    intent: ValidatedExecutionIntent
    claim: DomainClaim
    attempt: DurableAttempt
    submission: SubmissionRecord

    @property
    def submission_id(self) -> str:
        return self.submission.submission_id.value


@dataclass(frozen=True, slots=True)
class CorrelatedObservation:
    """An observation proven to match one recorded attempt and Dagster run."""

    observation: TactusObservation
    attempt: DurableAttempt
    submission: SubmissionRecord
    intent_digest: str


@dataclass(frozen=True, slots=True)
class ObservationApplication:
    """Outcome of applying one correlated observation."""

    correlated: CorrelatedObservation
    applied: bool
    work_order_state: WorkOrderState
    work_order_revision: WorkOrderRevision
    attempt_closed: bool


@dataclass(frozen=True, slots=True)
class RecoveryApplication:
    """Outcome of applying one validated Ictus recovery decision."""

    decision: SemanticDecision
    #: ``None`` on an idempotent replay (the decision already applied).
    application: DecisionApplicationRecord | None
    next_intent: ValidatedExecutionIntent | None
    work_order_revision: WorkOrderRevision | None
    #: ``False`` when this decision was already applied (idempotent replay).
    applied: bool = True


# -- the cycle --------------------------------------------------------------


class ExecutionCycle:
    """Compose validated intent, durable admission, observation and outcome."""

    def __init__(self, admission: ExecutionCycleAdmissionPort) -> None:
        if not isinstance(admission, ExecutionCycleAdmissionPort):
            raise TypeError("admission must implement ExecutionCycleAdmissionPort")
        self._admission = admission

    @property
    def admission(self) -> ExecutionCycleAdmissionPort:
        return self._admission

    # -- admission -------------------------------------------------------

    def accept(
        self,
        claim: DomainClaim,
        decision: Mapping[str, Any] | ValidatedExecutionIntent,
        *,
        source_revision: SourceRevision | str,
        work_order: WorkOrder | None = None,
        at: datetime | None = None,
    ) -> AcceptedExecution:
        """Commit one validated intent under an existing fenced claim.

        The durable write precedes any external dispatch. When a live
        ``work_order`` projection is supplied it is moved to ``ACTIVE`` only
        after the durable acceptance succeeds, so a failed admission does not
        leave the in-memory entity ahead of the ledger.
        """

        validated = (
            decision
            if isinstance(decision, ValidatedExecutionIntent)
            else parse_execution_decision(decision)
        )
        if validated.work_order_id != claim.work_order_id:
            raise IntentDecisionError(
                f"intent targets {validated.work_order_id}, claim owns "
                f"{claim.work_order_id}"
            )
        if work_order is not None and work_order.id != claim.work_order_id:
            raise IntentDecisionError("Work Order projection does not match the claim")
        if work_order is not None and (
            work_order.state is not WorkOrderState.OPEN
            or work_order.readiness is not OpenStatus.READY
        ):
            raise WorkOrderProjectionError(
                "Work Order must be OPEN + READY before durable acceptance "
                f"(state={work_order.state.value}, "
                f"readiness={work_order.readiness.value if work_order.readiness else None})"
            )

        request = DurableAdmissionRequest(
            work_order_id=claim.work_order_id,
            expected_revision=claim.expected_revision,
            source_revision=(
                source_revision
                if isinstance(source_revision, SourceRevision)
                else SourceRevision(source_revision)
            ),
            claim_token=claim.token,
            intent_id=validated.intent_id,
            intent_digest=validated.digest,
        )
        attempt, submission = self._admission.accept_durable(claim, request, at=at)
        if work_order is not None:
            work_order.claim(
                reason=(
                    "durable execution admission accepted intent "
                    f"{validated.intent_id} (attempt {attempt.attempt_id})"
                ),
                authority=TransitionAuthority.EXECUTION_ADMISSION,
                at=at,
            )
        return AcceptedExecution(validated, claim, attempt, submission)

    def attach_receipt(
        self,
        accepted: AcceptedExecution,
        dagster_run_id: DagsterRunId | str,
    ) -> SubmissionRecord:
        """Attach the runtime adapter's durable receipt to the exact submission."""

        record = self._admission.attach_receipt(
            accepted.submission.submission_id, dagster_run_id
        )
        if record.attempt_id != accepted.attempt.attempt_id:
            raise ObservationCorrelationError(
                "receipt was attached to a different execution attempt"
            )
        return record

    # -- observation parsing and correlation -----------------------------

    @staticmethod
    def parse_observation(payload: Mapping[str, Any]) -> TactusObservation:
        """Strictly parse an Ictus ``ExecutionObservation`` v1 receipt."""

        return parse_observation(payload)

    def correlate(
        self,
        observation: Mapping[str, Any] | TactusObservation,
        *,
        attempt_id: ExecutionAttemptId | str,
        submission_id: Any,
        expected_work_order_id: WorkOrderId | str | None = None,
        expected_intent_digest: str | None = None,
    ) -> CorrelatedObservation:
        """Bind an observation to ``subject/intent/attempt``, failing closed.

        The checks are deliberately redundant with durable uniqueness: a stale
        observation for another Work Order, intent, run or already-closed
        attempt must never be applied as this attempt's outcome.
        """

        parsed = (
            observation
            if isinstance(observation, TactusObservation)
            else parse_observation(observation)
        )
        attempt = self._admission.attempt(attempt_id)
        submission = self._admission.submission(submission_id)
        if submission.attempt_id != attempt.attempt_id:
            raise ObservationCorrelationError(
                "submission does not belong to the referenced execution attempt"
            )
        if attempt.closed_at is not None:
            raise ObservationCorrelationError(
                f"execution attempt {attempt.attempt_id} is already closed"
            )
        if submission.dagster_run_id is None:
            raise ObservationCorrelationError(
                "submission has no durable Dagster receipt to correlate"
            )
        if parsed.execution_id != submission.dagster_run_id.value:
            raise ObservationCorrelationError(
                "observation execution does not match the receipt Dagster run"
            )
        if parsed.intent_id != attempt.intent_id.value:
            raise ObservationCorrelationError(
                "observation intent does not match the accepted attempt intent"
            )
        if (
            expected_work_order_id is not None
            and attempt.work_order_id != _work_order_id(expected_work_order_id)
        ):
            raise ObservationCorrelationError(
                "observation attempt belongs to a different Work Order"
            )
        if (
            expected_intent_digest is not None
            and submission.intent_digest != expected_intent_digest
        ):
            raise ObservationCorrelationError(
                "submission intent digest does not match the validated intent"
            )
        return CorrelatedObservation(
            observation=parsed,
            attempt=attempt,
            submission=submission,
            intent_digest=submission.intent_digest,
        )

    # -- outcome application ---------------------------------------------

    def apply_observation(
        self,
        correlated: CorrelatedObservation,
        *,
        work_order: WorkOrder | None = None,
        expected_revision: WorkOrderRevision | None = None,
        at: datetime | None = None,
    ) -> ObservationApplication:
        """Apply exactly one correlated observation as a deduplicated result.

        A success effect closes the attempt and marks the Work Order
        ``IMPLEMENTED`` in the same transaction. A failure effect closes the
        attempt but leaves the Work Order ``ACTIVE`` so no automatic retry is
        implied; Ictus must make a separate validated recovery decision.
        """

        if not isinstance(correlated, CorrelatedObservation):
            raise TypeError("correlated must be a CorrelatedObservation")
        if work_order is not None and work_order.id != correlated.attempt.work_order_id:
            raise WorkOrderProjectionError(
                "Work Order projection does not match the correlated attempt"
            )
        if work_order is not None and work_order.state is not WorkOrderState.ACTIVE:
            raise WorkOrderProjectionError(
                "the supplied Work Order projection must be ACTIVE before an "
                f"observation outcome can be applied (state={work_order.state.value})"
            )
        revision = self._current_revision(
            correlated.attempt.work_order_id,
            expected_revision=expected_revision,
        )
        result = ResultRecord(
            result_id=f"observation:{correlated.observation.observation_id}",
            attempt_id=correlated.attempt.attempt_id,
            payload_digest=_observation_digest(correlated.observation),
        )
        success = (
            correlated.observation.category is IctusObservationCategory.SUCCESS
        )

        if success:
            def apply_success(connection: object) -> None:
                self._complete_work_order(
                    connection,
                    correlated.attempt.work_order_id,
                    revision,
                    at=at,
                )
        else:
            def apply_failure(connection: object) -> None:
                state, readiness, _ = _work_order_snapshot(
                    connection, correlated.attempt.work_order_id
                )
                if state is not WorkOrderState.ACTIVE or readiness is not None:
                    raise WorkOrderProjectionError(
                        "a failed observation requires an ACTIVE Work Order"
                    )

        applied = self._admission.apply_result(
            result,
            apply_domain_effect=apply_success if success else apply_failure,
            at=at,
        )

        if applied:
            if success and work_order is not None:
                if work_order.state is WorkOrderState.ACTIVE:
                    work_order.complete(
                        reason=(
                            "Ictus SUCCESS observation "
                            f"{correlated.observation.observation_id} for attempt "
                            f"{correlated.attempt.attempt_id}"
                        ),
                        authority=TransitionAuthority.VERIFIER,
                        at=at,
                    )
            elif not success and work_order is not None:
                work_order.record_failure(
                    FailureObservation(
                        summary=correlated.observation.category.value,
                        detail=correlated.observation.message or "",
                        observed_at=correlated.observation.observed_at,
                        evidence={
                            ref.kind: ref.uri for ref in correlated.observation.evidence
                        },
                    )
                )

        _, state, _ = self._admission.work_order_snapshot(
            correlated.attempt.work_order_id
        )
        return ObservationApplication(
            correlated=correlated,
            applied=applied,
            work_order_state=state if isinstance(state, WorkOrderState) else WorkOrderState(state),
            work_order_revision=self._current_revision(
                correlated.attempt.work_order_id
            ),
            attempt_closed=(
                self._admission.attempt(correlated.attempt.attempt_id).closed_at
                is not None
            ),
        )

    # -- recovery decision -----------------------------------------------

    def recovery_snapshot(
        self,
        work_order: WorkOrder,
        *,
        observation: TactusObservation,
        attempt: DurableAttempt,
        submission: SubmissionRecord,
        semantic_budget: SemanticBudget,
        step_retry: StepRetryDiagnostic | int,
        snapshot_id: str,
        timestamp: datetime,
        capability_id: str,
        backend_facts: BackendFacts | None = None,
        authorization_grants: tuple[AuthorizationGrant, ...] = (),
        dependencies_satisfied: bool | None = None,
        scope_constraints: tuple[str, ...] = (),
        constraints: tuple[str, ...] = (),
        source_revision: str | None = None,
        work_order_revision: int | None = None,
    ) -> dict[str, Any]:
        """Build the outbound RECOVERY context from a real correlated attempt."""

        if submission.attempt_id != attempt.attempt_id:
            raise ObservationCorrelationError(
                "submission does not belong to the recovery attempt"
            )
        if submission.dagster_run_id is None:
            raise ObservationCorrelationError(
                "recovery requires a durable Dagster receipt"
            )
        correlated_attempt = ExecutionAttempt(
            attempt_id=attempt.attempt_id,
            work_order_id=attempt.work_order_id,
            intent_id=attempt.intent_id,
            dagster_run_id=submission.dagster_run_id,
            accepted_at=attempt.accepted_at,
        )
        return build_state_snapshot(
            work_order=work_order,
            phase=SnapshotPhase.RECOVERY,
            observation=observation,
            attempt=correlated_attempt,
            semantic_budget=semantic_budget,
            step_retry=step_retry,
            snapshot_id=snapshot_id,
            timestamp=timestamp,
            capability_id=capability_id,
            backend_facts=backend_facts,
            authorization_grants=authorization_grants,
            dependencies_satisfied=dependencies_satisfied,
            scope_constraints=scope_constraints,
            constraints=constraints,
            source_revision=source_revision,
            work_order_revision=work_order_revision,
        )

    def apply_recovery_decision(
        self,
        work_order: WorkOrder,
        envelope: Mapping[str, Any],
        *,
        state_port: DurableWorkOrderStatePort,
        expected_revision: WorkOrderRevision,
        at: datetime | None = None,
        child_plan: Any = None,
    ) -> RecoveryApplication:
        """Apply one ALLOWED Ictus recovery decision and persist its projection.

        The decision vocabulary and mapping stay in the existing domain applier;
        this method only binds the envelope subject to the Work Order and writes
        the resulting guarded state/readiness to the durable projection.
        """

        if not isinstance(envelope, Mapping):
            raise IntentDecisionError("Ictus decision envelope must be a mapping")
        policy = _required_mapping(envelope, "policy_decision")
        if policy.get("decision") != "ALLOW":
            raise IntentDecisionError(
                "Tactus applies only an Ictus ALLOW decision "
                f"(got {policy.get('decision')!r})"
            )
        proposal = _required_mapping(envelope, "proposal")
        subject = _required_mapping(proposal, "subject")
        if subject.get("type") != "work_order" or subject.get("id") != work_order.id.value:
            raise IntentDecisionError(
                "Ictus recovery decision does not target this Work Order"
            )
        decision = parse_decision(proposal)

        next_intent: ValidatedExecutionIntent | None = None
        raw_intent = envelope.get("execution_intent")
        if raw_intent is None and decision.kind.requires_capability:
            raise IntentDecisionError(
                f"Ictus {decision.kind.value} ALLOW decision carries no ExecutionIntent"
            )
        if raw_intent is not None:
            next_intent = parse_execution_decision(
                envelope, expected_work_order_id=work_order.id
            )

        # Bind the decision to the exact attempt it recovers and to the
        # revision it was validated against. Everything below commits in one
        # transaction: the guarded projection, the domain effect (intervention,
        # terminal outcome or children), the attempt closure and the
        # idempotency row. The live entity is only mutated inside that
        # transaction, so a replay or a rollback never leaves it ahead.
        # An *entry* decision authorizes the first attempt, so no attempt exists
        # yet; a recovery decision is bound to the attempt that just failed.
        attempt = self._admission.find_latest_attempt(work_order.id)
        if attempt is None:
            # No attempt exists yet: this can only be the INITIAL authorization
            # that creates the first semantic attempt. Recovery effects
            # (ESCALATE / ABORT / DECOMPOSE) require a correlated attempt and
            # must never be reachable without one.
            if not decision.kind.requires_capability:
                raise IntentDecisionError(
                    f"Ictus {decision.kind.value} requires a correlated execution "
                    "attempt; the absence of an attempt authorizes only the "
                    "INITIAL execution decision"
                )
            if next_intent is None:
                raise IntentDecisionError(
                    "an INITIAL authorization requires the Ictus ExecutionIntent"
                )
        record = RecoveryDecisionRecord(
            decision_id=decision.decision_id,
            work_order_id=work_order.id,
            attempt_id=None if attempt is None else attempt.attempt_id,
            decision_digest=canonical_digest(
                {
                    "decision": decision.kind.value,
                    "decision_id": decision.decision_id,
                    "capability": decision.capability,
                    "proposal": dict(proposal),
                    "policy": dict(policy),
                }
            ),
            expected_revision=expected_revision,
        )

        applier = DecisionApplier()
        original = _snapshot_work_order(work_order)
        state = {"application": None}

        def apply_effect(connection: object) -> None:
            # Only executed by the ledger when this decision is not a replay.
            state["application"] = applier.apply(
                work_order, decision, child_plan=child_plan, at=at
            )
            state_port.apply_transition_in(
                connection,  # same transaction as the ledger row
                work_order.id,
                expected_revision=expected_revision,
                state=work_order.state,
                readiness=work_order.readiness,
                at=at,
            )

        # The entity is mutated only inside the transaction, but the transaction
        # can still fail *after* the callback returns: attempt closure, the
        # idempotency insert, or COMMIT itself. SQLite rolls those back, so the
        # in-memory projection must be restored here as well — otherwise the
        # caller keeps a transition that was never persisted.
        try:
            applied = self._admission.apply_recovery_decision(
                record, apply_domain_effect=apply_effect, at=at
            )
        except BaseException:
            _restore_work_order(work_order, original)
            raise
        # ``None`` is a replay: the decision was already applied, so no new
        # application record and no second mutation.
        application = state["application"]
        revision, _, _ = self._admission.work_order_snapshot(work_order.id)
        return RecoveryApplication(
            decision=decision,
            application=application,
            # A replayed decision authorizes nothing new: the caller must not be
            # able to redispatch a semantic attempt that was already applied.
            next_intent=next_intent if applied else None,
            work_order_revision=revision,
            applied=applied,
        )

    # -- durable helpers -------------------------------------------------

    def _current_revision(
        self,
        work_order_id: WorkOrderId,
        *,
        expected_revision: WorkOrderRevision | None = None,
    ) -> WorkOrderRevision:
        revision, _, _ = self._admission.work_order_snapshot(work_order_id)
        if expected_revision is not None and revision != expected_revision:
            raise WorkOrderProjectionError(
                f"Work Order {work_order_id} is at revision {revision.value}, "
                f"expected {expected_revision.value}"
            )
        return revision

    def _complete_work_order(
        self,
        connection: object,
        work_order_id: WorkOrderId,
        expected_revision: WorkOrderRevision,
        *,
        at: datetime | None,
    ) -> None:
        cursor = connection.execute(  # type: ignore[attr-defined]
            """UPDATE work_orders
            SET state = ?, readiness = NULL, revision = revision + 1, updated_at = ?
            WHERE work_order_id = ? AND revision = ? AND state = ?""",
            (
                WorkOrderState.IMPLEMENTED.value,
                _timestamp(at),
                work_order_id.value,
                expected_revision.value,
                WorkOrderState.ACTIVE.value,
            ),
        )
        if cursor.rowcount != 1:
            raise WorkOrderProjectionError(
                "Work Order was not ACTIVE at the expected revision when success "
                "was applied"
            )


def _snapshot_work_order(work_order: WorkOrder) -> tuple[Any, ...]:
    return (
        work_order._state,  # noqa: SLF001 - deliberate rollback boundary
        work_order._readiness,  # noqa: SLF001
        work_order._prerequisites_known,  # noqa: SLF001
        copy.deepcopy(work_order._transitions),  # noqa: SLF001
        copy.deepcopy(work_order._failures),  # noqa: SLF001
        copy.deepcopy(work_order._blockers),  # noqa: SLF001
    )


def _restore_work_order(work_order: WorkOrder, snapshot: tuple[Any, ...]) -> None:
    (
        state,
        readiness,
        prerequisites_known,
        transitions,
        failures,
        blockers,
    ) = snapshot
    work_order._state = state  # noqa: SLF001
    work_order._readiness = readiness  # noqa: SLF001
    work_order._prerequisites_known = prerequisites_known  # noqa: SLF001
    work_order._transitions = transitions  # noqa: SLF001
    work_order._failures = failures  # noqa: SLF001
    work_order._blockers = blockers  # noqa: SLF001


# -- serialization helpers ---------------------------------------------------


def observation_payload(observation: TactusObservation) -> dict[str, Any]:
    """Return the normalized JSON shape used for observation digests/evidence."""

    payload: dict[str, Any] = {
        "schema_version": observation.schema_version,
        "observation_id": observation.observation_id,
        "execution_id": observation.execution_id,
        "intent_id": observation.intent_id,
        "category": observation.category.value,
        "observed_at": observation.observed_at.isoformat(),
    }
    if observation.message is not None:
        payload["message"] = observation.message
    if observation.evidence:
        payload["evidence"] = [_evidence_payload(ref) for ref in observation.evidence]
    if observation.retryable is not None:
        payload["retryable"] = observation.retryable
    return payload


def _observation_digest(observation: TactusObservation) -> str:
    return canonical_digest(observation_payload(observation))


def _evidence_payload(ref: EvidenceRef) -> dict[str, Any]:
    payload: dict[str, Any] = {"kind": ref.kind, "uri": ref.uri}
    if ref.sha256 is not None:
        payload["sha256"] = ref.sha256
    if ref.note is not None:
        payload["note"] = ref.note
    return payload


def _work_order_snapshot(
    connection: object, work_order_id: WorkOrderId
) -> tuple[WorkOrderState, OpenStatus | None, int]:
    row = connection.execute(  # type: ignore[attr-defined]
        "SELECT state, readiness, revision FROM work_orders WHERE work_order_id = ?",
        (work_order_id.value,),
    ).fetchone()
    if row is None:
        raise WorkOrderProjectionError(f"no durable Work Order {work_order_id}")
    readiness = OpenStatus(row["readiness"]) if row["readiness"] is not None else None
    return WorkOrderState(row["state"]), readiness, int(row["revision"])


def _timestamp(value: datetime | None) -> str:
    from tactus.domain._clock import utcnow

    resolved = value if value is not None else utcnow()
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return resolved.isoformat()


# -- validation helpers ------------------------------------------------------


def _required_mapping(container: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    value = container.get(field_name)
    if not isinstance(value, Mapping):
        raise IntentDecisionError(f"{field_name} must be an object")
    return value


def _required_str(container: Mapping[str, Any], field_name: str) -> str:
    value = container.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise IntentDecisionError(f"{field_name} must be a non-empty string")
    return value


def _work_order_id(value: WorkOrderId | str) -> WorkOrderId:
    return value if isinstance(value, WorkOrderId) else WorkOrderId(value)


__all__ = [
    "AcceptedExecution",
    "CorrelatedObservation",
    "DurableWorkOrderStatePort",
    "ExecutionCycleAdmissionPort",
    "ExecutionCycle",
    "ExecutionCycleError",
    "IntentDecisionError",
    "ObservationApplication",
    "ObservationCorrelationError",
    "RecoveryApplication",
    "ValidatedExecutionIntent",
    "WorkOrderProjectionError",
    "canonical_digest",
    "observation_payload",
    "parse_execution_decision",
]
