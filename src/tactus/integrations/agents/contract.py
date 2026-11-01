"""Explicit contract for a Tactus coding-agent adapter.

The runtime supervisor owns *process* supervision.  An adapter owns the narrow
translation between a Tactus attempt request and one pinned coding-agent
runtime: what the child is asked to do, how cancellation is expressed, which
artifacts must appear, and which terminal statuses the caller can observe.

The contract is data, not convention.  In particular:

* runtime selection stays explicit in the caller-supplied ``RuntimeDescriptor``;
  there is no discovery, no fallback list and no Ictus decision in this module;
* the adapter receives no publication authority and forwards no credentials;
* success claims are limited to what the supervisor and the fake-agent canary
  actually prove.  A real provider/model and an OS execution-permission boundary
  remain separate, explicitly recorded gates.

See :mod:`tactus.integrations.agents.adapter` for the implementation around a
hardened :class:`tactus.runtime.RuntimeSupervisor`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "AGENT_ADAPTER_CONTRACT_ID",
    "AGENT_ADAPTER_VERSION",
    "AGENT_REQUEST_SCHEMA",
    "AGENT_RESULT_SCHEMA",
    "AGENT_RESULT_STATUSES",
    "AgentAdapterContract",
]

#: Version of the adapter protocol itself.  Bump this when the request,
#: cancellation, artifact or status semantics change incompatibly.
AGENT_ADAPTER_VERSION = "1.0.0"

#: Stable identifier for the protocol family (independent of its version).
AGENT_ADAPTER_CONTRACT_ID = "tactus.coding-agent.adapter/v1"

#: The request file is the supervisor's bounded ``request.json``.
AGENT_REQUEST_SCHEMA = "tactus.runtime.request/v1"

#: The result file is the supervisor's bounded ``result.json`` schema.
AGENT_RESULT_SCHEMA = 1

#: Terminal statuses a caller must handle.  These are exactly the supervisor's
#: statuses, re-exported here so the adapter contract is testable on its own.
AGENT_RESULT_STATUSES = (
    "success",
    "failed",
    "timeout",
    "cancelled",
    "invalid",
    "unterminated",
)


@dataclass(frozen=True, slots=True)
class AgentAdapterContract:
    """Declarative description of the adapter boundary (versioned data)."""

    contract_id: str = AGENT_ADAPTER_CONTRACT_ID
    version: str = AGENT_ADAPTER_VERSION
    request_schema: str = AGENT_REQUEST_SCHEMA
    request_fields: tuple[str, ...] = ("attempt_id", "intent_id", "capability")
    result_schema: int = AGENT_RESULT_SCHEMA
    expected_artifacts: tuple[str, ...] = ("result.json", "patch.diff")
    result_statuses: tuple[str, ...] = AGENT_RESULT_STATUSES
    cancellation: str = (
        "cancel(reason) sets a supervisor cancellation; the supervisor sends "
        "SIGTERM then SIGKILL to the whole process group and confirms termination"
    )
    recovery: str = (
        "a leftover attempt is settled only by verified process identity; an "
        "unresolved attempt blocks new writers until an operator resolves it"
    )
    runtime_selection: str = (
        "explicit caller-supplied RuntimeDescriptor; no fallback, no discovery, "
        "no Ictus routing decision"
    )
    credential_environment: tuple[str, ...] = ()
    publication_authority: bool = False
    policy_owner: str = "ictus"
    remaining_gates: tuple[str, ...] = (
        "real coding-agent provider and model selection require an approved Work Order",
        "OS execution-permission boundary (filesystem, network and credential "
        "containment) is unresolved; a Git worktree does not provide it",
    )

    def __post_init__(self) -> None:
        for name in (
            "contract_id",
            "version",
            "request_schema",
            "cancellation",
            "recovery",
            "runtime_selection",
            "policy_owner",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"agent adapter contract {name} must be a non-empty string")
        if not self.request_fields:
            raise ValueError("agent adapter contract must declare request fields")
        if not self.expected_artifacts:
            raise ValueError("agent adapter contract must declare expected artifacts")
        if not self.result_statuses:
            raise ValueError("agent adapter contract must declare result statuses")
        for label, values in (
            ("request_fields", self.request_fields),
            ("expected_artifacts", self.expected_artifacts),
            ("result_statuses", self.result_statuses),
            ("credential_environment", self.credential_environment),
            ("remaining_gates", self.remaining_gates),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"agent adapter contract {label} contains duplicates")
        if self.result_schema < 1:
            raise ValueError("agent adapter contract result_schema must be at least 1")
        if self.publication_authority:
            raise ValueError("a coding-agent adapter may not carry publication authority")
        if self.credential_environment:
            raise ValueError("a coding-agent adapter may not forward credentials")

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "version": self.version,
            "request_schema": self.request_schema,
            "request_fields": list(self.request_fields),
            "result_schema": self.result_schema,
            "expected_artifacts": list(self.expected_artifacts),
            "result_statuses": list(self.result_statuses),
            "cancellation": self.cancellation,
            "recovery": self.recovery,
            "runtime_selection": self.runtime_selection,
            "credential_environment": list(self.credential_environment),
            "publication_authority": self.publication_authority,
            "policy_owner": self.policy_owner,
            "remaining_gates": list(self.remaining_gates),
        }
