"""Tactus <-> Ictus versioned-contract boundaries.

This package holds both directions of the Tactus/Ictus contract edge:

* the **inbound** ``ExecutionObservation`` v1 validation/translation boundary
  (:mod:`tactus.integrations.ictus.observation`): Ictus produces execution
  observations, Tactus validates and normalizes them; Tactus never fabricates
  or originates an ``ExecutionObservation``.
* the **outbound** ``StateSnapshot`` v1 context adapter
  (:mod:`tactus.integrations.ictus.state_snapshot`): Tactus assembles the
  domain-neutral state a ``DecisionProvider`` consumes. The adapter decides
  nothing; Ictus owns all decision policy.

Ownership boundary (normative)::

    Dagster / backend
        -> Ictus execution layer
        -> ExecutionResult
        -> Ictus-owned ExecutionResult.to_observation()
        -> ExecutionObservation v1
        -> this package (validate + normalize, fail-closed)
        -> Tactus normalized execution-observation fact
            + domain/control facts
            -> StateSnapshot v1 (pure adapter)
            -> Ictus DecisionProvider (decides)

Ictus owns execution semantics and the ``ExecutionResult`` ->
``ExecutionObservation`` transformation, including the Dagster adapter. Tactus
never duplicates that transformation; it only consumes and validates the wire
contract at its edge.

Execution facts flow through the inbound boundary. Domain/control facts
(dependency resolution, complexity/split decisions, supersession, scope
violations, backend capacity/health, ...) are not execution observations and are
represented on the ``StateSnapshot`` facts/context surface; they are never
disguised as execution observations.
"""

from .observation import (
    ICTUS_COMMIT,
    SUPPORTED_SCHEMA_VERSION,
    EvidenceRef,
    IctusObservationCategory,
    MalformedObservationError,
    ObservationContractError,
    TactusObservation,
    UnknownObservationCategoryError,
    UnsupportedObservationVersionError,
    parse_observation,
)
from .state_snapshot import (
    DEFAULT_DOMAIN,
    FACT_ATTEMPT_NUMBER,
    FACT_BACKEND_ADMINISTRATIVE_ENABLEMENT,
    FACT_BACKEND_DESCRIPTORS,
    FACT_BACKEND_HEALTH,
    FACT_BACKEND_PREVIOUS_BACKEND,
    FACT_BACKEND_PROVIDER_QUOTA,
    FACT_CAPABILITY_ID,
    FACT_DOMAIN_DEPENDENCIES_SATISFIED,
    FACT_DOMAIN_SCOPE_CONSTRAINTS,
    FACT_OBSERVATION_CATEGORY,
    FACT_OBSERVATION_EVIDENCE,
    FACT_OBSERVATION_EXECUTION_ID,
    FACT_OBSERVATION_ID,
    FACT_OBSERVATION_INTENT_ID,
    FACT_OBSERVATION_MESSAGE,
    FACT_OBSERVATION_OBSERVED_AT,
    FACT_OBSERVATION_RETRYABLE,
    FACT_RECOVERY_SEMANTIC_ATTEMPTS,
    FACT_WORK_ORDER_ID,
    FACT_WORK_ORDER_READINESS,
    FACT_WORK_ORDER_STATE,
    STATE_SNAPSHOT_SCHEMA,
    SUPPORTED_SNAPSHOT_SCHEMA_VERSION,
    WORK_ORDER_SUBJECT_TYPE,
    AttemptHistory,
    BackendFacts,
    StateSnapshotContractError,
    build_state_snapshot,
)

__all__ = [
    "DEFAULT_DOMAIN",
    "FACT_ATTEMPT_NUMBER",
    "FACT_BACKEND_ADMINISTRATIVE_ENABLEMENT",
    "FACT_BACKEND_DESCRIPTORS",
    "FACT_BACKEND_HEALTH",
    "FACT_BACKEND_PREVIOUS_BACKEND",
    "FACT_BACKEND_PROVIDER_QUOTA",
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
    "SUPPORTED_SCHEMA_VERSION",
    "SUPPORTED_SNAPSHOT_SCHEMA_VERSION",
    "WORK_ORDER_SUBJECT_TYPE",
    "AttemptHistory",
    "BackendFacts",
    "EvidenceRef",
    "IctusObservationCategory",
    "MalformedObservationError",
    "ObservationContractError",
    "StateSnapshotContractError",
    "TactusObservation",
    "UnknownObservationCategoryError",
    "UnsupportedObservationVersionError",
    "build_state_snapshot",
    "parse_observation",
]
