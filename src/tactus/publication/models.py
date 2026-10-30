"""Publication authorization value objects and fail-closed validation.

Ownership boundary (normative)
------------------------------
Publication is a *separate* authorized effect from implementation. An agent
that may edit a workspace has no publication authority merely because its edit
run succeeded. Tactus owns this authorization interface; Ictus remains the
source of policy decisions and Tactus consumes the resulting, already-validated
grant. This module does not manufacture an ``ALLOW`` envelope, choose policy or
publish anything.

The authorization facts are deliberately explicit and all must match the exact
publication intent:

* ``authority`` -- the authenticated issuing authority, checked against a
  caller-configured trusted allowlist;
* ``repository`` -- the canonical ``owner/name`` destination repository;
* ``branch`` -- the review branch being published;
* ``base_revision`` -- the exact immutable commit the branch is based on;
* ``destination`` -- the base ref/destination of the publication (for example
  the pull-request base branch);
* ``scope`` -- the authorized repository-relative paths/effects;
* ``source_revision`` -- the Tactus source revision the grant was issued for;
* ``issued_at``/``expires_at`` -- freshness bounded in time; and
* ``revoked_at`` -- an explicit revocation fact that is always fail-closed.

Credentials are deliberately absent from every value object here. A guard
resolves a credential only after :func:`validate_publication_grant` succeeds,
and returned evidence contains no credential material (see ``guard``).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable

from tactus.domain import SourceRevision, WorkOrderId

__all__ = [
    "DEFAULT_TRUSTED_AUTHORITIES",
    "PublicationAuthorization",
    "PublicationAuthorityError",
    "PublicationBindingError",
    "PublicationDenialCode",
    "PublicationDeniedError",
    "PublicationEffect",
    "PublicationError",
    "PublicationExpiredError",
    "PublicationGrant",
    "PublicationIdentityConflictError",
    "PublicationNotYetValidError",
    "PublicationRequest",
    "PublicationRevokedError",
    "PublicationScopeError",
    "PublicationCredentialError",
    "PublicationTransportError",
    "normalize_scope",
    "scope_covers",
    "validate_publication_grant",
]

#: The default authority allowlist. A test or composition root may supply a
#: different explicit set, but an empty or unvalidated set is never accepted.
DEFAULT_TRUSTED_AUTHORITIES = frozenset({"operator"})

_REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class PublicationError(ValueError):
    """Base class for invalid or unauthorized publication operations."""


class PublicationDeniedError(PublicationError):
    """A publication grant did not authorize the requested publication."""

    code = "DENIED"

    def __init__(self, authorization: "PublicationAuthorization") -> None:
        self.authorization = authorization
        self.code = authorization.denial_code.value if authorization.denial_code else self.code
        super().__init__(authorization.reason or "publication is not authorized")


class PublicationAuthorityError(PublicationDeniedError):
    """The grant's issuing authority is not trusted."""


class PublicationBindingError(PublicationDeniedError):
    """The grant is bound to different publication facts."""


class PublicationScopeError(PublicationDeniedError):
    """The requested scope is wider than the grant's authorized scope."""


class PublicationNotYetValidError(PublicationDeniedError):
    """The grant was issued after the authorization instant."""


class PublicationExpiredError(PublicationDeniedError):
    """The grant is expired at the authorization instant."""


class PublicationRevokedError(PublicationDeniedError):
    """The grant has been explicitly revoked."""


class PublicationIdentityConflictError(PublicationError):
    """One publication intent identity was reused with different content."""


class PublicationCredentialError(PublicationError):
    """A credential provider could not produce a usable, opaque credential."""


class PublicationTransportError(PublicationError):
    """The publication transport refused or returned an invalid receipt."""


class PublicationEffect(str, Enum):
    """The closed set of effects a publication grant can authorize."""

    OPEN_PULL_REQUEST = "OPEN_PULL_REQUEST"
    PUBLISH_REVIEW_BRANCH = "PUBLISH_REVIEW_BRANCH"


