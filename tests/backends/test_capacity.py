from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timedelta, timezone

import pytest

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
_T1 = _T0 + timedelta(minutes=1)
_REQUIREMENTS = BackendRequirements(required_capabilities=frozenset({"shell"}))


def _observation(
    backend: BackendId,
    limit: int | None,
    *,
    observed_at: datetime = _T0,
    expires_at: datetime | None = None,
) -> ProviderCapacityObservation:
    return ProviderCapacityObservation(
        backend=backend,
        observed_at=observed_at,
        limit=limit,
        unit="requests_per_minute",
        source="provider_api",
        expires_at=expires_at,
    )


def test_observation_validation_rejects_negative_limit() -> None:
    with pytest.raises(ValueError):
        _observation(BackendId("generic-a"), -1)


def test_known_zero_capacity_is_distinct_from_unknown() -> None:
    known_zero = ProviderCapacityModel()
    known_zero.record(_observation(BackendId("generic-a"), 0))
    unknown = ProviderCapacityModel()

    assert known_zero.effective_capacity("generic-a", at=_T0).limit == 0
    assert unknown.effective_capacity("generic-a", at=_T0) is None


def test_provider_reported_limit_is_a_fact_not_an_execution_slot() -> None:
    """The observation records what the provider reports, with no occupancy."""

    model = ProviderCapacityModel()
    model.record(_observation(BackendId("generic-a"), 8))

    observation = model.effective_capacity("generic-a", at=_T0)
    assert observation.limit == 8
    assert observation.unit == "requests_per_minute"
    assert observation.source == "provider_api"


def test_observation_has_no_execution_slot_occupancy_fields() -> None:
    """Tactus must not model execution-slot occupancy (that is Dagster's)."""

    field_names = {field.name for field in fields(ProviderCapacityObservation)}

    assert "limit" in field_names
    for forbidden in ("in_use", "available", "slots", "occupied", "reserved"):
        assert forbidden not in field_names


def test_expiry_is_evaluated_at_query_time_and_history_is_retained() -> None:
    model = ProviderCapacityModel()
    observation = _observation(BackendId("generic-a"), 5, expires_at=_T1)
    model.record(observation)

    assert model.effective_capacity("generic-a", at=_T0) is observation
    assert model.effective_capacity("generic-a", at=_T1) is None
    assert model.history("generic-a") == (observation,)


def test_latest_observation_wins_and_older_is_kept_as_history() -> None:
    model = ProviderCapacityModel()
    newer = _observation(BackendId("generic-a"), 3, observed_at=_T0 + timedelta(minutes=5))
    older = _observation(BackendId("generic-a"), 9, observed_at=_T0)
    model.record(newer)
    model.record(older)

    assert model.latest_observation("generic-a") is newer
    assert model.effective_capacity("generic-a", at=_T0).limit == 3
    assert model.history("generic-a") == (newer, older)


def test_provider_capacity_and_status_are_independent_facts() -> None:
    backend_id = BackendId("generic-a")
    health = BackendHealthModel()
    capacity = ProviderCapacityModel()
    capacity.record(_observation(backend_id, 4))

    # No status observation yet, but provider capacity is known: independent.
    assert health.effective_status(backend_id, at=_T0) is None
    assert capacity.effective_capacity(backend_id, at=_T0).limit == 4


def test_compatibility_is_independent_of_provider_capacity_and_status() -> None:
    backend_id = BackendId("generic-a")
    registry = BackendRegistry(
        [BackendDescriptor(backend_id, capabilities=frozenset({"shell"}))]
    )
    health = BackendHealthModel()
    health.record(
        BackendStatusObservation(backend_id, BackendHealth.UNAVAILABLE, observed_at=_T0)
    )
    capacity = ProviderCapacityModel()
    capacity.record(_observation(backend_id, 0))

    # A saturated / unavailable backend is still a factual capability match:
    # compatibility does not consult status or provider capacity.
    assert [d.backend_id for d in registry.compatible(_REQUIREMENTS)] == [backend_id]
