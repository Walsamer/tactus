"""Pure Tactus -> Ictus ``StateSnapshot`` v1 context adapter.

Pinned contract
---------------
* Ictus commit: ``833175d``
* Contract source: ``contracts/state-snapshot.schema.json`` (``StateSnapshot``)
* Supported ``schema_version``: ``1``

Ictus's ``DecisionProvider`` consumes a ``StateSnapshot``, not an
``ExecutionObservation`` directly. This module is the *outbound* sibling of the
inbound execution-observation boundary: it assembles one valid, domain-neutral
``StateSnapshot`` v1 payload from

* the already-validated execution observation (included verbatim, never
  reclassified),
* Tactus Work Order / domain facts,
* attempt and recovery history,
* backend availability facts,
* constraints / authorization tokens.

Ownership boundary (normative)::

    ExecutionObservation (inbound, validated by .observation)
            + WorkOrder / domain facts
            + attempt / recovery history
            + backend availability facts
            + constraints / authorization
            ↓
    Ictus StateSnapshot v1   (this module: assemble facts, decide nothing)
            ↓
    Ictus DecisionProvider

It does **not** decide anything: no retry/reroute/decompose/escalate policy, no
recovery-decision vocabulary, no Work Order mutation, no lifecycle transition,
no persistence. It reads the public, read-only views of the domain entity and
emits plain JSON values. Tactus persistence models are never placed in the
snapshot: only primitives, lists and mappings.

Two distinct attempt counters are represented separately and are never
conflated:

``attempt.number``
    The execution backend's own **Dagster micro-retry** attempt index. It is
    backend-owned (Dagster) and carried through verbatim as an opaque fact. It
    is never used to derive a semantic attempt count.

``recovery.semantic_attempts``
    Tactus's **semantic** execution-attempt / recovery count. It is the count of
    authoritative execution attempts / recovery cycles, which is the
    decision-relevant count. It is never replaced by the Dagster micro-retry
    index.

This separation is load-bearing: a Dagster step retry is not a new semantic
execution attempt.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from tactus.domain._clock import utcnow
from tactus.domain.work_order import WorkOrder

from .observation import ICTUS_COMMIT, EvidenceRef, TactusObservation

#: Ictus ``StateSnapshot`` contract version supported here.
SUPPORTED_SNAPSHOT_SCHEMA_VERSION = 1

#: Authoritative Ictus schema file this adapter conforms to.
STATE_SNAPSHOT_SCHEMA = "contracts/state-snapshot.schema.json"

#: Domain namespace used for the Tactus software-engineering control plane.
DEFAULT_DOMAIN = "software"

#: ``subject.type`` used for Tactus Work Orders.
WORK_ORDER_SUBJECT_TYPE = "work_order"

#: Dotted fact keys in the adapter's declared vocabulary. Kept as named
#: constants so consumers and tests share one source of truth.
FACT_OBSERVATION_CATEGORY = "observation.category"
FACT_OBSERVATION_MESSAGE = "observation.message"
FACT_OBSERVATION_ID = "observation.observation_id"
FACT_OBSERVATION_EXECUTION_ID = "observation.execution_id"
FACT_OBSERVATION_INTENT_ID = "observation.intent_id"
FACT_OBSERVATION_OBSERVED_AT = "observation.observed_at"
FACT_OBSERVATION_RETRYABLE = "observation.retryable"
FACT_OBSERVATION_EVIDENCE = "observation.evidence"
FACT_WORK_ORDER_ID = "work_order.id"
FACT_WORK_ORDER_STATE = "work_order.state"
FACT_WORK_ORDER_READINESS = "work_order.readiness"
FACT_ATTEMPT_NUMBER = "attempt.number"
FACT_RECOVERY_SEMANTIC_ATTEMPTS = "recovery.semantic_attempts"
FACT_CAPABILITY_ID = "capability.id"
FACT_BACKEND_AVAILABLE_CANDIDATES = "backend.available_candidates"
FACT_BACKEND_PREVIOUS_BACKEND = "backend.previous_backend"
FACT_DOMAIN_DEPENDENCIES_SATISFIED = "domain.dependencies_satisfied"
FACT_DOMAIN_SCOPE_CONSTRAINTS = "domain.scope_constraints"


class StateSnapshotContractError(ValueError):
    """An adapter input cannot produce a valid ``StateSnapshot`` v1 payload."""


@dataclass(frozen=True, slots=True)
class AttemptHistory:
    """The two distinct attempt counters, deliberately separate.

    ``dagster_micro_retry_attempt`` is the execution backend's micro-retry
    index (Dagster-owned) and is emitted as ``attempt.number``.
    ``semantic_attempts`` is Tactus's semantic execution-attempt / recovery
    count and is emitted as ``recovery.semantic_attempts``. They are never
    summed, substituted or conflated.
    """

    dagster_micro_retry_attempt: int = 0
    semantic_attempts: int = 0

    def __post_init__(self) -> None:
        for label, value in (
            ("dagster_micro_retry_attempt", self.dagster_micro_retry_attempt),
            ("semantic_attempts", self.semantic_attempts),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise StateSnapshotContractError(
                    f"{label} must be an integer"
                )
            if value < 0:
                raise StateSnapshotContractError(
                    f"{label} must be non-negative"
                )


@dataclass(frozen=True, slots=True)
class BackendFacts:
    """Backend availability facts consumed by the decision plane.

    Selection/ranking policy is not encoded here and never in the snapshot;
    this records only which backends are available as candidates and which
    backend (if any) was previously used.
    """

    available_candidates: tuple[str, ...] = ()
    previous_backend: str | None = None

    def __post_init__(self) -> None:
        candidates = tuple(self.available_candidates)
        for candidate in candidates:
            _require_non_empty(candidate, "backend candidate id")
        if self.previous_backend is not None:
            _require_non_empty(self.previous_backend, "previous_backend")
        object.__setattr__(self, "available_candidates", candidates)


def build_state_snapshot(
    *,
    work_order: WorkOrder,
    observation: TactusObservation,
    snapshot_id: str | None = None,
    timestamp: datetime | None = None,
    domain: str = DEFAULT_DOMAIN,
    subject_type: str = WORK_ORDER_SUBJECT_TYPE,
    capability_id: str | None = None,
    attempt_history: AttemptHistory | None = None,
    backend_facts: BackendFacts | None = None,
    dependencies_satisfied: bool | None = None,
    scope_constraints: Iterable[str] = (),
    constraints: Iterable[str] = (),
) -> dict[str, Any]:
    """Build one Ictus ``StateSnapshot`` v1 payload.

    Pure: it reads the Work Order's public views and returns a fresh
    JSON-serializable ``dict``. It never mutates the Work Order, never performs
    a lifecycle transition and takes no decision.

    The execution observation is included without reclassification: its
    ``category`` is copied verbatim (never mapped to a Tactus fact and never
    replaced by a domain concept). Tactus/domain facts are emitted as
    ``facts`` entries with their own keys and are never disguised as execution
    categories.
    """

    if not isinstance(work_order, WorkOrder):
        raise StateSnapshotContractError("work_order must be a WorkOrder")
    if not isinstance(observation, TactusObservation):
        raise StateSnapshotContractError(
            "observation must be a validated TactusObservation"
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
    facts: list[dict[str, Any]] = []

    # -- execution observation, verbatim (never reclassified) ------------
    facts.append(
        {"key": FACT_OBSERVATION_CATEGORY, "value": observation.category.value}
    )
    if observation.message is not None:
        facts.append({"key": FACT_OBSERVATION_MESSAGE, "value": observation.message})
    facts.append(
        {"key": FACT_OBSERVATION_ID, "value": observation.observation_id}
    )
    facts.append(
        {"key": FACT_OBSERVATION_EXECUTION_ID, "value": observation.execution_id}
    )
    facts.append(
        {"key": FACT_OBSERVATION_INTENT_ID, "value": observation.intent_id}
    )
    facts.append(
        {"key": FACT_OBSERVATION_OBSERVED_AT, "value": _to_rfc3339(observation.observed_at)}
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

    # -- Work Order domain facts -----------------------------------------
    facts.append({"key": FACT_WORK_ORDER_ID, "value": work_order.id.value})
    facts.append({"key": FACT_WORK_ORDER_STATE, "value": work_order.state.value})
    if work_order.readiness is not None:
        facts.append(
            {"key": FACT_WORK_ORDER_READINESS, "value": work_order.readiness.value}
        )

    # -- attempt / recovery history (two separate counters) --------------
    history = attempt_history if attempt_history is not None else AttemptHistory()
    facts.append(
        {
            "key": FACT_ATTEMPT_NUMBER,
            "value": history.dagster_micro_retry_attempt,
        }
    )
    facts.append(
        {
            "key": FACT_RECOVERY_SEMANTIC_ATTEMPTS,
            "value": history.semantic_attempts,
        }
    )

    # -- capability ------------------------------------------------------
    if capability_id is not None:
        resolved_capability = _require_non_empty(capability_id, "capability_id")
        facts.append({"key": FACT_CAPABILITY_ID, "value": resolved_capability})
        capabilities.append(resolved_capability)

    # -- backend availability facts --------------------------------------
    backends = backend_facts if backend_facts is not None else BackendFacts()
    facts.append(
        {
            "key": FACT_BACKEND_AVAILABLE_CANDIDATES,
            "value": list(backends.available_candidates),
        }
    )
    if backends.previous_backend is not None:
        facts.append(
            {"key": FACT_BACKEND_PREVIOUS_BACKEND, "value": backends.previous_backend}
        )

    # -- domain / control facts ------------------------------------------
    if dependencies_satisfied is not None:
        if not isinstance(dependencies_satisfied, bool):
            raise StateSnapshotContractError(
                "dependencies_satisfied must be a boolean when present"
            )
        facts.append(
            {
                "key": FACT_DOMAIN_DEPENDENCIES_SATISFIED,
                "value": dependencies_satisfied,
            }
        )
    facts.append(
        {
            "key": FACT_DOMAIN_SCOPE_CONSTRAINTS,
            "value": [_require_non_empty(item, "scope constraint") for item in scope_constraints],
        }
    )

    return {
        "schema_version": SUPPORTED_SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": resolved_snapshot_id,
        "timestamp": resolved_timestamp,
        "domain": resolved_domain,
        "subject": {"type": resolved_subject_type, "id": work_order.id.value},
        "facts": facts,
        "capabilities": capabilities,
        "constraints": [
            _require_non_empty(item, "constraint") for item in constraints
        ],
    }


# -- helpers --------------------------------------------------------------


def _require_non_empty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StateSnapshotContractError(f"{label} must be a non-empty string")
    return value.strip()


def _to_rfc3339(value: datetime) -> str:
    """Serialize a datetime as an RFC 3339 / ISO 8601 UTC instant.

    Naive datetimes are interpreted as UTC; the schema requires a ``date-time``.
    """

    if not isinstance(value, datetime):
        raise StateSnapshotContractError("timestamp must be a datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _evidence_to_json(ref: EvidenceRef) -> dict[str, str]:
    """Project an :class:`EvidenceRef` onto the v1 evidence wire shape.

    Optional provenance fields are omitted when absent, matching the schema's
    "optional means omitted" convention used by the observation boundary.
    """

    payload = {"kind": ref.kind, "uri": ref.uri}
    if ref.sha256 is not None:
        payload["sha256"] = ref.sha256
    if ref.note is not None:
        payload["note"] = ref.note
    return payload


__all__ = [
    "DEFAULT_DOMAIN",
    "FACT_ATTEMPT_NUMBER",
    "FACT_BACKEND_AVAILABLE_CANDIDATES",
    "FACT_BACKEND_PREVIOUS_BACKEND",
    "FACT_CAPABILITY_ID",
    "FACT_DOMAIN_DEPENDENCIES_SATISFIED",
    "FACT_DOMAIN_SCOPE_CONSTRAINTS",
    "FACT_OBSERVATION_CATEGORY",
    "FACT_OBSERVATION_EVIDENCE",
    "FACT_OBSERVATION_EXECUTION_ID",
    "FACT_OBSERVATION_ID",
    "FACT_OBSERVATION_INTENT_ID",
    "FACT_OBSERVATION_MESSAGE",
    "FACT_OBSERVATION_OBSERVED_AT",
    "FACT_OBSERVATION_RETRYABLE",
    "FACT_RECOVERY_SEMANTIC_ATTEMPTS",
    "FACT_WORK_ORDER_ID",
    "FACT_WORK_ORDER_READINESS",
    "FACT_WORK_ORDER_STATE",
    "ICTUS_COMMIT",
    "STATE_SNAPSHOT_SCHEMA",
    "SUPPORTED_SNAPSHOT_SCHEMA_VERSION",
    "WORK_ORDER_SUBJECT_TYPE",
    "AttemptHistory",
    "BackendFacts",
    "StateSnapshotContractError",
    "build_state_snapshot",
]
