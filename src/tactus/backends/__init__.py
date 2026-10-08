"""Backend facts: registry, status, and external provider-capacity facts.

These are factual, operational inputs to scheduling. Backend *selection* policy
(ranking, model/provider/effort choice, routing) belongs to the decision plane,
not this package. Execution concurrency (max concurrent runs, pool slots, run
queue) is Dagster-owned and deliberately not modelled here.
"""

from .capacity import ProviderCapacityModel, ProviderCapacityObservation
from .facts import (
    BACKEND_FACT_SCHEMA_VERSION,
    FACT_BACKEND_ADMINISTRATIVE_ENABLEMENT,
    FACT_BACKEND_DESCRIPTORS,
    FACT_BACKEND_HEALTH,
    FACT_BACKEND_PROVIDER_QUOTA,
    UNKNOWN_HEALTH,
    BackendAdministrativeEnablementFact,
    BackendDescriptorFact,
    BackendHealthFact,
    BackendProviderQuotaFact,
)
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
    "BACKEND_FACT_SCHEMA_VERSION",
    "BackendAdministrativeEnablementFact",
    "BackendDescriptorFact",
    "BackendError",
    "BackendHealth",
    "BackendHealthFact",
    "BackendHealthModel",
    "BackendId",
    "BackendRegistry",
    "BackendRequirements",
    "BackendProviderQuotaFact",
    "BackendStatusObservation",
    "DuplicateBackendError",
    "EffortLevel",
    "FACT_BACKEND_ADMINISTRATIVE_ENABLEMENT",
    "FACT_BACKEND_DESCRIPTORS",
    "FACT_BACKEND_HEALTH",
    "FACT_BACKEND_PROVIDER_QUOTA",
    "ProviderCapacityModel",
    "ProviderCapacityObservation",
    "UNKNOWN_HEALTH",
    "UnknownBackendError",
    "coerce_backend_id",
]
