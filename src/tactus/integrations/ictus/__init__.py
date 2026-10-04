"""Inbound Tactus <- Ictus execution-observation compatibility boundary.

The *only* public entry point for Ictus observation traffic. It validates the
versioned Ictus ``ExecutionObservation`` v1 contract and normalizes it into a
Tactus-side fact. There is deliberately no outbound direction: Tactus never
originates or fabricates an ``ExecutionObservation``.

Ownership boundary (normative)::

    Dagster / backend
        -> Ictus execution layer
        -> ExecutionResult
        -> Ictus-owned ExecutionResult.to_observation()
        -> ExecutionObservation v1
        -> this module (validate + normalize, fail-closed)
        -> Tactus normalized execution-observation fact

Ictus owns execution semantics and the ``ExecutionResult`` ->
``ExecutionObservation`` transformation, including the Dagster adapter. Tactus
never duplicates that transformation; it only consumes and validates the wire
contract at its edge.

Execution facts flow through this boundary. Domain/control facts (dependency
resolution, complexity/split decisions, supersession, scope violations, backend
capacity/health, ...) are not execution observations and belong to the
StateSnapshot facts/context surface (FIX-005); they must not be disguised as
execution observations.
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

__all__ = [
    "ICTUS_COMMIT",
    "SUPPORTED_SCHEMA_VERSION",
    "EvidenceRef",
    "IctusObservationCategory",
    "MalformedObservationError",
    "ObservationContractError",
    "TactusObservation",
    "UnknownObservationCategoryError",
    "UnsupportedObservationVersionError",
    "parse_observation",
]