class PublicationDenialCode(str, Enum):
    """Typed, non-secret reasons a grant was not accepted."""

    UNTRUSTED_AUTHORITY = "UNTRUSTED_AUTHORITY"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    SCOPE_NOT_AUTHORIZED = "SCOPE_NOT_AUTHORIZED"
    NOT_YET_VALID = "NOT_YET_VALID"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


def _require_non_empty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PublicationError(f"{label} must be a non-empty string")
    value = value.strip()
    if _CONTROL_RE.search(value):
        raise PublicationError(f"{label} must not contain control characters")
    return value


def _require_aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PublicationError(f"{label} must be a timezone-aware datetime")
    return value


def _normalize_repository(value: str) -> str:
    repository = _require_non_empty(value, "repository")
    if not _REPOSITORY_RE.fullmatch(repository):
        raise PublicationError("repository must be an owner/name slug")
    owner, name = repository.split("/", 1)
    if owner in {".", ".."} or name in {".", ".."}:
        raise PublicationError("repository must be an owner/name slug")
    return repository


def _normalize_branch(value: str, label: str = "branch") -> str:
    branch = _require_non_empty(value, label)
    if branch.startswith("-") or branch.startswith("/") or branch.endswith("/"):
        raise PublicationError(f"{label} must be a usable ref name")
    if ".." in branch or "//" in branch or "@{" in branch or branch.endswith(".lock"):
        raise PublicationError(f"{label} must be a usable ref name")
    if not _BRANCH_RE.fullmatch(branch):
        raise PublicationError(f"{label} must be a usable ref name")
    return branch


def _normalize_revision(value: str) -> str:
    revision = _require_non_empty(value, "base_revision")
    if not _REVISION_RE.fullmatch(revision):
        raise PublicationError(
            "base_revision must be an exact lowercase 40- or 64-character Git object id"
        )
    return revision


def _normalize_authority(value: str) -> str:
    return _require_non_empty(value, "authority").casefold()


def _normalize_scope_entry(entry: str) -> str:
    normalized = _require_non_empty(entry, "scope entry")
    while normalized.endswith("/**"):
        normalized = normalized[: -len("/**")]
    normalized = normalized.rstrip("/")
    if (
        not normalized
        or set(normalized) == {"*"}
        or normalized == ".."
        or normalized.startswith("../")
        or "/../" in normalized
        or normalized.startswith("/")
    ):
        raise PublicationError(f"unbounded or unsafe scope entry: {entry!r}")
    return normalized


def normalize_scope(entries: Iterable[str]) -> tuple[str, ...]:
    """Return a sorted, de-duplicated, normalized scope.

    ``src/**``, ``src/`` and ``src`` all normalize to the authorization prefix
    ``src``. Bare wildcards and path traversal are refused instead of silently
    turning into a whole-repository grant.
    """

    if isinstance(entries, str):
        raise PublicationError("scope must be an iterable of strings, not a string")
    return tuple(sorted({_normalize_scope_entry(entry) for entry in entries}))


def scope_covers(granted_scope: Iterable[str], requested_scope: Iterable[str]) -> bool:
    """Whether every requested scope entry is within the granted scope.

    Coverage is segment-prefix containment, so ``src`` covers
    ``src/publication/guard.py`` but never ``src2/guard.py``. An empty grant
    covers nothing.
    """

    granted = normalize_scope(granted_scope)
    for requested in normalize_scope(requested_scope):
        if not any(
            requested == entry or requested.startswith(entry + "/")
            for entry in granted
        ):
            return False
    return True


