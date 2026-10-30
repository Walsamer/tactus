"""Coding-agent adapter around the hardened :class:`RuntimeSupervisor`.

This module adds *contract* and *result shape*, not process semantics.  The
supervisor still owns preflight, the writer lock, deadlines, cancellation,
process-tree termination, artifact bounds and restart recovery.  The adapter:

1. accepts one explicit :class:`tactus.runtime.RuntimeRequest`;
2. delegates the bounded run to the hardened supervisor;
3. checks that the observed evidence really satisfies the adapter contract;
4. returns the patch digest and changed paths together with the full evidence.

It never selects a runtime (the descriptor is caller-supplied), never applies a
policy decision, and never forwards a credential or publication authority.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from tactus.integrations.agents.contract import AgentAdapterContract
from tactus.runtime.supervision import (
    PATCH_FILE,
    RecoveryReport,
    RuntimeEvidence,
    RuntimeRequest,
    RuntimeSupervisor,
)

__all__ = [
    "AgentAdapterError",
    "AgentContractViolation",
    "AgentRunResult",
    "CodingAgentAdapter",
    "RuntimeAgentAdapter",
]


class AgentAdapterError(RuntimeError):
    """Base class for adapter-level contract failures."""


class AgentContractViolation(AgentAdapterError):
    """Supervisor evidence is internally inconsistent with the adapter contract."""


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """Bounded, machine-checkable outcome of one adapter run.

    ``patch_sha256`` is the digest of the exact patch bytes the supervisor
    recorded, and ``changed_paths`` are the declared paths it validated.  The
    raw supervisor evidence remains available for audit.
    """

    contract_version: str
    status: str
    attempt_id: str
    summary: str
    changed_paths: tuple[str, ...]
    patch_sha256: str
    patch_bytes: int
    patch_path: str
    publication_authority: bool
    errors: tuple[str, ...]
    evidence: RuntimeEvidence

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "status": self.status,
            "attempt_id": self.attempt_id,
            "summary": self.summary,
            "changed_paths": list(self.changed_paths),
            "patch_sha256": self.patch_sha256,
            "patch_bytes": self.patch_bytes,
            "patch_path": self.patch_path,
            "publication_authority": self.publication_authority,
            "errors": list(self.errors),
            "evidence": self.evidence.as_dict(),
        }


@runtime_checkable
class CodingAgentAdapter(Protocol):
    """Versioned adapter surface a coding-agent integration must implement."""

    @property
    def contract(self) -> AgentAdapterContract:
        ...

    def run(self, request: RuntimeRequest) -> AgentRunResult:
        ...

    def cancel(self, reason: str = "operator cancelled the attempt") -> None:
        ...

    def recover(self) -> RecoveryReport:
        ...

    def resolve_unresolved(self, *, operator: str, reason: str) -> RecoveryReport:
        ...


class RuntimeAgentAdapter:
    """One coding-agent adapter bound to one explicit runtime supervisor."""

    def __init__(
        self,
        supervisor: RuntimeSupervisor,
        *,
        contract: AgentAdapterContract | None = None,
    ) -> None:
        if not isinstance(supervisor, RuntimeSupervisor):
            raise TypeError("a RuntimeSupervisor is required")
        self._supervisor = supervisor
        self._contract = contract or AgentAdapterContract()

    @property
    def contract(self) -> AgentAdapterContract:
        return self._contract

    @property
    def supervisor(self) -> RuntimeSupervisor:
        return self._supervisor

    def cancel(self, reason: str = "operator cancelled the attempt") -> None:
        """Request cancellation through the supervisor's hardened path."""
        self._supervisor.cancel(reason)

    def recover(self) -> RecoveryReport:
        """Settle a leftover attempt using verified process identity only."""
        return self._supervisor.recover()

    def resolve_unresolved(self, *, operator: str, reason: str) -> RecoveryReport:
        """Record the explicit operator decision required by a fail-closed hold."""
        return self._supervisor.resolve_unresolved(operator=operator, reason=reason)

    def run(self, request: RuntimeRequest) -> AgentRunResult:
        """Supervise one attempt and return a contract-checked result."""
        if not isinstance(request, RuntimeRequest):
            raise AgentAdapterError("a RuntimeRequest is required")
        evidence = self._supervisor.run(request)
        self._validate_evidence(evidence)
        patch_path = ""
        if evidence.status == "success":
            patch_path = str(
                Path(evidence.attempt_dir) / "output" / PATCH_FILE
            )
        return AgentRunResult(
            contract_version=self._contract.version,
            status=evidence.status,
            attempt_id=request.attempt_id,
            summary=evidence.agent_summary,
            changed_paths=evidence.declared_paths,
            patch_sha256=evidence.patch_sha256,
            patch_bytes=evidence.patch_bytes,
            patch_path=patch_path,
            publication_authority=self._contract.publication_authority,
            errors=evidence.errors,
            evidence=evidence,
        )

    def _validate_evidence(self, evidence: RuntimeEvidence) -> None:
        contract = self._contract
        if evidence.status not in contract.result_statuses:
            raise AgentContractViolation(
                f"supervisor returned {evidence.status!r}, which is not one of "
                f"{list(contract.result_statuses)}"
            )
        if evidence.status != "success":
            return
        if evidence.agent_status != "completed":
            raise AgentContractViolation(
                "a successful adapter run must carry agent_status='completed'"
            )
        if not evidence.declared_paths:
            raise AgentContractViolation("a successful adapter run must report changed paths")
        if len(evidence.declared_paths) > evidence.limits.max_changed_paths:
            raise AgentContractViolation(
                f"successful run reported {len(evidence.declared_paths)} changed paths; "
                f"limit is {evidence.limits.max_changed_paths}"
            )
        if evidence.patch_bytes < 1:
            raise AgentContractViolation("a successful adapter run must report a patch")
        if evidence.patch_bytes > evidence.limits.max_patch_bytes:
            raise AgentContractViolation(
                f"patch is {evidence.patch_bytes} bytes; limit is "
                f"{evidence.limits.max_patch_bytes}"
            )
        if not evidence.patch_sha256:
            raise AgentContractViolation("a successful adapter run must report a patch digest")
        raw = self._read_patch(evidence)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != evidence.patch_sha256:
            raise AgentContractViolation(
                "reported patch digest does not match the patch artifact"
            )
        if len(raw) != evidence.patch_bytes:
            raise AgentContractViolation(
                "reported patch size does not match the patch artifact"
            )
        undeclared = [path for path in evidence.patch_paths if path not in evidence.declared_paths]
        if undeclared:
            raise AgentContractViolation(
                "patch touches undeclared paths: " + ", ".join(undeclared)
            )

    @staticmethod
    def _read_patch(evidence: RuntimeEvidence) -> bytes:
        patch_path = Path(evidence.attempt_dir) / "output" / PATCH_FILE
        limit = evidence.limits.max_patch_bytes
        try:
            with patch_path.open("rb") as handle:
                raw = handle.read(limit + 1)
        except OSError as exc:
            raise AgentContractViolation(f"patch artifact is unreadable: {exc}") from exc
        if len(raw) > limit:
            raise AgentContractViolation(
                f"patch artifact exceeds the {limit}-byte limit"
            )
        return raw
