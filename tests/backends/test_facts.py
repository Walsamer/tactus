from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from tactus.backends import (
    BackendAdministrativeEnablementFact,
    BackendDescriptor,
    BackendDescriptorFact,
    BackendHealth,
    BackendHealthFact,
    BackendId,
    BackendProviderQuotaFact,
    BackendStatusObservation,
    ProviderCapacityObservation,
)

_T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(minutes=1)


def test_descriptor_fact_serializes_raw_descriptor_without_policy() -> None:
    descriptor = BackendDescriptor(
        BackendId("generic-a"),
        capabilities=frozenset({"git", "shell"}),
        agent_runtime="local",
        model="model-a",
        provider="provider-a",
        constraints=frozenset({"workspace:repo"}),
    )

    fact = BackendDescriptorFact.from_descriptor(
        descriptor,
        provenance="declaration",
        observed_at=_T0,
        expires_at=_T1,
    )

    assert fact.to_fact() == {
        "schema_version": 1,
        "backend_id": "generic-a",
        "capabilities": ["git", "shell"],
        "runtime": "local",
        "model": "model-a",
        "provider": "provider-a",
        "constraints": ["workspace:repo"],
        "provenance": "declaration",
        "observed_at": "2026-01-01T12:00:00Z",
        "expires_at": "2026-01-01T12:01:00Z",
    }


def test_fact_records_are_frozen_and_use_immutable_collections() -> None:
    fact = BackendDescriptorFact(
        BackendId("generic-a"), capabilities=("shell",), constraints=("scope",)
    )

    with pytest.raises(FrozenInstanceError):
        fact.provider = "changed"  # type: ignore[misc]
    assert isinstance(fact.capabilities, tuple)
    assert isinstance(fact.constraints, tuple)


def test_expired_health_serializes_as_explicit_unknown() -> None:
    observation = BackendStatusObservation(
        BackendId("generic-a"),
        BackendHealth.AVAILABLE,
        observed_at=_T0,
        expires_at=_T1,
        reason="probe",
    )

    fact = BackendHealthFact.from_observation(
        observation,
        at=_T1,
        provenance="probe-runner",
    )

    assert fact.to_fact()["health"] == "UNKNOWN"
    assert fact.to_fact()["observed_at"] == "2026-01-01T12:00:00Z"
    assert fact.to_fact()["expires_at"] == "2026-01-01T12:01:00Z"


def test_absent_health_serializes_as_explicit_unknown() -> None:
    fact = BackendHealthFact.from_observation(
        None, backend=BackendId("generic-a"), provenance="probe-runner"
    )

    assert fact.to_fact()["health"] == "UNKNOWN"
    assert fact.to_fact()["backend_id"] == "generic-a"


def test_administrative_enablement_is_independent_from_health() -> None:
    health = BackendHealthFact(
        BackendId("generic-a"), BackendHealth.AVAILABLE, provenance="probe"
    )
    disabled = BackendAdministrativeEnablementFact(
        BackendId("generic-a"), enabled=False, provenance="operator"
    )

    assert health.to_fact()["health"] == "AVAILABLE"
    assert disabled.to_fact()["enabled"] is False


def test_provider_quota_fact_does_not_expose_worker_slots() -> None:
    observation = ProviderCapacityObservation(
        BackendId("generic-a"),
        observed_at=_T0,
        limit=0,
        unit="requests_per_minute",
        source="provider_api",
    )

    fact = BackendProviderQuotaFact.from_observation(observation).to_fact()

    assert fact["limit"] == 0
    assert fact["provenance"] == "provider_api"
    for forbidden in ("in_use", "available", "slots", "occupied", "reserved"):
        assert forbidden not in fact
