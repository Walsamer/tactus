"""Tactus <-> Ictus compatibility boundary.

The *only* public entry point for Ictus observation traffic. It validates the
versioned Ictus ``ExecutionObservation`` v1 contract and normalizes it into a
Tactus-side fact, and can originate an Ictus v1 payload from a Tactus failure
classification.

Ownership boundary (normative):

    Dagster / backend
        -> Ictus execution layer
        -> ExecutionResult
        -> Ictus-owned ExecutionResult.to_observation()
        -> ExecutionObservation v1
        -> this module (validate + normalize)
        -> Tactus normalized failure/recovery facts

Ictus owns execution semantics and the ``ExecutionResult`` ->
``ExecutionObservation`` transformation, including the Dagster adapter. Tactus
never duplicates that transformation; it only consumes and validates the wire
contract at its edge.
"""

from .observation import (
    ICTUS_COMMIT,
    SUPPORTED_SCHEMA_VERSION,
    EvidenceRef,
    IctusObservationCategory,
    MalformedObservationError,
    ObservationContractError,
    TactusFailureCategory,
    TactusObservation,
    UnknownObservationCategoryError,
    UnsupportedObservationVersionError,
    ictus_category_for,
    observation_from_failure,
    parse_observation,
    to_ictus_payload,
)

__all__ = [
    "ICTUS_COMMIT",
    "SUPPORTED_SCHEMA_VERSION",
    "EvidenceRef",
    "IctusObservationCategory",
    "MalformedObservationError",
    "ObservationContractError",
    "TactusFailureCategory",
    "TactusObservation",
    "UnknownObservationCategoryError",
    "UnsupportedObservationVersionError",
    "ictus_category_for",
    "observation_from_failure",
    "parse_observation",
    "to_ictus_payload",
]
