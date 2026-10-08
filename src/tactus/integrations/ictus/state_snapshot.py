"""Pure, versioned Tactus -> Ictus ``StateSnapshot`` context adapter.

Pinned contract
---------------
* Ictus commit: ``833175d``
* Contract source: ``contracts/state-snapshot.schema.json`` (``StateSnapshot``)
* Supported generic ``schema_version``: ``1`` (Ictus-owned)
* Supported Tactus *fact profile* version: ``2``
  (:data:`SUPPORTED_SNAPSHOT_PROFILE_VERSION`, versioned independently)

Ictus's ``DecisionProvider`` consumes a ``StateSnapshot``. This module is the
*outbound* sibling of the inbound execution-observation boundary: it assembles
one valid, domain-neutral ``StateSnapshot`` v1 payload from Tactus/domain facts.
It decides nothing and mutates nothing.

Ownership boundary (normative)::

    ExecutionObservation (inbound, validated by .observation)
            + WorkOrder / source / revision facts
            + fenced execution attempt correlation
            + canonical semantic count/limit + diagnostic step-retry index
            + raw backend descriptors / health / quota facts
            + raw authorization grants/evidence + constraints
            ↓
    Ictus StateSnapshot v1 (this module: assemble facts, decide nothing)
            ↓
    Ictus DecisionProvider (owns all policy)

Two phases, one profile
-----------------------
* :attr:`SnapshotPhase.INITIAL` — first execution. It carries **no** execution
  observation: a fake/previous observation is never fabricated. The context is
  still fully specified (work order, budget, backend and authorization facts).
* :attr:`SnapshotPhase.RECOVERY` — recovery after a failed execution. It carries
  exactly one validated, **correlated** failure observation. The authoritative
  :class:`~tactus.domain.admission.ExecutionAttempt` is checked against the
  Work Order (subject), the observation's ``intent_id`` (intent) and its
  ``execution_id``/Dagster run (attempt). A mismatch fails closed.

Canonical budget facts vs execution diagnostics
-----------------------------------------------
``recovery.semantic_attempts`` / ``recovery.max_semantic_attempts`` are the
canonical semantic count/limit; another attempt is permitted only while
``count < limit``. ``execution.step_retry_index`` is an execution-owned
diagnostic and can never consume or refill the semantic allowance. The
superseded ``attempt.number``/``retry.attempt``/``retry.budget`` facts are
legacy, not emitted, and only translated explicitly by
:func:`tactus.integrations.ictus.facts.translate_legacy_budget_facts`.

Deterministic profile and digest
--------------------------------
The profile version, phase, canonical facts and a SHA-256 ``snapshot.digest``
over the canonical JSON body are emitted. With an explicit ``snapshot_id`` and
``timestamp`` the whole payload, including the digest, is deterministic.
Building context never mutates the Work Order or any domain entity.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tactus.domain import ExecutionAttempt, WorkOrder
from tactus.domain._clock import utcnow

from .facts import (
    LEGACY_FACT_ATTEMPT_NUMBER,
    SUPPORTED_SNAPSHOT_PROFILE_VERSION,
    AuthorizationGrant,
    BackendFacts,
    MalformedSnapshotError,
    SemanticBudget,
    SnapshotPhase,
    StateSnapshotContractError,
    StepRetryDiagnostic,
    UnsupportedSnapshotProfileError,
    UnsupportedSnapshotVersionError,
    _require_count,
    authorization_grant_fact,
    backend_descriptor_fact,
    backend_quota_fact,
    backend_status_fact,
    to_rfc3339,
)
from .observation import ICTUS_COMMIT, EvidenceRef, IctusObservationCategory, TactusObservation

#: Ictus generic ``StateSnapshot`` contract version supported here.
SUPPORTED_SNAPSHOT_SCHEMA_VERSION = 1

#: Authoritative Ictus schema file this adapter conforms to.
STATE_SNAPSHOT_SCHEMA = "contracts/state-snapshot.schema.json"

#: Domain namespace used for the Tactus software-engineering control plane.
DEFAULT_DOMAIN = "software"

#: ``subject.type`` used for Tactus Work Orders.
WORK_ORDER_SUBJECT_TYPE = "work_order"

# -- profile fact keys -----------------------------------------------------

FACT_PROFILE_VERSION = "snapshot.profile_version"
FACT_SNAPSHOT_PHASE = "snapshot.phase"
FACT_SNAPSHOT_DIGEST = "snapshot.digest"

# -- observation facts (RECOVERY only) ------------------------------------
FACT_OBSERVATION_CATEGORY = "observation.category"
FACT_OBSERVATION_MESSAGE = "observation.message"
FACT_OBSERVATION_ID = "observation.observation_id"
FACT_OBSERVATION_EXECUTION_ID = "observation.execution_id"
FACT_OBSERVATION_INTENT_ID = "observation.intent_id"
FACT_OBSERVATION_OBSERVED_AT = "observation.observed_at"
FACT_OBSERVATION_RETRYABLE = "observation.retryable"
FACT_OBSERVATION_EVIDENCE = "observation.evidence"

# -- work order / source facts --------------------------------------------
FACT_WORK_ORDER_ID = "work_order.id"
FACT_WORK_ORDER_STATE = "work_order.state"
FACT_WORK_ORDER_READINESS = "work_order.readiness"
FACT_WORK_ORDER_REVISION = "work_order.revision"
FACT_SOURCE_REVISION = "source.revision"

# -- canonical semantic budget + execution diagnostic ---------------------
FACT_RECOVERY_SEMANTIC_ATTEMPTS = "recovery.semantic_attempts"
FACT_RECOVERY_MAX_SEMANTIC_ATTEMPTS = "recovery.max_semantic_attempts"
FACT_EXECUTION_STEP_RETRY_INDEX = "execution.step_retry_index"
FACT_RECOVERY_ATTEMPT_ID = "recovery.attempt_id"
FACT_RECOVERY_INTENT_ID = "recovery.intent_id"

# -- capability / backend / authorization / domain facts ------------------
FACT_CAPABILITY_ID = "capability.id"
FACT_BACKEND_DESCRIPTORS = "backend.descriptors"
FACT_BACKEND_STATUS = "backend.status"
FACT_BACKEND_ADMINISTRATIVE_ENABLEMENT = "backend.administrative_enablement"
FACT_BACKEND_QUOTA = "backend.quota"
FACT_BACKEND_PREVIOUS_BACKEND = "backend.previous_backend"
FACT_AUTHORIZATION_GRANTS = "authorization.grants"
FACT_DOMAIN_DEPENDENCIES_SATISFIED = "domain.dependencies_satisfied"
FACT_DOMAIN_SCOPE_CONSTRAINTS = "domain.scope_constraints"

#: Superseded v1-profile fact key. Retained only for legacy interop; never emitted.
FACT_ATTEMPT_NUMBER = LEGACY_FACT_ATTEMPT_NUMBER


@dataclass(frozen=True, slots=True)
class AttemptHistory:
    """DEPRECATED legacy two-counter input from the superseded v1 profile.

    Use :class:`SemanticBudget` (canonical count/limit) plus
    :class:`StepRetryDiagnostic`. A compatibility consumer must translate
    explicitly with :func:`semantic_budget_from_legacy` and must never infer a
    budget from absent facts.
    """

    dagster_micro_retry_attempt: int = 0
    semantic_attempts: int = 0

    def __post_init__(self) -> None:
        for label, value in (
            ("dagster_micro_retry_attempt", self.dagster_micro_retry_attempt),
            ("semantic_attempts", self.semantic_attempts),
        ):
            _require_count(value, label)


def semantic_budget_from_legacy(
    history: AttemptHistory,
    *,
    max_semantic_attempts: int,
) -> SemanticBudget:
    """Explicitly translate a legacy :class:`AttemptHistory` to canonical facts.

    The legacy ``dagster_micro_retry_attempt`` becomes the diagnostic
    step-retry index (not the semantic count). The caller must supply the
    authorized limit explicitly: it is never inferred from the legacy input.
    """

    if not isinstance(history, AttemptHistory):
        raise StateSnapshotContractError("history must be an AttemptHistory")
    return SemanticBudget(
        semantic_attempts=history.semantic_attempts,
        max_semantic_attempts=max_semantic_attempts,
    )


def build_state_snapshot(
    *,
    work_order: WorkOrder,
    phase: SnapshotPhase | str | None = None,
    observation: TactusObservation | None = None,
    attempt: ExecutionAttempt | None = None,
    semantic_budget: SemanticBudget | None = None,
    step_retry: StepRetryDiagnostic | int | None = None,
    snapshot_id: str | None = None,
    timestamp: datetime | None = None,
    domain: str = DEFAULT_DOMAIN,
    subject_type: str = WORK_ORDER_SUBJECT_TYPE,
    capability_id: str | None = None,
    backend_facts: BackendFacts | None = None,
    authorization_grants: Iterable[AuthorizationGrant] = (),
    dependencies_satisfied: bool | None = None,
    scope_constraints: Iterable[str] = (),
    constraints: Iterable[str] = (),
    source_revision: str | None = None,
    work_order_revision: int | None = None,
) -> dict[str, Any]:
    """Build one versioned Ictus ``StateSnapshot`` payload.

    Pure: it reads the Work Order's public, read-only views and the supplied
    domain facts, and returns a fresh JSON-serializable ``dict``. It never
    mutates the Work Order, never performs a lifecycle transition and takes no
    decision.

    ``phase`` is inferred when omitted: an observation or correlated attempt
    implies ``RECOVERY``, otherwise ``INITIAL``. INITIAL must not carry an
    observation; RECOVERY requires the correlated attempt and rejects a wrong
    subject, intent or attempt/execution.
    """

    if not isinstance(work_order, WorkOrder):
        raise StateSnapshotContractError("work_order must be a WorkOrder")

    if semantic_budget is None:
        raise StateSnapshotContractError(
            "semantic_budget is required; missing budget must not default to zero/unlimited"
        )
    if not isinstance(semantic_budget, SemanticBudget):
        raise StateSnapshotContractError("semantic_budget must be a SemanticBudget")

    resolved_phase = _coerce_phase(phase, observation=observation, attempt=attempt)
    resolved_step_retry = _coerce_step_retry(step_retry)

    if resolved_phase is SnapshotPhase.INITIAL:
        _validate_initial(observation=observation, attempt=attempt)
    else:
        _validate_recovery(
            work_order=work_order,
            observation=observation,
            attempt=attempt,
        )

    resolved_snapshot_id = (
        _require_non_empty(snapshot_id, "snapshot_id")
        if snapshot_id is not None
        else uuid.uuid4().hex
    )
    resolved_domain = _require_non_empty(domain, "domain")
    resolved_subject_type = _require_non_empty(subject_type, "subject_type")
    resolved_timestamp = _to_rfc3339(timestamp if timestamp is not None else utcnow())

    capabilities: list[str] = []
    fact_entries: list[dict[str, Any]] = []

    # -- profile facts ---------------------------------------------------
    fact_entries.append(
        {"key": FACT_PROFILE_VERSION, "value": SUPPORTED_SNAPSHOT_PROFILE_VERSION}
    )
    fact_entries.append({"key": FACT_SNAPSHOT_PHASE, "value": resolved_phase.value})

    # -- correlated execution observation (RECOVERY only, never fabricated)
    if resolved_phase is SnapshotPhase.RECOVERY:
        assert observation is not None  # validated above
        assert attempt is not None  # validated above
        fact_entries.extend(_observation_facts(observation))
        fact_entries.append(
            {"key": FACT_RECOVERY_ATTEMPT_ID, "value": attempt.attempt_id.value}
        )
        fact_entries.append(
            {"key": FACT_RECOVERY_INTENT_ID, "value": attempt.intent_id.value}
        )

    # -- Work Order / source facts ---------------------------------------
    fact_entries.append({"key": FACT_WORK_ORDER_ID, "value": work_order.id.value})
    fact_entries.append({"key": FACT_WORK_ORDER_STATE, "value": work_order.state.value})
    if work_order.readiness is not None:
        fact_entries.append(
            {"key": FACT_WORK_ORDER_READINESS, "value": work_order.readiness.value}
        )
    if work_order_revision is not None:
        fact_entries.append(
            {
                "key": FACT_WORK_ORDER_REVISION,
                "value": _require_non_negative_int(work_order_revision, "work_order_revision"),
            }
        )
    if source_revision is not None:
        fact_entries.append(
            {
                "key": FACT_SOURCE_REVISION,
                "value": _require_non_empty(source_revision, "source_revision"),
            }
        )

    # -- canonical semantic budget + diagnostic step retry ---------------
    fact_entries.append(
        {
            "key": FACT_RECOVERY_SEMANTIC_ATTEMPTS,
            "value": semantic_budget.semantic_attempts,
        }
    )
    fact_entries.append(
        {
            "key": FACT_RECOVERY_MAX_SEMANTIC_ATTEMPTS,
            "value": semantic_budget.max_semantic_attempts,
        }
    )
    fact_entries.append(
        {"key": FACT_EXECUTION_STEP_RETRY_INDEX, "value": resolved_step_retry.index}
    )

    # -- capability ------------------------------------------------------
    if capability_id is not None:
        resolved_capability = _require_non_empty(capability_id, "capability_id")
        fact_entries.append({"key": FACT_CAPABILITY_ID, "value": resolved_capability})
        capabilities.append(resolved_capability)

    # -- raw backend facts (never a filtered candidate list) -------------
    _append_backend_facts(fact_entries, backend_facts)

    # -- raw authorization evidence --------------------------------------
    grants = tuple(authorization_grants)
    fact_entries.append(
        {
            "key": FACT_AUTHORIZATION_GRANTS,
            "value": [authorization_grant_fact(grant) for grant in grants],
        }
    )

    # -- domain / control facts ------------------------------------------
    if dependencies_satisfied is not None:
        if not isinstance(dependencies_satisfied, bool):
            raise StateSnapshotContractError(
                "dependencies_satisfied must be a boolean when present"
            )
        fact_entries.append(
            {
                "key": FACT_DOMAIN_DEPENDENCIES_SATISFIED,
                "value": dependencies_satisfied,
            }
        )
    fact_entries.append(
        {
            "key": FACT_DOMAIN_SCOPE_CONSTRAINTS,
            "value": [_require_non_empty(item, "scope constraint") for item in scope_constraints],
        }
    )

    payload: dict[str, Any] = {
        "schema_version": SUPPORTED_SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": resolved_snapshot_id,
        "timestamp": resolved_timestamp,
        "domain": resolved_domain,
        "subject": {"type": resolved_subject_type, "id": work_order.id.value},
        "facts": fact_entries,
        "capabilities": capabilities,
        "constraints": [
            _require_non_empty(item, "constraint") for item in constraints
        ],
    }

    # Digest over the canonical body *without* the digest fact itself, then
    # append the digest. Deterministic for explicit snapshot_id/timestamp.
    digest = compute_snapshot_digest(payload)
    fact_entries.append({"key": FACT_SNAPSHOT_DIGEST, "value": digest})
    return payload


# -- phase / validation helpers -------------------------------------------


def _coerce_phase(
    phase: SnapshotPhase | str | None,
    *,
    observation: TactusObservation | None,
    attempt: ExecutionAttempt | None,
) -> SnapshotPhase:
    if phase is None:
        supplied = observation is not None or attempt is not None
        return SnapshotPhase.RECOVERY if supplied else SnapshotPhase.INITIAL
    if isinstance(phase, SnapshotPhase):
        return phase
    if isinstance(phase, str):
        try:
            return SnapshotPhase(phase)
        except ValueError:
            raise StateSnapshotContractError(
                f"unknown snapshot phase: {phase!r}; supported are "
                f"{SnapshotPhase.INITIAL.value}/{SnapshotPhase.RECOVERY.value}"
            ) from None
    raise StateSnapshotContractError("phase must be a SnapshotPhase or string")


def _coerce_step_retry(
    step_retry: StepRetryDiagnostic | int | None,
) -> StepRetryDiagnostic:
    if step_retry is None:
        return StepRetryDiagnostic()
    if isinstance(step_retry, StepRetryDiagnostic):
        return step_retry
    if isinstance(step_retry, bool) or not isinstance(step_retry, int):
        raise StateSnapshotContractError(
            "step_retry must be a StepRetryDiagnostic or non-negative integer"
        )
    return StepRetryDiagnostic(index=step_retry)


def _validate_initial(
    *,
    observation: TactusObservation | None,
    attempt: ExecutionAttempt | None,
) -> None:
    if observation is not None:
        raise StateSnapshotContractError(
            "initial snapshot must not carry an execution observation"
        )
    if attempt is not None:
        raise StateSnapshotContractError(
            "initial snapshot must not carry a recovery attempt correlation"
        )


def _validate_recovery(
    *,
    work_order: WorkOrder,
    observation: TactusObservation | None,
    attempt: ExecutionAttempt | None,
) -> None:
    if observation is None:
        raise StateSnapshotContractError(
            "recovery snapshot requires a validated correlated observation"
        )
    if not isinstance(observation, TactusObservation):
        raise StateSnapshotContractError(
            "observation must be a validated TactusObservation"
        )
    if attempt is None:
        raise StateSnapshotContractError(
            "recovery snapshot requires the authoritative ExecutionAttempt correlation"
        )
    if not isinstance(attempt, ExecutionAttempt):
        raise StateSnapshotContractError("attempt must be an ExecutionAttempt")

    if attempt.work_order_id.value != work_order.id.value:
        raise StateSnapshotContractError(
            "recovery observation subject does not match the Work Order"
        )
    if attempt.intent_id.value != observation.intent_id:
        raise StateSnapshotContractError(
            "recovery observation intent does not match the accepted attempt intent"
        )
    if attempt.dagster_run_id.value != observation.execution_id:
        raise StateSnapshotContractError(
            "recovery observation execution does not match the accepted attempt run"
        )
    if observation.category is IctusObservationCategory.SUCCESS:
        raise StateSnapshotContractError(
            "recovery snapshot must not carry a success observation; "
            "success is applied before recovery"
        )


def _observation_facts(observation: TactusObservation) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = [
        {"key": FACT_OBSERVATION_CATEGORY, "value": observation.category.value}
    ]
    if observation.message is not None:
        facts.append({"key": FACT_OBSERVATION_MESSAGE, "value": observation.message})
    facts.append({"key": FACT_OBSERVATION_ID, "value": observation.observation_id})
    facts.append(
        {"key": FACT_OBSERVATION_EXECUTION_ID, "value": observation.execution_id}
    )
    facts.append({"key": FACT_OBSERVATION_INTENT_ID, "value": observation.intent_id})
    facts.append(
        {
            "key": FACT_OBSERVATION_OBSERVED_AT,
            "value": _to_rfc3339(observation.observed_at),
        }
    )
    if observation.retryable is not None:
        facts.append(
            {"key": FACT_OBSERVATION_RETRYABLE, "value": observation.retryable}
        )
    if observation.evidence:
        facts.append(
            {
                "key": FACT_OBSERVATION_EVIDENCE,
                "value": [_evidence_to_json(ref) for ref in observation.evidence],
            }
        )
    return facts


def _append_backend_facts(
    fact_entries: list[dict[str, Any]],
    backend_facts: BackendFacts | None,
) -> None:
    if backend_facts is None:
        descriptors: tuple[Any, ...] = ()
        statuses: tuple[Any, ...] = ()
        administrative_enablement: tuple[Any, ...] = ()
        quotas: tuple[Any, ...] = ()
        previous_backend: str | None = None
    else:
        if not isinstance(backend_facts, BackendFacts):
            raise StateSnapshotContractError("backend_facts must be a BackendFacts")
        descriptors = backend_facts.descriptors
        statuses = backend_facts.statuses
        administrative_enablement = backend_facts.administrative_enablement
        quotas = backend_facts.quotas
        previous_backend = backend_facts.previous_backend

    fact_entries.append(
        {
            "key": FACT_BACKEND_DESCRIPTORS,
            "value": [backend_descriptor_fact(descriptor) for descriptor in descriptors],
        }
    )
    fact_entries.append(
        {
            "key": FACT_BACKEND_STATUS,
            "value": [backend_status_fact(status) for status in statuses],
        }
    )
    if administrative_enablement:
        fact_entries.append(
            {
                "key": FACT_BACKEND_ADMINISTRATIVE_ENABLEMENT,
                "value": [entry.to_fact() for entry in administrative_enablement],
            }
        )
    fact_entries.append(
        {"key": FACT_BACKEND_QUOTA, "value": [backend_quota_fact(quota) for quota in quotas]}
    )
    if previous_backend is not None:
        fact_entries.append(
            {"key": FACT_BACKEND_PREVIOUS_BACKEND, "value": previous_backend}
        )


# -- digest / profile validation ------------------------------------------


def compute_snapshot_digest(payload: Mapping[str, Any]) -> str:
    """Compute the deterministic SHA-256 digest of a snapshot body.

    The digest is taken over the canonical JSON encoding (sorted keys, compact
    separators, UTF-8) of the payload. Callers must pass the body *without* the
    ``snapshot.digest`` fact; :func:`build_state_snapshot` does exactly that.
    """

    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_snapshot_profile(payload: Mapping[str, Any]) -> None:
    """Validate the versioned Tactus profile of an emitted snapshot payload.

    Fails closed for an unsupported generic ``schema_version`` or Tactus
    profile version, a malformed phase, a missing digest and a digest that does
    not match the canonical body. This is the consumer-side profile guard (for
    example a cross-repository conformance test), not decision policy.
    """

    if not isinstance(payload, Mapping):
        raise MalformedSnapshotError("state snapshot must be a mapping")

    version = payload.get("schema_version")
    if isinstance(version, bool) or version != SUPPORTED_SNAPSHOT_SCHEMA_VERSION:
        raise UnsupportedSnapshotVersionError(
            f"unsupported StateSnapshot schema_version: {version!r}; "
            f"supported is {SUPPORTED_SNAPSHOT_SCHEMA_VERSION}"
        )

    raw_facts = payload.get("facts")
    if not isinstance(raw_facts, list):
        raise MalformedSnapshotError("facts must be an array")
    fact_map: dict[str, Any] = {}
    for entry in raw_facts:
        if not isinstance(entry, Mapping) or "key" not in entry:
            raise MalformedSnapshotError("each fact must be an object with a key")
        fact_map[str(entry["key"])] = entry.get("value")

    profile_version = fact_map.get(FACT_PROFILE_VERSION)
    if profile_version != SUPPORTED_SNAPSHOT_PROFILE_VERSION:
        raise UnsupportedSnapshotProfileError(
            f"unsupported Tactus snapshot profile version: {profile_version!r}; "
            f"supported is {SUPPORTED_SNAPSHOT_PROFILE_VERSION}"
        )

    phase = fact_map.get(FACT_SNAPSHOT_PHASE)
    if not isinstance(phase, str) or phase not in {member.value for member in SnapshotPhase}:
        raise MalformedSnapshotError(
            f"snapshot.phase must be one of "
            f"{[member.value for member in SnapshotPhase]}, got {phase!r}"
        )

    digest = fact_map.get(FACT_SNAPSHOT_DIGEST)
    if not isinstance(digest, str) or not digest:
        raise MalformedSnapshotError("snapshot.digest must be a non-empty string")

    body = dict(payload)
    body["facts"] = [
        entry
        for entry in raw_facts
        if not (isinstance(entry, Mapping) and entry.get("key") == FACT_SNAPSHOT_DIGEST)
    ]
    if compute_snapshot_digest(body) != digest:
        raise MalformedSnapshotError("snapshot.digest does not match the canonical body")


# -- primitive helpers -----------------------------------------------------


def _require_non_empty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StateSnapshotContractError(f"{label} must be a non-empty string")
    return value.strip()


def _require_non_negative_int(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StateSnapshotContractError(f"{label} must be an integer")
    if value < 0:
        raise StateSnapshotContractError(f"{label} must be non-negative")
    return value


def _to_rfc3339(value: datetime) -> str:
    return to_rfc3339(value)


def _evidence_to_json(ref: EvidenceRef) -> dict[str, str]:
    payload = {"kind": ref.kind, "uri": ref.uri}
    if ref.sha256 is not None:
        payload["sha256"] = ref.sha256
    if ref.note is not None:
        payload["note"] = ref.note
    return payload


__all__ = [
    "DEFAULT_DOMAIN",
    "FACT_ATTEMPT_NUMBER",
    "FACT_AUTHORIZATION_GRANTS",
    "FACT_BACKEND_DESCRIPTORS",
    "FACT_BACKEND_ADMINISTRATIVE_ENABLEMENT",
    "FACT_BACKEND_PREVIOUS_BACKEND",
    "FACT_BACKEND_QUOTA",
    "FACT_BACKEND_STATUS",
    "FACT_CAPABILITY_ID",
    "FACT_DOMAIN_DEPENDENCIES_SATISFIED",
    "FACT_DOMAIN_SCOPE_CONSTRAINTS",
    "FACT_EXECUTION_STEP_RETRY_INDEX",
    "FACT_OBSERVATION_CATEGORY",
    "FACT_OBSERVATION_EVIDENCE",
    "FACT_OBSERVATION_EXECUTION_ID",
    "FACT_OBSERVATION_ID",
    "FACT_OBSERVATION_INTENT_ID",
    "FACT_OBSERVATION_MESSAGE",
    "FACT_OBSERVATION_OBSERVED_AT",
    "FACT_OBSERVATION_RETRYABLE",
    "FACT_PROFILE_VERSION",
    "FACT_RECOVERY_ATTEMPT_ID",
    "FACT_RECOVERY_INTENT_ID",
    "FACT_RECOVERY_MAX_SEMANTIC_ATTEMPTS",
    "FACT_RECOVERY_SEMANTIC_ATTEMPTS",
    "FACT_SNAPSHOT_DIGEST",
    "FACT_SNAPSHOT_PHASE",
    "FACT_SOURCE_REVISION",
    "FACT_WORK_ORDER_ID",
    "FACT_WORK_ORDER_READINESS",
    "FACT_WORK_ORDER_REVISION",
    "FACT_WORK_ORDER_STATE",
    "ICTUS_COMMIT",
    "STATE_SNAPSHOT_SCHEMA",
    "SUPPORTED_SNAPSHOT_PROFILE_VERSION",
    "SUPPORTED_SNAPSHOT_SCHEMA_VERSION",
    "WORK_ORDER_SUBJECT_TYPE",
    "AttemptHistory",
    "AuthorizationGrant",
    "BackendFacts",
    "MalformedSnapshotError",
    "SemanticBudget",
    "SnapshotPhase",
    "StateSnapshotContractError",
    "StepRetryDiagnostic",
    "UnsupportedSnapshotProfileError",
    "UnsupportedSnapshotVersionError",
    "build_state_snapshot",
    "compute_snapshot_digest",
    "semantic_budget_from_legacy",
    "validate_snapshot_profile",
]
