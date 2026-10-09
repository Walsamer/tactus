"""The generic semantic decision vocabulary and its validated Tactus value.

Ownership boundary (normative)
------------------------------
Ictus owns the generic, domain-neutral decision vocabulary, diagnosis and all
decision policy. This module is Tactus's **read-only mirror** of the frozen
vocabulary plus the validated decision value Tactus is allowed to apply. It
contains no diagnosis, no policy, no retry budget and no backend selection: it
only names the tokens and carries the validated parameters.

The frozen token set (Ictus ``DecisionProposal`` v2, ADR
``0001-generic-decision-vocabulary``) is::

    REEXECUTE | ROUTE | DECOMPOSE | ESCALATE | ABORT | EXECUTE_CAPABILITY

The legacy v1 token ``RETRY`` is accepted as an alias for ``REEXECUTE`` at the
Ictus wire boundary (:mod:`tactus.integrations.ictus.decisions`); it is not a
separate decision and never becomes a distinct :class:`DecisionKind` here.

This module deliberately defines **no** decision-making surface. Choosing *which*
decision applies is Ictus policy; deciding *what effect* a decision has on a
Work Order is Tactus (see :mod:`tactus.domain.decision_application`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DecisionError(ValueError):
    """Base class for invalid decision values."""


class UnknownDecisionError(DecisionError):
    """A decision token is outside the frozen generic vocabulary.

    Fails closed: Tactus must never invent a decision it cannot map.
    """


def _require_non_empty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DecisionError(f"{label} must be a non-empty string")
    return value.strip()


class DecisionKind(str, Enum):
    """The frozen generic semantic decision tokens, mirrored from Ictus.

    The vocabulary is intentionally domain-neutral: it contains no Tactus
    lifecycle nouns (``REQUEUE_READY``/``BLOCK``/``RETIRE``) and no
    durable-execution retry concept. ``REEXECUTE`` is a *semantic* re-execution
    request and is never a Dagster step retry.
    """

    REEXECUTE = "REEXECUTE"
    ROUTE = "ROUTE"
    DECOMPOSE = "DECOMPOSE"
    ESCALATE = "ESCALATE"
    ABORT = "ABORT"
    EXECUTE_CAPABILITY = "EXECUTE_CAPABILITY"

    @classmethod
    def from_token(cls, token: str) -> DecisionKind:
        """Parse a generic decision token, failing closed when unknown.

        ``RETRY`` is the legacy Ictus v1 alias for :attr:`REEXECUTE`. Version
        gating (which token is legal under which proposal ``schema_version``)
        belongs to the wire boundary, not here.
        """

        if not isinstance(token, str) or not token.strip():
            raise UnknownDecisionError("decision token must be a non-empty string")
        normalized = token.strip()
        if normalized == "RETRY":
            return cls.REEXECUTE
        try:
            return cls(normalized)
        except ValueError:
            raise UnknownDecisionError(
                f"unknown generic decision token: {normalized!r}"
            ) from None

    @property
    def requires_capability(self) -> bool:
        """Whether the decision addresses a named capability.

        Mirrors the Ictus contract exactly; choosing/validating the capability
        is Ictus policy.
        """

        return self in _CAPABILITY_DECISIONS

    @property
    def is_v2_only(self) -> bool:
        """Whether the token exists only in the ``DecisionProposal`` v2 contract."""

        return self in _V2_ONLY_DECISIONS


_CAPABILITY_DECISIONS = frozenset(
    {
        DecisionKind.REEXECUTE,
        DecisionKind.ROUTE,
        DecisionKind.EXECUTE_CAPABILITY,
    }
)

_V2_ONLY_DECISIONS = frozenset({DecisionKind.ROUTE, DecisionKind.DECOMPOSE})


@dataclass(frozen=True, slots=True)
class RouteConstraints:
    """Generic route constraints for a ``ROUTE`` decision (Ictus-owned shape).

    Every constraint names a *role* (backend, provider, runtime), never a
    concrete vendor or a domain noun. Tactus preserves these constraints for the
    execution plane; it never selects, ranks or filters a backend itself.
    """

    exclude_backend: str | None = None
    preferred_backend: str | None = None
    required_provider: str | None = None
    required_runtime: str | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("route.exclude_backend", self.exclude_backend),
            ("route.preferred_backend", self.preferred_backend),
            ("route.required_provider", self.required_provider),
            ("route.required_runtime", self.required_runtime),
        ):
            if value is not None:
                object.__setattr__(
                    self, _field_name(label), _require_non_empty(value, label)
                )

    @property
    def is_empty(self) -> bool:
        """True when no constraint is set.

        An unconstrained ``ROUTE`` is still valid: it simply expresses no
        preference.
        """

        return (
            self.exclude_backend is None
            and self.preferred_backend is None
            and self.required_provider is None
            and self.required_runtime is None
        )


def _field_name(label: str) -> str:
    return label.split(".", 1)[1]


@dataclass(frozen=True, slots=True)
class SemanticDecision:
    """A validated generic Ictus decision, in Tactus's internal value form.

    This is the anti-corruption result of the Ictus wire boundary. It carries the
    decision identity, the frozen kind and the validated decision parameters.
    Structural invariants are enforced here (fail closed); no semantic policy is.
    """

    decision_id: str
    kind: DecisionKind
    capability: str | None = None
    route: RouteConstraints | None = None
    schema_version: int = 2

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_id", _require_non_empty(self.decision_id, "decision_id"))
        if not isinstance(self.kind, DecisionKind):
            raise DecisionError("kind must be a DecisionKind")
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise DecisionError("schema_version must be an integer")

        if self.kind.requires_capability:
            if self.capability is None:
                raise DecisionError(
                    f"{self.kind.value} requires a capability id"
                )
            object.__setattr__(
                self, "capability", _require_non_empty(self.capability, "capability")
            )
        elif self.capability is not None:
            raise DecisionError(
                f"{self.kind.value} must not carry a capability id"
            )

        if self.route is not None and self.kind is not DecisionKind.ROUTE:
            raise DecisionError(
                f"route constraints are only valid for ROUTE, not {self.kind.value}"
            )
        if self.route is not None and not isinstance(self.route, RouteConstraints):
            raise DecisionError("route must be RouteConstraints")


__all__ = [
    "DecisionError",
    "DecisionKind",
    "RouteConstraints",
    "SemanticDecision",
    "UnknownDecisionError",
]
