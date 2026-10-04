"""Transition provenance records.

A transition record captures *who/what caused or authorized* a lifecycle
transition, *when*, and *why*. It deliberately does not encode transition
semantics such as admission, withdrawal or supersession: those are already
expressed by the called operation and by the recorded ``from_state`` /
``to_state`` pair.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from .work_order import WorkOrderState


class TransitionAuthority(str, Enum):
    """The authority or subsystem that caused/authorized a transition.

    The vocabulary is intentionally small. It answers "who did this?", not
    "what happened?".
    """

    OPERATOR = "operator"
    ADMISSION = "admission"
    SCHEDULER = "scheduler"
    VERIFIER = "verifier"
    RECOVERY = "recovery"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class TransitionRecord:
    """Immutable provenance record for a single lifecycle transition.

    Lineage-compatible by design: later revisions may add decision,
    observation or state-version references without changing or reinterpreting
    the fields below.
    """

    from_state: WorkOrderState
    to_state: WorkOrderState
    reason: str
    authority: TransitionAuthority
    occurred_at: datetime
    transition_id: str = field(default_factory=lambda: uuid.uuid4().hex)
