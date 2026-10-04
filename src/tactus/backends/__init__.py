"""Backend facts: registry, status and capacity.

These are factual, operational inputs to scheduling. Backend *selection*
policy (ranking, model/provider/effort choice, routing) belongs to the decision
plane, not this package.
"""

from .capacity import Capacity, CapacityView
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
    "Capacity",
    "CapacityView",
    "DuplicateBackendError",
    "EffortLevel",
    "UnknownBackendError",
    "coerce_backend_id",
]
