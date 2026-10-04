"""Backend status (health) model.

Backend status is factual, volatile system state — observed independently of
any Work Order and of any execution attempt. It is deliberately separate from
provider capacity and quota:

* status            -> ``AVAILABLE`` / ``UNAVAILABLE`` / ``DISABLED``
* provider capacity -> externally reported limits/quota/GPU availability
                       (see :mod:`tactus.backends.capacity`)
* execution concurrency -> Dagster-owned (max concurrent runs, pool slots,
                       queued runs); deliberately **not** modelled here

``DISABLED`` is administrative, not a health condition, which is why the
observation type is named :class:`BackendStatusObservation` rather than
``HealthObservation``: it covers both probe-derived and operator-controlled
state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .registry import BackendId, coerce_backend_id


class BackendHealth(str, Enum):
    """Small, precise status vocabulary.

    ``AVAILABLE`` — operational.
    ``UNAVAILABLE`` — currently not operational; must not receive work.
    ``DISABLED`` — administratively excluded until explicitly re-enabled or
    superseded.
    """

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    DISABLED = "DISABLED"


@dataclass(frozen=True, slots=True)
class BackendStatusObservation:
    """A status observation with explicit freshness semantics.

    ``expires_at is None`` means the observation is authoritative until
    superseded (for example an operator ``DISABLED``). There is deliberately no
    default TTL: freshness policy belongs to each observation producer.
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
