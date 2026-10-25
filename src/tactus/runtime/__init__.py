"""Bounded supervised runtime for one coding-agent process (Tactus-owned).

Owns process supervision only: a pinned runtime descriptor, an explicit
workspace and write scope, a deadline and cancellation, confirmed process-tree
termination, a writer lock that survives unconfirmed termination, and structured
evidence. It does not publish, does not hold credentials and is not a sandbox
(see :mod:`tactus.runtime.supervision`).
"""

from tactus.runtime.descriptor import (
    REQUEST_PLACEHOLDER,
    RuntimeDescriptor,
    RuntimePreflightError,
    RuntimeProvenance,
    preflight,
)
from tactus.runtime.supervision import (
    RecoveryReport,
    RuntimeRecoveryRequired,
    RuntimeContractError,
    RuntimeEvidence,
    RuntimeLimits,
    RuntimeRequest,
    RuntimeSupervisionError,
    RuntimeSupervisor,
    TerminationReport,
    WriterLock,
    WriterLockError,
    process_start_marker,
    terminate_process_group,
)
from tactus.runtime.workspace import (
    Workspace,
    WorkspaceScopeError,
    normalize_changed_path,
    patch_header_paths,
    validate_changed_paths,
)

__all__ = [
    "REQUEST_PLACEHOLDER",
    "RecoveryReport",
    "RuntimeRecoveryRequired",
    "RuntimeContractError",
    "RuntimeDescriptor",
    "RuntimeEvidence",
    "RuntimeLimits",
    "RuntimePreflightError",
    "RuntimeProvenance",
    "RuntimeRequest",
    "RuntimeSupervisionError",
    "RuntimeSupervisor",
    "TerminationReport",
    "Workspace",
    "WorkspaceScopeError",
    "WriterLock",
    "WriterLockError",
    "normalize_changed_path",
    "patch_header_paths",
    "preflight",
    "process_start_marker",
    "terminate_process_group",
    "validate_changed_paths",
]
