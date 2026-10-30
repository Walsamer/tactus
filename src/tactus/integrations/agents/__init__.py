"""Tactus coding-agent adapters (fake-runtime canary only).

The adapter protocol is versioned in :mod:`tactus.integrations.agents.contract`
and implemented around the hardened runtime supervisor in
:mod:`tactus.integrations.agents.adapter`.  No real Pi/Codex/provider runtime is
launched here, and no publication authority or GitHub credential crosses the
adapter boundary.
"""

from tactus.integrations.agents.adapter import (
    AgentAdapterError,
    AgentContractViolation,
    AgentRunResult,
    CodingAgentAdapter,
    RuntimeAgentAdapter,
)
from tactus.integrations.agents.contract import (
    AGENT_ADAPTER_CONTRACT_ID,
    AGENT_ADAPTER_VERSION,
    AGENT_REQUEST_SCHEMA,
    AGENT_RESULT_SCHEMA,
    AGENT_RESULT_STATUSES,
    AgentAdapterContract,
)

__all__ = [
    "AGENT_ADAPTER_CONTRACT_ID",
    "AGENT_ADAPTER_VERSION",
    "AGENT_REQUEST_SCHEMA",
    "AGENT_RESULT_SCHEMA",
    "AGENT_RESULT_STATUSES",
    "AgentAdapterContract",
    "AgentAdapterError",
    "AgentContractViolation",
    "AgentRunResult",
    "CodingAgentAdapter",
    "RuntimeAgentAdapter",
]
