"""External provider-capacity facts.

This module records what an *external* provider reports about its own capacity:
quota, rate limits, GPU availability, or any externally imposed limit. These are
**facts about the outside world**, not execution slots owned by Tactus.

Ownership boundary (load-bearing)::

    external/provider capacity  ->  facts recorded here; consumed as context by
                                    Ictus routing and by the Dagster integration
    execution concurrency       ->  Dagster-owned: max concurrent runs, pool
                                    slots, queued runs, worker occupancy

Tactus does not track occupancy, does not compute free execution slots and does
not schedule workers. There is deliberately no slot-counter or reserve/debit
operation in this module; that accounting belongs to Dagster's run queue and
pool configuration.

``effective_capacity() -> None`` means "no fresh authoritative provider-capacity
fact" and is distinct from an observation whose reported ``limit`` is ``0``
("the provider reports no capacity").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .registry import BackendId, coerce_backend_id


@dataclass(frozen=True, slots=True)
class ProviderCapacityObservation:
    """A timestamped, externally reported provider-capacity fact.

    ``limit`` is the ceiling the *provider* reports (for example a quota or a
    rate limit) expressed in ``unit``. It is not a Tactus execution-slot
    allocation and must never be debited by Tactus to schedule work.
    ``limit is None`` means the observation reports provider state without a
    numeric ceiling.
    """

    backend: BackendId
    observed_at: datetime
    limit: int | None = None
    unit: str | None = None
    source: str | None = None
    expires_at: datetime | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.limit is not None and self.limit < 0:
            raise ValueError("provider capacity limit must be non-negative")

    def is_fresh_at(self, at: datetime) -> bool:
        if self.expires_at is None:
            return True
        return at < self.expires_at


class ProviderCapacityModel:
    """Latest-wins store of external provider-capacity observations.

    Mirrors the status model: observations are retained as history, expiry is
    evaluated at query time, and the newest ``observed_at`` wins. This is a
    factual store, not a scheduler: it exposes no operation that reserves,
    debits or otherwise mutates occupancy.
    """

    def __init__(self) -> None:
        self._history: dict[BackendId, list[ProviderCapacityObservation]] = {}
        self._latest: dict[BackendId, ProviderCapacityObservation] = {}

    def record(self, observation: ProviderCapacityObservation) -> None:
        """Record an observation.

        The newest ``observed_at`` wins. An out-of-order (older) observation is
        kept in history but does not overwrite newer state.
        """

        self._history.setdefault(observation.backend, []).append(observation)
        current = self._latest.get(observation.backend)
        if current is None or observation.observed_at > current.observed_at:
            self._latest[observation.backend] = observation

    def effective_capacity(
        self,
        backend: BackendId | str,
        *,
        at: datetime,
    ) -> ProviderCapacityObservation | None:
        """Fresh authoritative provider-capacity fact, or ``None``.

        ``None`` means "no fresh authoritative provider-capacity fact known" —
        it must not be interpreted as zero capacity.
        """

        observation = self._latest.get(coerce_backend_id(backend))
        if observation is None or not observation.is_fresh_at(at):
            return None
        return observation

    def latest_observation(
        self, backend: BackendId | str
    ) -> ProviderCapacityObservation | None:
        return self._latest.get(coerce_backend_id(backend))

    def history(
        self, backend: BackendId | str
    ) -> tuple[ProviderCapacityObservation, ...]:
        """All observations recorded for a backend, in record order."""

        return tuple(self._history.get(coerce_backend_id(backend), ()))
