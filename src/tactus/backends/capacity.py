"""Backend capacity (concurrency/slots).

Capacity is kept strictly separate from backend status and from compatibility.
A backend can be ``AVAILABLE`` with zero free slots: that is a *busy* backend,
not an unavailable one, and it never blocks a Work Order.

``capacity_of() -> None`` means "no authoritative capacity information", which
is different from ``Capacity(limit=0)`` ("known capacity is zero").
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .registry import BackendId, coerce_backend_id


@dataclass(frozen=True, slots=True)
class Capacity:
    """Current slot usage for a backend."""

    in_use: int
    limit: int

    def __post_init__(self) -> None:
        if self.limit < 0:
            raise ValueError("capacity limit must be non-negative")
        if self.in_use < 0:
            raise ValueError("capacity in_use must be non-negative")
        if self.in_use > self.limit:
            raise ValueError("capacity in_use cannot exceed limit")

    @property
    def available(self) -> int:
        return self.limit - self.in_use


class CapacityView:
    """Read-mostly view of current per-backend capacity.

    The interface intentionally exposes a single read method,
    :meth:`capacity_of`. Mutators exist so tests and future scheduler wiring can
    populate the view from real runtime state; they are not a scheduling API.
    """

    def __init__(self, capacities: Mapping[BackendId | str, Capacity] | None = None) -> None:
        self._capacities: dict[BackendId, Capacity] = {}
        if capacities is not None:
            for backend, capacity in capacities.items():
                self.set(backend, capacity)

    def capacity_of(self, backend: BackendId | str) -> Capacity | None:
        return self._capacities.get(coerce_backend_id(backend))

    def set(self, backend: BackendId | str, capacity: Capacity) -> None:
        self._capacities[coerce_backend_id(backend)] = capacity

    def clear(self, backend: BackendId | str) -> None:
        self._capacities.pop(coerce_backend_id(backend), None)
