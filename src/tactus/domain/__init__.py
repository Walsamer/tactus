"""Tactus domain model.

Pure value types and entities for the Work Order lifecycle and its dependency
graph. This package deliberately has no I/O, framework or persistence
dependencies so that the core invariants stay portable (including a possible
future Rust implementation of a reliability-critical component).
"""

from .dependency import (
    CyclicDependencyError,
    DependencyChangeRecord,
    DependencyError,
    DependencyGraph,
    DuplicateDependencyError,
    MissingDependencyError,
    SelfDependencyError,
    WorkOrderDependency,
)
from .observation import FailureObservation
from .records import TransitionAuthority, TransitionRecord
from .work_order import (
    IllegalTransitionError,
    InvalidReadinessError,
    OpenStatus,
    WorkOrder,
    WorkOrderId,
    WorkOrderState,
)

__all__ = [
    "CyclicDependencyError",
    "DependencyChangeRecord",
    "DependencyError",
    "DependencyGraph",
    "DuplicateDependencyError",
    "FailureObservation",
    "IllegalTransitionError",
    "InvalidReadinessError",
    "MissingDependencyError",
    "OpenStatus",
    "SelfDependencyError",
    "TransitionAuthority",
    "TransitionRecord",
    "WorkOrder",
    "WorkOrderDependency",
    "WorkOrderId",
    "WorkOrderState",
]
