"""Backend facts: registry, status, and external provider-capacity facts.

These are factual, operational inputs to scheduling. Backend *selection* policy
(ranking, model/provider/effort choice, routing) belongs to the decision plane,
not this package. Execution concurrency (max concurrent runs, pool slots, run
queue) is Dagster-owned and deliberately not modelled here.
"""

from .capacity import ProviderCapacityModel, ProviderCapacityObservation
from .health import BackendHealth, BackendHealthModel, BackendStatusObservation
from .registry import (
    BackendDescriptor,
    BackendError,
    BackendId,
    BackendRegistry,
    BackendRequirements,
    DuplicateBackendError,
    EffortLevel,
    UnknownBackendError,
    coerce_backend_id,
)

__all__ = [
    "BackendDescriptor",
    "BackendError",
    "BackendHealth",
    "BackendHealthModel",
    "BackendId",
    "BackendRegistry",
    "BackendRequirements",
    "BackendStatusObservation",
    "DuplicateBackendError",
    "EffortLevel",
    "ProviderCapacityModel",
    "ProviderCapacityObservation",
    "UnknownBackendError",
    "coerce_backend_id",
]