def _digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class PublicationRequest:
    """One stable publication intent.

    ``intent_id`` is the caller's semantic identity. The stronger
    :attr:`publication_id` is derived deterministically from every bound field,
    so repeating the same intent (even in a fresh process) yields the same
    publication identity and never depends on wall-clock time or randomness.
    """

    intent_id: str
    work_order_id: WorkOrderId
    repository: str
    branch: str
    base_revision: str
    destination: str
    scope: tuple[str, ...]
    source_revision: SourceRevision
    effect: PublicationEffect = PublicationEffect.OPEN_PULL_REQUEST

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_id", _require_non_empty(self.intent_id, "intent_id"))
        if not isinstance(self.work_order_id, WorkOrderId):
            object.__setattr__(self, "work_order_id", WorkOrderId(self.work_order_id))
        object.__setattr__(self, "repository", _normalize_repository(self.repository))
        object.__setattr__(self, "branch", _normalize_branch(self.branch, "branch"))
        object.__setattr__(self, "base_revision", _normalize_revision(self.base_revision))
        object.__setattr__(
            self, "destination", _normalize_branch(self.destination, "destination")
        )
        object.__setattr__(self, "scope", normalize_scope(self.scope))
        if not isinstance(self.source_revision, SourceRevision):
            object.__setattr__(
                self, "source_revision", SourceRevision(self.source_revision)
            )
        if not isinstance(self.effect, PublicationEffect):
            try:
                object.__setattr__(self, "effect", PublicationEffect(self.effect))
            except ValueError as exc:
                raise PublicationError("effect must be a PublicationEffect") from exc

    def _canonical(self) -> dict[str, object]:
        return {
            "effect": self.effect.value,
            "intent_id": self.intent_id,
            "work_order_id": str(self.work_order_id),
            "repository": self.repository,
            "branch": self.branch,
            "base_revision": self.base_revision,
            "destination": self.destination,
            "scope": list(self.scope),
            "source_revision": str(self.source_revision),
        }

    @property
    def intent_digest(self) -> str:
        """SHA-256 digest of the complete publication intent."""

        return _digest(self._canonical())

    @property
    def publication_id(self) -> str:
        """Stable, content-addressed publication identity for this intent."""

        return "pub-" + _digest(
            {"schema_version": 1, "intent_digest": self.intent_digest}
        )[:32]

    def as_dict(self) -> dict[str, object]:
        """Serializable intent projection (contains no credential material)."""

        return {
            **self._canonical(),
            "intent_digest": self.intent_digest,
            "publication_id": self.publication_id,
        }


@dataclass(frozen=True, slots=True)
class PublicationGrant:
    """An already-validated grant consumed by the publication guard.

    This value is supplied by the trusted grant/human-resolution boundary. The
    guard re-checks every field against the current publication request and
    clock; constructing a grant is never itself proof of authorization, and
    this module has no path that manufactures an Ictus ``ALLOW``.
    """

    grant_id: str
    authority: str
    work_order_id: WorkOrderId
    repository: str
    branch: str
    base_revision: str
    destination: str
    scope: tuple[str, ...]
    source_revision: SourceRevision
    issued_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    effect: PublicationEffect = PublicationEffect.OPEN_PULL_REQUEST

    def __post_init__(self) -> None:
        object.__setattr__(self, "grant_id", _require_non_empty(self.grant_id, "grant_id"))
        object.__setattr__(self, "authority", _normalize_authority(self.authority))
        if not isinstance(self.work_order_id, WorkOrderId):
            object.__setattr__(self, "work_order_id", WorkOrderId(self.work_order_id))
        object.__setattr__(self, "repository", _normalize_repository(self.repository))
        object.__setattr__(self, "branch", _normalize_branch(self.branch, "branch"))
        object.__setattr__(self, "base_revision", _normalize_revision(self.base_revision))
        object.__setattr__(
            self, "destination", _normalize_branch(self.destination, "destination")
        )
        object.__setattr__(self, "scope", normalize_scope(self.scope))
        if not isinstance(self.source_revision, SourceRevision):
            object.__setattr__(
                self, "source_revision", SourceRevision(self.source_revision)
            )
        issued_at = _require_aware(self.issued_at, "issued_at")
        object.__setattr__(self, "issued_at", issued_at)
        if self.expires_at is not None:
            expires_at = _require_aware(self.expires_at, "expires_at")
            if expires_at <= issued_at:
                raise PublicationError("expires_at must be after issued_at")
            object.__setattr__(self, "expires_at", expires_at)
        if self.revoked_at is not None:
            object.__setattr__(self, "revoked_at", _require_aware(self.revoked_at, "revoked_at"))
        if not isinstance(self.effect, PublicationEffect):
            try:
                object.__setattr__(self, "effect", PublicationEffect(self.effect))
            except ValueError as exc:
                raise PublicationError("effect must be a PublicationEffect") from exc

    @property
    def is_revoked(self) -> bool:
        """Revocation is a recorded fact and is always fail-closed."""

        return self.revoked_at is not None

    def is_fresh_at(self, at: datetime) -> bool:
        """Whether the grant is usable at *at* (ignoring authority/binding)."""

        at = _require_aware(at, "at")
        if self.is_revoked:
            return False
        if self.issued_at > at:
            return False
        return self.expires_at is None or at < self.expires_at


