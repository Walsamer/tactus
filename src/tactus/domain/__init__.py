"""Tactus domain model.

Pure value types and entities for the Work Order lifecycle, its dependency
graph, and the execution-admission boundary. This package deliberately has no
I/O, framework or persistence dependencies so that the core invariants stay
portable (including a possible future Rust implementation of a
reliability-critical component).
"""

from .admission import (
    AdmissionDecision,
    AdmissionError,
    AdmissionFacts,
    DagsterRunId,
    DuplicateExecutionAttemptError,
    ExecutionAdmission,
    ExecutionAdmissionPort,
    ExecutionAttempt,
    ExecutionAttemptId,
    ExecutionIntentId,
    ExecutionRequest,
    ExecutionRequestMismatchError,
    IneligibleWorkOrderError,
    UnknownExecutionAttemptError,
)
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
    "AdmissionDecision",
    "AdmissionError",
    "AdmissionFacts",
    "CyclicDependencyError",
    "DagsterRunId",
    "DependencyChangeRecord",
    "DependencyError",
    "DependencyGraph",
    "DuplicateDependencyError",
    "DuplicateExecutionAttemptError",
    "ExecutionAdmission",
    "ExecutionAdmissionPort",
    "ExecutionAttempt",
    "ExecutionAttemptId",
    "ExecutionIntentId",
    "ExecutionRequest",
    "ExecutionRequestMismatchError",
    "FailureObservation",
    "IllegalTransitionError",
    "IneligibleWorkOrderError",
    "InvalidReadinessError",
    "MissingDependencyError",
    "OpenStatus",
    "SelfDependencyError",
    "TransitionAuthority",
    "TransitionRecord",
    "UnknownExecutionAttemptError",
    "WorkOrder",
    "WorkOrderDependency",
    "WorkOrderId",
    "WorkOrderState",
]
