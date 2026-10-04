from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tactus.backends import (
    BackendDescriptor,
    BackendHealth,
    BackendHealthModel,
    BackendId,
    BackendRegistry,
    BackendRequirements,
    BackendStatusObservation,
    Capacity,
    CapacityView,
)

_T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
_REQUIREMENTS = BackendRequirements(required_capabilities=frozenset({"shell"}))


def test_capacity_available() -> None:
    assert Capacity(in_use=1, limit=3).available == 2


def test_capacity_validation() -> None:
    with pytest.raises(ValueError):
        Capacity(in_use=0, limit=-1)
    with pytest.raises(ValueError):
        Capacity(in_use=-1, limit=1)
    with pytest.raises(ValueError):
        Capacity(in_use=4, limit=3)


def test_known_zero_capacity_is_distinct_from_unknown() -> None:
    known_zero = CapacityView({BackendId("generic-a"): Capacity(in_use=0, limit=0)})
    unknown = CapacityView()

    assert known_zero.capacity_of("generic-a") == Capacity(in_use=0, limit=0)
    assert known_zero.capacity_of("generic-a").available == 0
    assert unknown.capacity_of("generic-a") is None


def test_capacity_view_set_and_clear() -> None:
    view = CapacityView()
    view.set("generic-a", Capacity(in_use=1, limit=2))
    assert view.capacity_of("generic-a").available == 1

    view.clear("generic-a")
    assert view.capacity_of("generic-a") is None


def test_available_and_busy_backend_remains_compatible_and_healthy() -> None:
    backend_id = BackendId("generic-a")
    registry = BackendRegistry(
        [BackendDescriptor(backend_id, capabilities=frozenset({"shell"}))]
    )
    health = BackendHealthModel()
    health.record(
        BackendStatusObservation(backend_id, BackendHealth.AVAILABLE, observed_at=_T0)
    )
    capacity = CapacityView({backend_id: Capacity(in_use=3, limit=3)})

    # Saturated is not unavailable, and not incompatible: the Work Order would
    # stay OPEN + READY and wait, it would not be blocked.
    assert [d.backend_id for d in registry.compatible(_REQUIREMENTS)] == [backend_id]
    assert health.effective_status(backend_id, at=_T0) is BackendHealth.AVAILABLE
    assert capacity.capacity_of(backend_id).available == 0


def test_unavailable_backend_is_still_compatible() -> None:
    backend_id = BackendId("generic-a")
    registry = BackendRegistry(
        [BackendDescriptor(backend_id, capabilities=frozenset({"shell"}))]
    )
    health = BackendHealthModel()
    health.record(
        BackendStatusObservation(backend_id, BackendHealth.UNAVAILABLE, observed_at=_T0)
    )

    # Compatibility is factual capability matching only; status is a separate
    # fact consumed later by the decision plane.
    assert [d.backend_id for d in registry.compatible(_REQUIREMENTS)] == [backend_id]
    assert health.effective_status(backend_id, at=_T0) is BackendHealth.UNAVAILABLE


def test_status_and_capacity_are_independent_facts() -> None:
    backend_id = BackendId("generic-a")
    health = BackendHealthModel()
    capacity = CapacityView({backend_id: Capacity(in_use=0, limit=1)})

    # No status observation yet, but capacity is known: still independent.
    assert health.effective_status(backend_id, at=_T0) is None
    assert capacity.capacity_of(backend_id).available == 1
