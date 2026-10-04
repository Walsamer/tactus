"""Architecture guards for backend facts.

These tests encode the frozen ownership boundary at the code level:

* ``BackendRegistry.compatible()`` stays a factual capability matcher and is
  independent of status and provider-capacity facts;
* no Tactus source owns execution slots / a dynamic execution-slot scheduler —
  execution concurrency is Dagster's.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import datetime, timezone
from pathlib import Path

import tactus
import tactus.backends as backends
from tactus.backends import (
    BackendDescriptor,
    BackendHealth,
    BackendHealthModel,
    BackendId,
    BackendRegistry,
    BackendRequirements,
    BackendStatusObservation,
    ProviderCapacityModel,
    ProviderCapacityObservation,
)

_T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

# Identifiers that would indicate Tactus-owned execution-slot scheduling.
_FORBIDDEN_EXECUTION_SLOT_NAMES = frozenset(
    {
        "in_use",
        "available_slots",
        "free_slots",
        "worker_slots",
        "slot_scheduler",
        "CapacityView",
        "ExecutionSlot",
        "SlotScheduler",
    }
)


def test_compatible_ignores_status_and_capacity() -> None:
    backend_id = BackendId("generic-a")
    registry = BackendRegistry(
        [BackendDescriptor(backend_id, capabilities=frozenset({"shell"}))]
    )
    health = BackendHealthModel()
    health.record(
        BackendStatusObservation(backend_id, BackendHealth.DISABLED, observed_at=_T0)
    )
    capacity = ProviderCapacityModel()
    capacity.record(
        ProviderCapacityObservation(backend_id, observed_at=_T0, limit=0)
    )

    requirements = BackendRequirements(required_capabilities=frozenset({"shell"}))

    # Disabled status and a zero provider-reported limit change nothing about
    # factual compatibility.
    assert [d.backend_id for d in registry.compatible(requirements)] == [backend_id]


def test_compatible_returns_all_matches_not_a_ranked_pick() -> None:
    registry = BackendRegistry(
        [
            BackendDescriptor(BackendId("generic-a"), capabilities=frozenset({"shell"})),
            BackendDescriptor(BackendId("generic-b"), capabilities=frozenset({"shell"})),
        ]
    )

    compatible = registry.compatible(
        BackendRequirements(required_capabilities=frozenset({"shell"}))
    )

    # No ranking or route choice: every factual match is returned in
    # deterministic order for the decision plane (Ictus) to choose from.
    assert {d.backend_id.value for d in compatible} == {"generic-a", "generic-b"}


def test_public_api_exposes_provider_capacity_not_execution_slots() -> None:
    public = set(backends.__all__)

    assert "ProviderCapacityObservation" in public
    assert "ProviderCapacityModel" in public
    assert "Capacity" not in public
    assert "CapacityView" not in public


def test_backends_api_has_no_ranking_or_route_choice() -> None:
    """Backend selection policy belongs to Ictus, not this package."""

    for name in backends.__all__:
        lowered = name.lower()
        for banned in ("rank", "select", "choose", "best", "prefer"):
            assert banned not in lowered, (
                f"tactus.backends must not expose selection policy: {name}"
            )


def test_provider_capacity_observation_is_frozen() -> None:
    assert dataclasses.is_dataclass(ProviderCapacityObservation)
    assert ProviderCapacityObservation.__dataclass_params__.frozen


def test_no_tactus_source_owns_execution_slots() -> None:
    """No Tactus module defines or references execution-slot scheduling names."""

    package_root = Path(tactus.__file__).resolve().parent
    offenders: list[str] = []

    for module_path in sorted(package_root.rglob("*.py")):
        tree = ast.parse(module_path.read_text(), filename=str(module_path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name)
            elif isinstance(node, ast.arg):
                names.append(node.arg)
            elif isinstance(node, ast.keyword) and node.arg is not None:
                names.append(node.arg)
            elif isinstance(node, ast.alias):
                names.append(node.asname or node.name.split(".")[-1])
            for name in names:
                if name in _FORBIDDEN_EXECUTION_SLOT_NAMES:
                    offenders.append(f"{module_path.name}:{node.lineno}: {name}")

    assert offenders == [], (
        "Tactus must not own execution slots; found: " + ", ".join(offenders)
    )