@dataclass(frozen=True, slots=True)
class PublicationAuthorization:
    """Pure result of evaluating one grant against one publication intent.

    ``allowed`` is a Tactus authorization-guard verdict over facts already
    supplied by the caller. It is not an Ictus policy decision and does not by
    itself resolve a credential.
    """

    publication_id: str
    intent_id: str
    work_order_id: WorkOrderId
    grant_id: str
    authority: str
    repository: str
    branch: str
    base_revision: str
    destination: str
    source_revision: SourceRevision
    scope: tuple[str, ...]
    effect: PublicationEffect
    checked_at: datetime
    allowed: bool
    denial_code: PublicationDenialCode | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        """Redacted authorization projection suitable for evidence/audit."""

        return {
            "allowed": self.allowed,
            "authority": self.authority,
            "base_revision": self.base_revision,
            "branch": self.branch,
            "checked_at": self.checked_at.isoformat(),
            "denial_code": self.denial_code.value if self.denial_code else None,
            "destination": self.destination,
            "effect": self.effect.value,
            "grant_id": self.grant_id,
            "intent_id": self.intent_id,
            "publication_id": self.publication_id,
            "reason": self.reason,
            "repository": self.repository,
            "scope": list(self.scope),
            "source_revision": str(self.source_revision),
            "work_order_id": str(self.work_order_id),
        }


def _authorization(
    request: PublicationRequest,
    grant: PublicationGrant,
    *,
    checked_at: datetime,
    allowed: bool,
    denial_code: PublicationDenialCode | None = None,
    reason: str = "",
) -> PublicationAuthorization:
    return PublicationAuthorization(
        publication_id=request.publication_id,
        intent_id=request.intent_id,
        work_order_id=request.work_order_id,
        grant_id=grant.grant_id,
        authority=grant.authority,
        repository=request.repository,
        branch=request.branch,
        base_revision=request.base_revision,
        destination=request.destination,
        source_revision=request.source_revision,
        scope=request.scope,
        effect=request.effect,
        checked_at=checked_at,
        allowed=allowed,
        denial_code=denial_code,
        reason=reason,
    )


def _same_repository(left: str, right: str) -> bool:
    # GitHub repository names are case-insensitive; owner/name comparison is
    # therefore case-folded while every other binding stays exact.
    return left.casefold() == right.casefold()


