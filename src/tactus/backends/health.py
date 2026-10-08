"""Backend health observation model.

Backend health is factual, volatile system state observed independently of any
Work Order and of any execution attempt. It is deliberately separate from both
provider capacity/quota and administrative enablement:

* health            -> ``AVAILABLE`` / ``UNAVAILABLE`` probe/runtime fact
* admin enablement  -> operator-controlled fact serialized separately
* provider capacity -> externally reported limits/quota/GPU availability
                       (see :mod:`tactus.backends.capacity`)
* execution concurrency -> Dagster-owned (max concurrent runs, pool slots,
                       queued runs); deliberately **not** modelled here
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .registry import BackendId, coerce_backend_id


class BackendHealth(str, Enum):
    """Small, precise observed-health vocabulary.

    ``AVAILABLE`` — observed operational.
    ``UNAVAILABLE`` — observed not operational.

    Administrative disablement is not health and is serialized as a separate
    ``backend.administrative_enablement`` fact.
    """

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class BackendStatusObservation:
    """A status observation with explicit freshness semantics.

    ``expires_at is None`` means the observation is authoritative until
    superseded. There is deliberately no default TTL: freshness policy belongs
    to each observation producer.
    """

    backend: BackendId
    status: BackendHealth
    observed_at: datetime
    expires_at: datetime | None = None
    reason: str | None = None

    def is_fresh_at(self, at: datetime) -> bool:
        if self.expires_at is None:
            return True
        return at < self.expires_at


class BackendHealthModel:
    """Latest-wins status store with query-time expiry.

    Observations are retained as history. Expiry is evaluated when queried, so
    recording never mutates or discards past observations.
    """

    def __init__(self) -> None:
        self._history: dict[BackendId, list[BackendStatusObservation]] = {}
        self._latest: dict[BackendId, BackendStatusObservation] = {}

    def record(self, observation: BackendStatusObservation) -> None:
        """Record an observation.

        The newest ``observed_at`` wins. An out-of-order (older) observation is
        kept in history but does not overwrite newer state.
        """

        self._history.setdefault(observation.backend, []).append(observation)
        current = self._latest.get(observation.backend)
        if current is None or observation.observed_at > current.observed_at:
            self._latest[observation.backend] = observation

    def effective_status(
        self,
        backend: BackendId | str,
        *,
        at: datetime,
    ) -> BackendHealth | None:
        """Fresh authoritative status, or ``None``.

        ``None`` means "no fresh authoritative status known" — it must not be
        interpreted as ``UNAVAILABLE``.
        """

        observation = self._latest.get(coerce_backend_id(backend))
        if observation is None or not observation.is_fresh_at(at):
            return None
        return observation.status

    def latest_observation(
        self, backend: BackendId | str
    ) -> BackendStatusObservation | None:
        return self._latest.get(coerce_backend_id(backend))

    def history(
        self, backend: BackendId | str
    ) -> tuple[BackendStatusObservation, ...]:
        """All observations recorded for a backend, in record order."""

        return tuple(self._history.get(coerce_backend_id(backend), ()))
