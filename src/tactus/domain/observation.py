"""Failure observations attached to an ``ACTIVE`` Work Order.

A ``FailureObservation`` is an event/value attached to a Work Order. Recording
one never changes the lifecycle state: per the lifecycle contract the Work
Order stays ``ACTIVE`` until Tactus applies a validated recovery decision.

The precise Ictus-facing observation schema is defined separately (issue #7).
This type is intentionally small and opaque so the Tactus-internal model does
not pre-empt that contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping

from ._clock import utcnow


@dataclass(frozen=True, slots=True)
class FailureObservation:
    """A normalized failure observation for a failed execution attempt."""

    summary: str
    detail: str = ""
    observed_at: datetime = field(default_factory=utcnow)
    evidence: Mapping[str, str] = field(default_factory=dict)
