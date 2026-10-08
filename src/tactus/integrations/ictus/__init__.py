"""Tactus <-> Ictus versioned-contract boundaries.

This package holds both directions of the Tactus/Ictus contract edge:

* the **inbound** ``ExecutionObservation`` v1 validation/translation boundary
  (:mod:`tactus.integrations.ictus.observation`): Ictus produces execution
  observations, Tactus validates and normalizes them; Tactus never fabricates
  or originates an ``ExecutionObservation``.
* the **outbound** versioned ``StateSnapshot`` context adapter
  (:mod:`tactus.integrations.ictus.state_snapshot`, with domain fact adapters in
  :mod:`tactus.integrations.ictus.facts`): Tactus assembles the domain-neutral
  state a ``DecisionProvider`` consumes for first execution (``INITIAL``) and
  recovery (``RECOVERY``). The adapter decides nothing; Ictus owns all policy.

Ownership boundary (normative)::

    Dagster / backend
        -> Ictus execution layer
        -> ExecutionResult
        -> Ictus-owned ExecutionResult.to_observation()
        -> ExecutionObservation v1
        -> this package (validate + normalize, fail-closed)
        -> Tactus normalized execution-observation fact
            + domain/control facts
            -> versioned StateSnapshot profile (pure adapter)
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

from .facts import (
    LEGACY_FACT_ATTEMPT_NUMBER,
    LEGACY_FACT_RETRY_ATTEMPT,
    LEGACY_FACT_RETRY_BUDGET,
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
    authorization_grant_fact,
    backend_descriptor_fact,
    backend_quota_fact,
    backend_status_fact,
    translate_legacy_budget_facts,
)
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
    FACT_AUTHORIZATION_GRANTS,
    FACT_BACKEND_DESCRIPTORS,
    FACT_BACKEND_PREVIOUS_BACKEND,
    FACT_BACKEND_QUOTA,
    FACT_BACKEND_STATUS,
    FACT_CAPABILITY_ID,
    FACT_DOMAIN_DEPENDENCIES_SATISFIED,
    FACT_DOMAIN_SCOPE_CONSTRAINTS,
    FACT_EXECUTION_STEP_RETRY_INDEX,
    FACT_OBSERVATION_CATEGORY,
    FACT_OBSERVATION_EVIDENCE,
    FACT_OBSERVATION_EXECUTION_ID,
    FACT_OBSERVATION_ID,
    FACT_OBSERVATION_INTENT_ID,
    FACT_OBSERVATION_MESSAGE,
    FACT_OBSERVATION_OBSERVED_AT,
    FACT_OBSERVATION_RETRYABLE,
    FACT_PROFILE_VERSION,
    FACT_RECOVERY_ATTEMPT_ID,
    FACT_RECOVERY_INTENT_ID,
    FACT_RECOVERY_MAX_SEMANTIC_ATTEMPTS,
    FACT_RECOVERY_SEMANTIC_ATTEMPTS,
    FACT_SNAPSHOT_DIGEST,
    FACT_SNAPSHOT_PHASE,
    FACT_SOURCE_REVISION,
    FACT_WORK_ORDER_ID,
    FACT_WORK_ORDER_READINESS,
    FACT_WORK_ORDER_REVISION,
    FACT_WORK_ORDER_STATE,
    STATE_SNAPSHOT_SCHEMA,
    SUPPORTED_SNAPSHOT_SCHEMA_VERSION,
    WORK_ORDER_SUBJECT_TYPE,
    AttemptHistory,
    build_state_snapshot,
    compute_snapshot_digest,
    semantic_budget_from_legacy,
    validate_snapshot_profile,
)

__all__ = [
    "DEFAULT_DOMAIN",
    "FACT_ATTEMPT_NUMBER",
    "FACT_AUTHORIZATION_GRANTS",
    "FACT_BACKEND_DESCRIPTORS",
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
    "LEGACY_FACT_ATTEMPT_NUMBER",
    "LEGACY_FACT_RETRY_ATTEMPT",
    "LEGACY_FACT_RETRY_BUDGET",
    "STATE_SNAPSHOT_SCHEMA",
    "SUPPORTED_SCHEMA_VERSION",
    "SUPPORTED_SNAPSHOT_PROFILE_VERSION",
    "SUPPORTED_SNAPSHOT_SCHEMA_VERSION",
    "WORK_ORDER_SUBJECT_TYPE",
    "AttemptHistory",
    "AuthorizationGrant",
    "BackendFacts",
    "EvidenceRef",
    "IctusObservationCategory",
    "MalformedObservationError",
    "MalformedSnapshotError",
    "ObservationContractError",
    "SemanticBudget",
    "SnapshotPhase",
    "StateSnapshotContractError",
    "StepRetryDiagnostic",
    "TactusObservation",
    "UnknownObservationCategoryError",
    "UnsupportedObservationVersionError",
    "UnsupportedSnapshotProfileError",
    "UnsupportedSnapshotVersionError",
    "authorization_grant_fact",
    "backend_descriptor_fact",
    "backend_quota_fact",
    "backend_status_fact",
    "build_state_snapshot",
    "compute_snapshot_digest",
    "parse_observation",
    "semantic_budget_from_legacy",
    "translate_legacy_budget_facts",
    "validate_snapshot_profile",
]