def validate_publication_grant(
    request: PublicationRequest,
    grant: PublicationGrant,
    *,
    trusted_authorities: Iterable[str] = DEFAULT_TRUSTED_AUTHORITIES,
    at: datetime | None = None,
) -> PublicationAuthorization:
    """Validate one grant against one intent without side effects.

    The checks are ordered authority, exact binding, scope, freshness and
    revocation. ``at`` defaults to the current UTC time. The returned object is
    a Tactus guard verdict only; it never contacts a credential provider.
    """

    if not isinstance(request, PublicationRequest):
        raise PublicationError("request must be a PublicationRequest")
    if not isinstance(grant, PublicationGrant):
        raise PublicationError("grant must be a PublicationGrant")

    checked_at = _require_aware(
        at if at is not None else datetime.now(timezone.utc), "at"
    )
    if isinstance(trusted_authorities, str):
        raise PublicationError(
            "trusted_authorities must be an iterable of authorities, not a string"
        )
    try:
        trusted_items = tuple(trusted_authorities)
    except TypeError:
        raise PublicationError(
            "trusted_authorities must be an iterable of authorities"
        ) from None
    trusted = frozenset(_normalize_authority(item) for item in trusted_items)

    def deny(code: PublicationDenialCode, reason: str) -> PublicationAuthorization:
        return _authorization(
            request, grant, checked_at=checked_at, allowed=False, denial_code=code, reason=reason
        )

    if not trusted:
        raise PublicationError("trusted_authorities must not be empty")
    if grant.authority not in trusted:
        return deny(
            PublicationDenialCode.UNTRUSTED_AUTHORITY,
            f"grant authority {grant.authority!r} is not a trusted publication authority",
        )

    if grant.work_order_id != request.work_order_id:
        return deny(
            PublicationDenialCode.BINDING_MISMATCH,
            "grant is bound to a different WorkOrder",
        )
    if not _same_repository(grant.repository, request.repository):
        return deny(
            PublicationDenialCode.BINDING_MISMATCH,
            "grant is bound to a different repository",
        )
    if grant.branch != request.branch:
        return deny(
            PublicationDenialCode.BINDING_MISMATCH,
            "grant is bound to a different branch",
        )
    if grant.base_revision != request.base_revision:
        return deny(
            PublicationDenialCode.BINDING_MISMATCH,
            "grant is bound to a different base revision",
        )
    if grant.destination != request.destination:
        return deny(
            PublicationDenialCode.BINDING_MISMATCH,
            "grant is bound to a different publication destination",
        )
    if grant.source_revision != request.source_revision:
        return deny(
            PublicationDenialCode.BINDING_MISMATCH,
            "grant is bound to a stale source revision",
        )
    if grant.effect is not request.effect:
        return deny(
            PublicationDenialCode.BINDING_MISMATCH,
            "grant authorizes a different publication effect",
        )
    if not request.scope:
        return deny(
            PublicationDenialCode.SCOPE_NOT_AUTHORIZED,
            "publication request declares no authorized scope",
        )
    if not scope_covers(grant.scope, request.scope):
        return deny(
            PublicationDenialCode.SCOPE_NOT_AUTHORIZED,
            "requested scope is not covered by the grant",
        )
    if grant.is_revoked:
        return deny(
            PublicationDenialCode.REVOKED,
            f"grant {grant.grant_id!r} is revoked",
        )
    if grant.issued_at > checked_at:
        return deny(
            PublicationDenialCode.NOT_YET_VALID,
            f"grant {grant.grant_id!r} is not valid yet",
        )
    if grant.expires_at is not None and checked_at >= grant.expires_at:
        return deny(
            PublicationDenialCode.EXPIRED,
            f"grant {grant.grant_id!r} expired at {grant.expires_at.isoformat()}",
        )

    return _authorization(
        request, grant, checked_at=checked_at, allowed=True, reason="authorized"
    )


_DENIAL_ERRORS: dict[PublicationDenialCode, type[PublicationDeniedError]] = {
    PublicationDenialCode.UNTRUSTED_AUTHORITY: PublicationAuthorityError,
    PublicationDenialCode.BINDING_MISMATCH: PublicationBindingError,
    PublicationDenialCode.SCOPE_NOT_AUTHORIZED: PublicationScopeError,
    PublicationDenialCode.NOT_YET_VALID: PublicationNotYetValidError,
    PublicationDenialCode.EXPIRED: PublicationExpiredError,
    PublicationDenialCode.REVOKED: PublicationRevokedError,
}


def raise_for_denial(authorization: PublicationAuthorization) -> None:
    """Raise the typed denial for an unauthorized verdict."""

    if not isinstance(authorization, PublicationAuthorization):
        raise PublicationError("authorization must be a PublicationAuthorization")
    if authorization.allowed:
        return
    code = authorization.denial_code or PublicationDenialCode.BINDING_MISMATCH
    error = _DENIAL_ERRORS.get(code, PublicationDeniedError)
    raise error(authorization)
