"""Tactus application services.

Application services compose the pure domain with a work-source adapter and a
persistence port. They own orchestration and idempotent import, not policy
(GitHub intent) or persistence mechanics (SQLite).
"""

from .decomposition import (
    ChildMaterializationStore,
    ChildMaterializer,
    InMemoryMaterializationStore,
    LedgerConflictError,
    MaterializationError,
    MaterializationEvidence,
    MaterializationOutcome,
    MaterializedGeneration,
    executable_children,
)
from .execution_cycle import (
    AcceptedExecution,
    CorrelatedObservation,
    DurableWorkOrderStatePort,
    ExecutionCycle,
    ExecutionCycleAdmissionPort,
    ExecutionCycleError,
    IntentDecisionError,
    ObservationApplication,
    ObservationCorrelationError,
    RecoveryApplication,
    ValidatedExecutionIntent,
    WorkOrderProjectionError,
    canonical_digest,
    observation_payload,
    parse_execution_decision,
)
from .import_issue import (
    ImportResult,
    apply_source_holds,
    import_issue,
    recheck_source,
)

__all__ = [
    "AcceptedExecution",
    "ChildMaterializationStore",
    "ChildMaterializer",
    "CorrelatedObservation",
    "DurableWorkOrderStatePort",
    "ExecutionCycle",
    "ExecutionCycleAdmissionPort",
    "ExecutionCycleError",
    "ImportResult",
    "InMemoryMaterializationStore",
    "IntentDecisionError",
    "LedgerConflictError",
    "MaterializationError",
    "MaterializationEvidence",
    "MaterializationOutcome",
    "MaterializedGeneration",
    "ObservationApplication",
    "ObservationCorrelationError",
    "RecoveryApplication",
    "ValidatedExecutionIntent",
    "WorkOrderProjectionError",
    "apply_source_holds",
    "canonical_digest",
    "executable_children",
    "import_issue",
    "observation_payload",
    "parse_execution_decision",
    "recheck_source",
]
