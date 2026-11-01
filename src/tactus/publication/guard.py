"""Publication authorization guard with one-shot, mock-only credentials.

The guard is the Tactus-owned ordering boundary between an already-validated
publication grant and a publication transport:

::

    validate authority + binding + scope + freshness + revocation
        -> resolve exactly one opaque credential
        -> invoke the publication transport exactly once
        -> return fully redacted evidence

If *any* check fails, the credential provider and transport are never called.
This module contains no policy evaluation, no Git/GitHub call and no live
transport. Production wiring for Stax belongs to a later Work Order; every
credential provider used by this Work Order is a test double.

Credentials are passed as opaque :class:`PublicationCredential` handles. The
handle's representation is always redacted and it is never included in
:class:`PublicationEvidence` or its serializable projection.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol, runtime_checkable

from tactus.domain import SourceRevision, WorkOrderId

from .models import (
    DEFAULT_TRUSTED_AUTHORITIES,
    PublicationAuthorization,
    PublicationEffect,
    PublicationError,
    PublicationGrant,
    PublicationIdentityConflictError,
    PublicationRequest,
    PublicationCredentialError,
    PublicationTransportError,
    raise_for_denial,
    validate_publication_grant,
)

__all__ = [
    "PublicationCredential",
    "PublicationCredentialProvider",
    "PublicationEvidence",
    "PublicationGuard",
    "PublicationReceipt",
    "PublicationTransport",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_FAILED = object()


def _require_aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PublicationError(f"{label} must be a timezone-aware datetime")
    return value


def _require_non_empty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PublicationError(f"{label} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class PublicationCredential:
    """An opaque, one-shot publication credential handle.

    The material is intentionally private and excluded from ``repr`` and
    equality. Callers receive the handle only to hand it to the transport; the
    guard never serializes it, logs it or copies it into evidence. A test fake
    may construct one with a recognizable sentinel value to prove redaction.
    """

    _material: str = field(repr=False, compare=False)
    provider_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "_material", _require_non_empty(self._material, "credential material")
        )
        if self.provider_id:
            object.__setattr__(
                self,
                "provider_id",
                _require_non_empty(self.provider_id, "credential provider_id"),
            )

    def __repr__(self) -> str:
        provider = f", provider={self.provider_id!r}" if self.provider_id else ""
        return f"PublicationCredential(<redacted>{provider})"

    def __str__(self) -> str:
        return "<redacted publication credential>"

    def __format__(self, format_spec: str) -> str:
        return str(self)

    def __reduce__(self) -> object:
        raise TypeError("PublicationCredential is not serializable")


@runtime_checkable
class PublicationCredentialProvider(Protocol):
    """Mock-only boundary that resolves exactly one credential per call.

    A real credential store is deliberately out of scope for this Work Order.
    Implementations must not attach the credential to the publication request,
    evidence or any process environment handed to a coding agent.
    """

    def resolve(
        self,
        grant: PublicationGrant,
        request: PublicationRequest,
        *,
        at: datetime,
    ) -> PublicationCredential:
        """Return an opaque credential handle for an already-authorized grant."""

        ...


@dataclass(frozen=True, slots=True)
class PublicationReceipt:
    """Result returned by a (mock) publication transport.

    The guard owns the stable ``publication_id``; a transport returns only its
    external reference and the time it completed. The reference is audit
    metadata, never credential material.
    """

    external_ref: str
    published_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "external_ref", _require_non_empty(self.external_ref, "external_ref")
        )
        object.__setattr__(
            self, "published_at", _require_aware(self.published_at, "published_at")
        )


@runtime_checkable
class PublicationTransport(Protocol):
    """Mock-only publication effect boundary.

    This Work Order deliberately ships no Git, GitHub or Stax implementation of
    this protocol. A future adapter consumes the opaque credential handle only
    after the guard has authorized the exact publication intent.
    """

    def publish(
        self,
        request: PublicationRequest,
        credential: PublicationCredential,
        *,
        publication_id: str,
        at: datetime,
    ) -> PublicationReceipt:
        """Perform one authorized publication and return an audit receipt."""

        ...


@dataclass(frozen=True, slots=True)
class PublicationEvidence:
    """Redacted, machine-checkable evidence for one authorized publication.

    There is intentionally no credential field. :meth:`as_dict` is the only
    supported serialization surface and contains identifiers, binding facts and
    timestamps only.
    """

    publication_id: str
    intent_id: str
    intent_digest: str
    grant_id: str
    authority: str
    work_order_id: WorkOrderId
    repository: str
    branch: str
    base_revision: str
    destination: str
    source_revision: SourceRevision
    scope: tuple[str, ...]
    effect: PublicationEffect
    authorized_at: datetime
    published_at: datetime
    external_ref: str
    schema_version: int = 1

    def as_dict(self) -> dict[str, object]:
        """Serialize only non-secret audit facts."""

        return {
            "authority": self.authority,
            "authorized_at": self.authorized_at.isoformat(),
            "base_revision": self.base_revision,
            "branch": self.branch,
            "destination": self.destination,
            "effect": self.effect.value,
            "external_ref": self.external_ref,
            "grant_id": self.grant_id,
            "intent_digest": self.intent_digest,
            "intent_id": self.intent_id,
            "publication_id": self.publication_id,
            "published_at": self.published_at.isoformat(),
            "redacted": True,
            "repository": self.repository,
            "schema_version": self.schema_version,
            "scope": list(self.scope),
            "source_revision": str(self.source_revision),
            "work_order_id": str(self.work_order_id),
        }


class PublicationGuard:
    """Validate publication grants and gate one-shot credential resolution.

    ``credentials`` and ``transport`` are injected so the default test suite
    never reads ambient credentials and never touches Git/GitHub. The guard
    keeps an in-process idempotency ledger keyed by the content-addressed
    ``publication_id``: repeating the same intent returns the original evidence
    instead of resolving another credential or publishing again. A reused
    ``intent_id`` with different content fails closed.
    """

    def __init__(
        self,
        credentials: PublicationCredentialProvider,
        transport: PublicationTransport,
        *,
        trusted_authorities: object = DEFAULT_TRUSTED_AUTHORITIES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(getattr(credentials, "resolve", None)):
            raise PublicationError("credentials must provide resolve()")
        if not callable(getattr(transport, "publish", None)):
            raise PublicationError("transport must provide publish()")
        if isinstance(trusted_authorities, str):
            raise PublicationError(
                "trusted_authorities must be an iterable of authorities, not a string"
            )
        try:
            raw_authorities = tuple(trusted_authorities)
        except TypeError:
            raise PublicationError(
                "trusted_authorities must be an iterable of authorities"
            ) from None
        trusted = frozenset(
            item.strip().casefold()
            for item in raw_authorities
            if isinstance(item, str) and item.strip()
        )
        if not trusted:
            raise PublicationError("trusted_authorities must not be empty")
        self._credentials = credentials
        self._transport = transport
        self._trusted_authorities = trusted
        self._clock = clock if clock is not None else _utcnow
        self._published: dict[str, PublicationEvidence] = {}
        self._intent_publications: dict[str, str] = {}
        self._intent_locks: dict[str, threading.Lock] = {}
        self._lock = threading.RLock()

    @property
    def trusted_authorities(self) -> frozenset[str]:
        return self._trusted_authorities

    def evaluate(
        self,
        request: PublicationRequest,
        grant: PublicationGrant,
        *,
        at: datetime | None = None,
    ) -> PublicationAuthorization:
        """Pure guard verdict; never resolves a credential."""

        checked_at = _require_aware(
            at if at is not None else self._clock(), "at"
        )
        return validate_publication_grant(
            request,
            grant,
            trusted_authorities=self._trusted_authorities,
            at=checked_at,
        )

    def authorize(
        self,
        request: PublicationRequest,
        grant: PublicationGrant,
        *,
        at: datetime | None = None,
    ) -> PublicationAuthorization:
        """Return the authorization or raise a typed, non-secret denial."""

        authorization = self.evaluate(request, grant, at=at)
        raise_for_denial(authorization)
        return authorization

    def publish(
        self,
        request: PublicationRequest,
        grant: PublicationGrant,
        *,
        at: datetime | None = None,
    ) -> PublicationEvidence:
        """Authorize, resolve one credential, publish once and return evidence.

        The credential provider is called only after every authorization check
        passes. If the same content-addressed intent has already completed,
        the original evidence is returned without another credential access or
        transport invocation.
        """

        checked_at = _require_aware(
            at if at is not None else self._clock(), "at"
        )
        authorization = self.authorize(request, grant, at=checked_at)

        # Serialize one intent so concurrent duplicates cannot both resolve a
        # credential or both publish. Different intents remain independent.
        with self._lock:
            intent_lock = self._intent_locks.setdefault(
                request.intent_id, threading.Lock()
            )
        with intent_lock:
            with self._lock:
                prior_publication = self._intent_publications.get(request.intent_id)
                if (
                    prior_publication is not None
                    and prior_publication != authorization.publication_id
                ):
                    raise PublicationIdentityConflictError(
                        f"intent {request.intent_id!r} was already used for publication "
                        f"{prior_publication!r}, not {authorization.publication_id!r}"
                    )
                existing = self._published.get(authorization.publication_id)
                if existing is not None:
                    return existing

            credential: object = _FAILED
            try:
                credential = self._credentials.resolve(grant, request, at=checked_at)
            except Exception:
                # The provider exception is deliberately not retained: its
                # message or traceback may contain credential material. Raise
                # a clean error outside the ``except`` block so even
                # ``__context__`` cannot expose the provider failure.
                pass
            if credential is _FAILED:
                raise PublicationCredentialError(
                    "credential provider failed to resolve a publication credential"
                ) from None
            if not isinstance(credential, PublicationCredential):
                raise PublicationCredentialError(
                    "credential provider must return a PublicationCredential"
                )

            receipt: object = _FAILED
            try:
                receipt = self._transport.publish(
                    request,
                    credential,
                    publication_id=authorization.publication_id,
                    at=checked_at,
                )
            except Exception:
                # As above, keep provider/transport internals out of the
                # exception chain; they may echo credential material.
                pass
            if receipt is _FAILED:
                raise PublicationTransportError(
                    "publication transport failed to complete the authorized publication"
                ) from None
            if not isinstance(receipt, PublicationReceipt):
                raise PublicationTransportError(
                    "publication transport must return a PublicationReceipt"
                )

            evidence = PublicationEvidence(
                publication_id=authorization.publication_id,
                intent_id=request.intent_id,
                intent_digest=request.intent_digest,
                grant_id=grant.grant_id,
                authority=grant.authority,
                work_order_id=request.work_order_id,
                repository=request.repository,
                branch=request.branch,
                base_revision=request.base_revision,
                destination=request.destination,
                source_revision=request.source_revision,
                scope=request.scope,
                effect=request.effect,
                authorized_at=authorization.checked_at,
                published_at=receipt.published_at,
                external_ref=receipt.external_ref,
            )

            with self._lock:
                self._published[evidence.publication_id] = evidence
                self._intent_publications[request.intent_id] = evidence.publication_id
            return evidence

    def evidence(
        self, publication_id: str
    ) -> PublicationEvidence | None:
        """Return recorded evidence for a stable publication identity, if any."""

        with self._lock:
            return self._published.get(publication_id)
