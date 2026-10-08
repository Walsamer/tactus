"""The Work Order lifecycle entity.

This module owns the five-state lifecycle, the orthogonal ``OPEN`` readiness
status, transition guards and transition provenance. It deliberately contains
no scheduler, dependency-graph, recovery-policy, Ictus, Dagster or agent logic.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ._clock import utcnow
from .blockers import BlockReason, Blocker, BlockerId, BlockerSet
from .observation import FailureObservation
from .records import TransitionAuthority, TransitionRecord


class IllegalTransitionError(ValueError):
    """Raised when a lifecycle transition is not permitted by the model."""


class InvalidReadinessError(ValueError):
    """Raised when readiness is read or changed outside ``OPEN``."""


@dataclass(frozen=True, slots=True)
class WorkOrderId:
    """Stable, validated identity for a Work Order.

    A value object (not a bare string) so identity can be validated once and
    used safely as a graph node. It is immutable and hashable.
    """

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("WorkOrderId must be a non-empty string")
        object.__setattr__(self, "value", self.value.strip())

    def __str__(self) -> str:
        return self.value


class WorkOrderState(str, Enum):
    """The exactly five lifecycle states owned by Tactus."""

    DRAFT = "DRAFT"
    OPEN = "OPEN"
    ACTIVE = "ACTIVE"
    IMPLEMENTED = "IMPLEMENTED"
    RETIRED = "RETIRED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATES


class OpenStatus(str, Enum):
    """Readiness of an ``OPEN`` Work Order.

    Orthogonal to lifecycle state and meaningful only while ``OPEN``.
    ``UNKNOWN`` is the initial value after admission, before readiness has been
    evaluated (for example against dependency/backend prerequisites).
    """

    UNKNOWN = "UNKNOWN"
    READY = "READY"
    BLOCKED = "BLOCKED"


_TERMINAL_STATES = frozenset({WorkOrderState.IMPLEMENTED, WorkOrderState.RETIRED})

# Legal lifecycle transitions. Readiness changes are intentionally absent:
# they are not lifecycle transitions.
_LEGAL_TRANSITIONS: dict[WorkOrderState, frozenset[WorkOrderState]] = {
    WorkOrderState.DRAFT: frozenset({WorkOrderState.OPEN, WorkOrderState.RETIRED}),
    WorkOrderState.OPEN: frozenset({WorkOrderState.ACTIVE, WorkOrderState.RETIRED}),
    WorkOrderState.ACTIVE: frozenset(
        {WorkOrderState.IMPLEMENTED, WorkOrderState.OPEN, WorkOrderState.RETIRED}
    ),
    WorkOrderState.IMPLEMENTED: frozenset(),
    WorkOrderState.RETIRED: frozenset(),
}


class WorkOrder:
    """Lifecycle entity; sole authority over lifecycle state and readiness.

    The entity is deliberately mutable: it accumulates immutable transition
    records and failure observations while its lifecycle state evolves. It owns
    no persistence.
    """

    def __init__(
        self,
        work_order_id: WorkOrderId | str,
        title: str | None = None,
    ) -> None:
        self._id = (
            work_order_id
            if isinstance(work_order_id, WorkOrderId)
            else WorkOrderId(work_order_id)
        )
        self._title = title
        self._state = WorkOrderState.DRAFT
        self._readiness: OpenStatus | None = None
        self._transitions: list[TransitionRecord] = []
        self._failures: list[FailureObservation] = []
        self._blockers: BlockerSet = BlockerSet()
        self._prerequisites_known = False

    @classmethod
    def create(cls, work_order_id: WorkOrderId | str, title: str | None = None) -> WorkOrder:
        """Create a new Work Order in ``DRAFT``."""

        return cls(work_order_id, title)

    # -- identity and read-only views -------------------------------------

    @property
    def id(self) -> WorkOrderId:
        return self._id

    @property
    def title(self) -> str | None:
        return self._title

    @property
    def state(self) -> WorkOrderState:
        return self._state

    @property
    def readiness(self) -> OpenStatus | None:
        """Readiness while ``OPEN``; ``None`` in every other lifecycle state."""

        return self._readiness

    @property
    def transitions(self) -> tuple[TransitionRecord, ...]:
        return tuple(self._transitions)

    @property
    def failure_observations(self) -> tuple[FailureObservation, ...]:
        return tuple(self._failures)

    @property
    def blockers(self) -> tuple[Blocker, ...]:
        """All typed blockers (resolved and unresolved), in insertion order."""

        return self._blockers.all

    @property
    def active_blockers(self) -> tuple[Blocker, ...]:
        """The unresolved blockers currently holding readiness at ``BLOCKED``."""

        return self._blockers.active

    @property
    def is_terminal(self) -> bool:
        return self._state.is_terminal

    # -- lifecycle operations ---------------------------------------------

    def admit(
        self,
        *,
        reason: str,
        at: datetime | None = None,
        authority: TransitionAuthority = TransitionAuthority.ADMISSION,
    ) -> TransitionRecord:
        """Admit a ``DRAFT`` Work Order into ``OPEN``.

        Admission only opens the Work Order. Readiness starts as ``UNKNOWN``
        and is resolved later by readiness evaluation (#3/#4).
        """

        self._require_state(WorkOrderState.DRAFT)
        return self._transition(
            WorkOrderState.OPEN,
            reason=reason,
            authority=authority,
            at=at,
            readiness=OpenStatus.UNKNOWN,
        )

    def withdraw(
        self,
        *,
        reason: str,
        at: datetime | None = None,
        authority: TransitionAuthority = TransitionAuthority.OPERATOR,
    ) -> TransitionRecord:
        """Withdraw a ``DRAFT`` Work Order before admission."""

        self._require_state(WorkOrderState.DRAFT)
        return self._transition(
            WorkOrderState.RETIRED, reason=reason, authority=authority, at=at
        )

    def claim(
        self,
        *,
        reason: str,
        at: datetime | None = None,
        authority: TransitionAuthority = TransitionAuthority.EXECUTION_ADMISSION,
    ) -> TransitionRecord:
        """Claim an ``OPEN + READY`` Work Order into ``ACTIVE``.

        ``OPEN -> ACTIVE`` is legal only from ``OPEN + READY``. ``UNKNOWN`` and
        ``BLOCKED`` readiness cannot be claimed.
        """

        self._require_state(WorkOrderState.OPEN)
        if self._readiness is not OpenStatus.READY:
            raise IllegalTransitionError(
                "claim requires OPEN + READY "
                f"(state={self._state.value}, readiness="
                f"{self._readiness.value if self._readiness else None})"
            )
        return self._transition(
            WorkOrderState.ACTIVE, reason=reason, authority=authority, at=at
        )

    def complete(
        self,
        *,
        reason: str,
        at: datetime | None = None,
        authority: TransitionAuthority = TransitionAuthority.VERIFIER,
    ) -> TransitionRecord:
        """Mark an ``ACTIVE`` Work Order ``IMPLEMENTED`` after verified success."""

        self._require_state(WorkOrderState.ACTIVE)
        return self._transition(
            WorkOrderState.IMPLEMENTED, reason=reason, authority=authority, at=at
        )

    def reopen(
        self,
        readiness: OpenStatus,
        *,
        reason: str,
        at: datetime | None = None,
        authority: TransitionAuthority = TransitionAuthority.RECOVERY,
    ) -> TransitionRecord:
        """Apply a recovery outcome that reopens an ``ACTIVE`` Work Order.

        Minimal v1 guard for the ``ACTIVE -> OPEN`` edge. The typed
        ``RecoveryDecision`` vocabulary and mapping are owned by #8; readiness
        must be resolved to ``READY`` or ``BLOCKED`` here.
        """

        if readiness is OpenStatus.UNKNOWN:
            raise InvalidReadinessError(
                "reopening after recovery must resolve readiness to READY or BLOCKED"
            )
        self._require_state(WorkOrderState.ACTIVE)
        return self._transition(
            WorkOrderState.OPEN,
            reason=reason,
            authority=authority,
            at=at,
            readiness=readiness,
        )

    def retire(
        self,
        *,
        reason: str,
        at: datetime | None = None,
        authority: TransitionAuthority = TransitionAuthority.OPERATOR,
    ) -> TransitionRecord:
        """Retire an ``OPEN`` or ``ACTIVE`` Work Order."""

        self._require_state(WorkOrderState.OPEN, WorkOrderState.ACTIVE)
        return self._transition(
            WorkOrderState.RETIRED, reason=reason, authority=authority, at=at
        )

    # -- readiness (not lifecycle transitions) ----------------------------

    def set_readiness(self, status: OpenStatus) -> None:
        """Set ``OPEN`` readiness directly (raw/legacy seam).

        This is intentionally *not* a lifecycle transition and appends no
        ``TransitionRecord``. Prefer :meth:`recompute_readiness` and the typed
        blocker API: readiness may not become ``READY`` while a typed blocker is
        still active.
        """

        self._require_open_for_readiness()
        if status is OpenStatus.READY and self._blockers.has_active:
            raise InvalidReadinessError(
                "readiness cannot be READY while typed blockers are active"
            )
        self._readiness = status
        self._prerequisites_known = status is OpenStatus.READY

    # -- typed blockers and independent unblocking ------------------------

    def add_blocker(self, blocker: Blocker) -> Blocker:
        """Attach a typed blocker; readiness becomes ``BLOCKED``.

        Blockers are meaningful only while ``OPEN``. Adding a blocker is
        additive and never clears an existing blocker.
        """

        self._require_open_for_readiness()
        added = self._blockers.add(blocker)
        self._readiness = OpenStatus.BLOCKED
        return added

    def block(
        self,
        reason: BlockReason,
        *,
        detail: str = "",
        subreason: str | None = None,
        evidence: Mapping[str, str] | None = None,
        at: datetime | None = None,
    ) -> Blocker:
        """Attach a typed blocker built from ``reason`` (convenience)."""

        return self.add_blocker(
            Blocker(
                reason=reason,
                detail=detail,
                subreason=subreason,
                evidence=evidence or {},
                blocked_at=at if at is not None else utcnow(),
            )
        )

    def resolve_blocker(
        self,
        blocker_id: BlockerId | str,
        *,
        at: datetime | None = None,
        authority: TransitionAuthority = TransitionAuthority.OPERATOR,
        note: str = "",
    ) -> Blocker:
        """Resolve exactly one blocker and recompute readiness.

        Other blockers are left untouched. Readiness only becomes ``READY`` when
        the whole set has no active blocker left and prerequisites are known.
        """

        self._require_open_for_readiness()
        resolved = self._blockers.resolve(
            blocker_id, at=at, authority=authority, note=note
        )
        self.recompute_readiness()
        return resolved

    def replace_blockers(
        self, blockers: Iterable[Blocker], *, prerequisites_known: bool = False
    ) -> OpenStatus:
        """Atomically replace the blocker set and recompute readiness.

        Used by readiness re-evaluation so readiness reflects the latest
        validated facts instead of accumulating stale blockers.
        """

        self._require_open_for_readiness()
        self._blockers.replace(blockers)
        self._prerequisites_known = prerequisites_known
        return self.recompute_readiness()

    def recompute_readiness(self) -> OpenStatus:
        """Recompute ``OPEN`` readiness from the whole blocker set.

        ``BLOCKED`` while any blocker is active. Without blockers, return
        ``READY`` only when prerequisites are known, otherwise ``UNKNOWN``.
        This never establishes missing facts or clears a blocker on its own.
        """

        self._require_open_for_readiness()
        if self._blockers.has_active:
            self._readiness = OpenStatus.BLOCKED
        else:
            self._readiness = (
                OpenStatus.READY if self._prerequisites_known else OpenStatus.UNKNOWN
            )
        return self._readiness

    def _require_open_for_readiness(self) -> None:
        if self._state is not WorkOrderState.OPEN:
            raise InvalidReadinessError(
                f"readiness is only defined while OPEN (state={self._state.value})"
            )

    # -- observations (do not change lifecycle state) ---------------------

    def record_failure(self, observation: FailureObservation) -> None:
        """Attach a ``FailureObservation`` to an ``ACTIVE`` Work Order.

        The lifecycle state is left untouched.
        """

        if self._state is not WorkOrderState.ACTIVE:
            raise IllegalTransitionError(
                "failure observations may only be recorded while ACTIVE "
                f"(state={self._state.value})"
            )
        self._failures.append(observation)

    # -- internals --------------------------------------------------------

    def _require_state(self, *states: WorkOrderState) -> None:
        if self._state not in states:
            allowed = ", ".join(state.value for state in states)
            raise IllegalTransitionError(
                f"operation requires state in [{allowed}] "
                f"(state={self._state.value})"
            )

    def _transition(
        self,
        to_state: WorkOrderState,
        *,
        reason: str,
        authority: TransitionAuthority,
        at: datetime | None,
        readiness: OpenStatus | None = None,
    ) -> TransitionRecord:
        if to_state not in _LEGAL_TRANSITIONS[self._state]:
            raise IllegalTransitionError(
                f"illegal transition {self._state.value} -> {to_state.value}"
            )

        record = TransitionRecord(
            from_state=self._state,
            to_state=to_state,
            reason=reason,
            authority=authority,
            occurred_at=at if at is not None else utcnow(),
        )

        self._state = to_state
        self._readiness = readiness if to_state is WorkOrderState.OPEN else None
        self._prerequisites_known = self._readiness is OpenStatus.READY
        self._transitions.append(record)
        return record
