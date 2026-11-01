"""Durable, idempotent publication of one authorized review pull request.

The state machine here is shared by two ports that differ in exactly one
reviewed way:

* :class:`GitHubPublicationPort` is the **mocked** port. Its transport must be
  non-live and non-network, enforced in the constructor, and no flag, setting or
  environment value can change that.
* :class:`GitHubLivePublicationPort` is the **live** port over ``git push`` and
  ``gh api``. Its transport must be live and networked, it resolves the single
  credential before reading, and it still never merges, never force-pushes and
  never writes the trunk.

Both keep the same rules: authorization before any effect, one credential per
attempt, durable content-addressed identity, create/reuse/reconcile outcomes,
separated implementation and publication statuses, and redacted evidence that
names the grant that authorized the publication.

Ordering for one authorized intent::

    validate implementation evidence and exact change binding
        -> validate authority/binding/scope/freshness/revocation
        -> reuse a durable publication record, or refuse a changed digest
        -> read remote base/branch/duplicate state
        -> resolve exactly one opaque credential
        -> ensure one deterministic branch
        -> create (or reconcile) exactly one pull request
        -> durably record the branch + PR identity

A failure after publication starts returns ``PublicationStatus.FAILED`` with
``ImplementationStatus.VERIFIED`` unchanged: a publication problem never
invalidates the verified patch and never restarts agent editing.

See ``docs/architecture/MOCKED_PUBLICATION.md`` and
``docs/architecture/LIVE_PUBLICATION.md``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Iterable, Iterator, Mapping, Protocol

from tactus.integrations.github.transport import (
    GitHubAcknowledgmentLostError,
    GitHubBaseMovedError,
    GitHubBranchConflictError,
    GitHubDuplicatePullRequestError,
    GitHubPermissionDeniedError,
    GitHubProtectedDestinationError,
    GitHubPullRequest,
    GitHubTransport,
    GitHubTransportError,
    RemoteOperation,
)
from tactus.integrations.stax.source_control import PreparedChange

from .guard import PublicationCredential, PublicationCredentialProvider
from .models import (
    DEFAULT_TRUSTED_AUTHORITIES,
    PublicationEffect,
    PublicationGrant,
    PublicationRequest,
    validate_publication_grant,
)

__all__ = [
    "LIVE_PUBLICATION_CONTRACT_ID",
    "PUBLICATION_CONTRACT_ID",
    "PUBLICATION_VERSION",
    "GitHubLivePublicationPort",
    "GitHubPublicationPort",
    "ImplementationStatus",
    "PublicationBusyError",
    "PublicationContract",
    "PublicationErrorCode",
    "PublicationPort",
    "PublicationResult",
    "PublicationStateError",
    "PublicationStatus",
]

#: Version of the mocked publication protocol.
PUBLICATION_VERSION = "1.0.0"

#: Stable identifier for the publication protocol family.
PUBLICATION_CONTRACT_ID = "tactus.publication.github/v1"

#: Contract identifier for the live (git push + gh api) publication port.
LIVE_PUBLICATION_CONTRACT_ID = "tactus.publication.github.live/v1"

_PR_TITLE_TEMPLATE = "Tactus: publish {work_order_id}"
_PR_BODY_TEMPLATE = (
    "Tactus publication marker: {publication_id}\n"
    "\n"
    "Work Order: {work_order_id}\n"
    "Prepared change: {prepared_change_id}\n"
    "Patch digest: {patch_digest}\n"
)


class PublicationStateError(RuntimeError):
    """The durable publication state cannot be read or written safely."""


class PublicationBusyError(PublicationStateError):
    """Another publisher holds the lock for this publication identity."""


class ImplementationStatus(str, Enum):
    """Whether the consumed prepared change still proves implementation."""

    VERIFIED = "VERIFIED"
    NOT_VERIFIED = "NOT_VERIFIED"


class PublicationStatus(str, Enum):
    """Publication is a separate effect from implementation."""

    PUBLISHED = "PUBLISHED"
    REUSED = "REUSED"
    RECONCILED = "RECONCILED"
    REFUSED = "REFUSED"
    FAILED = "FAILED"


_SUCCEEDED = frozenset(
    {
        PublicationStatus.PUBLISHED,
        PublicationStatus.REUSED,
        PublicationStatus.RECONCILED,
    }
)


class PublicationErrorCode(str, Enum):
    """Deterministic reason codes for refused/failed publication attempts."""

    IMPLEMENTATION_NOT_VERIFIED = "implementation_not_verified"
    STALE_SOURCE = "stale_source"
    CHANGE_BINDING_MISMATCH = "change_binding_mismatch"
    INTENT_CONFLICT = "intent_conflict"
    DIGEST_CONFLICT = "digest_conflict"
    BASE_MISSING = "base_missing"
    BASE_MOVED = "base_moved"
    STALE_BRANCH = "stale_branch"
    DUPLICATE_PULL_REQUEST = "duplicate_pull_request"
    PUBLICATION_CONFLICT = "publication_conflict"
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"
    PERMISSION_DENIED = "permission_denied"
    PROTECTED_DESTINATION = "protected_destination"
    ACKNOWLEDGMENT_LOST = "acknowledgment_lost"
    TRANSPORT_FAILED = "transport_failed"
    STATE_ERROR = "state_error"


@dataclass(frozen=True, slots=True)
class PublicationContract:
    """Machine-readable contract for the mocked publication port."""

    contract_id: str = PUBLICATION_CONTRACT_ID
    version: str = PUBLICATION_VERSION
    transport: str = "fake"
    live: bool = False
    network: bool = False
    branch_operation: bool = True
    pull_request_operation: bool = True
    separate_statuses: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_id": self.contract_id,
            "version": self.version,
            "transport": self.transport,
            "live": self.live,
            "network": self.network,
            "branch_operation": self.branch_operation,
            "pull_request_operation": self.pull_request_operation,
            "separate_statuses": self.separate_statuses,
        }


@dataclass(frozen=True, slots=True)
class PublicationResult:
    """Outcome of one publication attempt.

    ``implementation_status`` is derived only from the verified prepared
    change. ``publication_status`` records what the publication effect did.
    Publication failure therefore cannot erase implementation evidence.
    """

    publication_id: str
    intent_id: str
    work_order_id: str
    grant_id: str
    prepared_change_id: str
    implementation_status: ImplementationStatus
    publication_status: PublicationStatus
    patch_digest: str
    branch: str
    branch_revision: str | None
    pull_request_number: int | None
    pull_request_url: str | None
    recorded_at: datetime | None
    error_code: str | None
    reason: str
    remote_operations: tuple[RemoteOperation, ...]
    network_calls: int

    @property
    def implementation_verified(self) -> bool:
        return self.implementation_status is ImplementationStatus.VERIFIED

    @property
    def publication_succeeded(self) -> bool:
        return self.publication_status in _SUCCEEDED

    @property
    def published(self) -> bool:
        return self.publication_succeeded

    def as_dict(self) -> dict[str, object]:
        return {
            "publication_id": self.publication_id,
            "intent_id": self.intent_id,
            "work_order_id": self.work_order_id,
            "grant_id": self.grant_id,
            "prepared_change_id": self.prepared_change_id,
            "implementation_status": self.implementation_status.value,
            "implementation_verified": self.implementation_verified,
            "publication_status": self.publication_status.value,
            "publication_succeeded": self.publication_succeeded,
            "patch_digest": self.patch_digest,
            "branch": self.branch,
            "branch_revision": self.branch_revision,
            "pull_request_number": self.pull_request_number,
            "pull_request_url": self.pull_request_url,
            "recorded_at": self.recorded_at.isoformat() if self.recorded_at else None,
            "error_code": self.error_code,
            "reason": self.reason,
            "remote_operations": [op.as_dict() for op in self.remote_operations],
            "network_calls": self.network_calls,
            "redacted": True,
        }


class PublicationPort(Protocol):
    """Versioned publication surface consumed by higher-level composition."""

    @property
    def contract(self) -> PublicationContract: ...

    def publish(
        self,
        request: PublicationRequest,
        grant: PublicationGrant,
        change: PreparedChange,
        *,
        at: datetime | None = None,
    ) -> PublicationResult: ...

    def evidence(self, publication_id: str) -> Mapping[str, Any] | None: ...


# -- helpers --------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{label} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _require_absolute_state_root(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise PublicationStateError("state_root must be an absolute path")
    path = path.resolve()
    if path.parent == path:
        raise PublicationStateError("state_root must not be a filesystem root")
    return path


def _digest_json(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp"
    try:
        temporary.write_text(
            json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    except OSError as exc:
        raise PublicationStateError(
            f"could not write publication state at {path}"
        ) from exc


@contextmanager
def _publication_lock(directory: Path, publication_id: str) -> Iterator[None]:
    path = directory / f"{publication_id}.lock"
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise PublicationStateError(
            f"could not open publication lock at {path}"
        ) from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PublicationBusyError(
                f"publication {publication_id!r} is already in progress"
            ) from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _record_conflict(
    record: Mapping[str, Any],
    request: PublicationRequest,
    change: PreparedChange,
) -> PublicationErrorCode | None:
    if record.get("publication_id") != request.publication_id:
        return PublicationErrorCode.INTENT_CONFLICT
    if record.get("intent_id") != request.intent_id:
        return PublicationErrorCode.INTENT_CONFLICT
    if record.get("intent_digest") != request.intent_digest:
        return PublicationErrorCode.INTENT_CONFLICT
    if record.get("patch_digest") != change.patch_digest:
        return PublicationErrorCode.DIGEST_CONFLICT
    if record.get("prepared_change_id") != change.prepared_change_id:
        return PublicationErrorCode.DIGEST_CONFLICT
    if record.get("branch_revision") != change.branch_revision:
        return PublicationErrorCode.DIGEST_CONFLICT
    return None


def _change_binding_error(
    request: PublicationRequest, change: PreparedChange
) -> tuple[PublicationErrorCode, str] | None:
    if not change.implementation_verified:
        return (
            PublicationErrorCode.IMPLEMENTATION_NOT_VERIFIED,
            "prepared change does not carry verified implementation evidence",
        )
    if change.patch_digest != change.implementation.patch_digest:
        return (
            PublicationErrorCode.IMPLEMENTATION_NOT_VERIFIED,
            "prepared change patch digest and verification digest disagree",
        )
    if change.branch_revision != change.implementation.branch_revision:
        return (
            PublicationErrorCode.IMPLEMENTATION_NOT_VERIFIED,
            "prepared change branch revision and verification revision disagree",
        )
    if change.work_order_id != str(request.work_order_id):
        return (
            PublicationErrorCode.CHANGE_BINDING_MISMATCH,
            "publication request and prepared change name different Work Orders",
        )
    if change.review_branch != request.branch:
        return (
            PublicationErrorCode.CHANGE_BINDING_MISMATCH,
            "publication request and prepared change name different review branches",
        )
    if change.base_revision != request.base_revision:
        return (
            PublicationErrorCode.CHANGE_BINDING_MISMATCH,
            "publication request and prepared change use different base revisions",
        )
    if change.source_revision != str(request.source_revision):
        return (
            PublicationErrorCode.STALE_SOURCE,
            "publication request source revision does not match the prepared change",
        )
    return None


class _PublicationStateMachine:
    """Shared durable publication state machine.

    One implementation serves both ports, so the create/reuse/reconcile rules
    cannot drift between the mocked and live paths. Which transports a concrete
    port accepts is decided here in the constructor, from the class and never
    from configuration: the mocked port refuses a live or networked transport,
    and the live port refuses anything that is not both.
    """

    #: Set by a concrete port class. Never a setting, flag or environment value.
    _requires_live_transport: ClassVar[bool] = False

    def __init__(
        self,
        transport: GitHubTransport,
        credentials: PublicationCredentialProvider,
        *,
        state_root: str | os.PathLike[str],
        trusted_authorities: Iterable[str] = DEFAULT_TRUSTED_AUTHORITIES,
        clock: Any = _utcnow,
    ) -> None:
        contract = getattr(transport, "contract", None)
        live = contract is None or bool(getattr(contract, "live", True))
        network = contract is None or bool(getattr(contract, "network", True))
        if self._requires_live_transport:
            if (
                contract is None
                or not bool(getattr(contract, "live", False))
                or not bool(getattr(contract, "network", False))
            ):
                raise PublicationStateError(
                    "live publication requires a live, networked transport"
                )
        elif live or network:
            raise PublicationStateError(
                "mocked publication requires a non-live, non-network transport"
            )
        self._transport = transport
        self._credentials = credentials
        self._state_root = _require_absolute_state_root(state_root)
        self._base = self._state_root / ".tactus" / "publications"
        self._records = self._base / "records"
        self._intents = self._base / "intents"
        self._attempts = self._base / "attempts"
        self._locks = self._base / "locks"
        for directory in (
            self._base,
            self._records,
            self._intents,
            self._attempts,
            self._locks,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self._trusted_authorities = tuple(trusted_authorities)
        if not self._trusted_authorities:
            raise PublicationStateError("trusted_authorities must not be empty")
        if not callable(clock):
            raise PublicationStateError("clock must be callable")
        self._clock = clock
        self._contract = (
            PublicationContract(
                contract_id=LIVE_PUBLICATION_CONTRACT_ID,
                version=PUBLICATION_VERSION,
                transport="gh",
                live=True,
                network=True,
            )
            if self._requires_live_transport
            else PublicationContract()
        )

    @property
    def contract(self) -> PublicationContract:
        return self._contract

    @property
    def state_root(self) -> Path:
        return self._state_root

    # -- durable state ---------------------------------------------------

    def _record_path(self, publication_id: str) -> Path:
        return self._records / f"{publication_id}.json"

    def _intent_path(self, intent_id: str) -> Path:
        return self._intents / f"{hashlib.sha256(intent_id.encode('utf-8')).hexdigest()}.json"

    def _load(self, path: Path) -> dict[str, Any] | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PublicationStateError(
                f"could not read publication state at {path}"
            ) from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PublicationStateError(
                f"publication state at {path} is not valid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise PublicationStateError(
                f"publication state at {path} is not an object"
            )
        return payload

    def _load_intent(self, intent_id: str) -> dict[str, Any] | None:
        payload = self._load(self._intent_path(intent_id))
        if payload is None:
            return None
        if payload.get("intent_id") != intent_id:
            raise PublicationStateError(
                f"intent index collision for {intent_id!r}"
            )
        return payload

    def _save_success(
        self,
        request: PublicationRequest,
        grant: PublicationGrant,
        change: PreparedChange,
        result: PublicationResult,
    ) -> dict[str, Any]:
        record = {
            "schema_version": 1,
            "publication_id": result.publication_id,
            "intent_id": request.intent_id,
            "intent_digest": request.intent_digest,
            "work_order_id": str(request.work_order_id),
            "repository": request.repository,
            "branch": result.branch,
            "base_revision": request.base_revision,
            "destination": request.destination,
            "source_revision": str(request.source_revision),
            "scope": list(request.scope),
            "effect": request.effect.value,
            "prepared_change_id": change.prepared_change_id,
            "patch_digest": change.patch_digest,
            "branch_revision": result.branch_revision,
            "pull_request_number": result.pull_request_number,
            "pull_request_url": result.pull_request_url,
            "grant_id": result.grant_id,
            "authority": grant.authority,
            "grant_issued_at": grant.issued_at.isoformat(),
            "grant_expires_at": (
                grant.expires_at.isoformat() if grant.expires_at else None
            ),
            "publication_status": result.publication_status.value,
            "published_at": (
                result.recorded_at.isoformat() if result.recorded_at else None
            ),
        }
        _atomic_write(self._record_path(result.publication_id), record)
        _atomic_write(
            self._intent_path(request.intent_id),
            {
                "schema_version": 1,
                "intent_id": request.intent_id,
                "publication_id": result.publication_id,
            },
        )
        return record

    def _write_attempt(self, result: PublicationResult) -> None:
        payload = result.as_dict()
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        stamp = _utcnow().strftime("%Y%m%dT%H%M%S%fZ")
        name = f"{stamp}-{result.publication_id}-{hashlib.sha256(encoded.encode()).hexdigest()[:12]}.json"
        _atomic_write(self._attempts / name, payload)

    def evidence(self, publication_id: str) -> dict[str, Any] | None:
        """Return the durable success record, if one exists."""

        return self._load(self._record_path(publication_id))

    # -- result construction --------------------------------------------

    def _result(
        self,
        request: PublicationRequest,
        grant: PublicationGrant,
        change: PreparedChange,
        status: PublicationStatus,
        *,
        branch_revision: str | None = None,
        pull_request: GitHubPullRequest | None = None,
        recorded_at: datetime | None = None,
        error_code: PublicationErrorCode | str | None = None,
        reason: str = "",
        operations: tuple[RemoteOperation, ...] = (),
    ) -> PublicationResult:
        implementation = (
            ImplementationStatus.VERIFIED
            if change.implementation_verified
            else ImplementationStatus.NOT_VERIFIED
        )
        return PublicationResult(
            publication_id=request.publication_id,
            intent_id=request.intent_id,
            work_order_id=str(request.work_order_id),
            grant_id=grant.grant_id,
            prepared_change_id=change.prepared_change_id,
            implementation_status=implementation,
            publication_status=status,
            patch_digest=change.patch_digest,
            branch=request.branch,
            branch_revision=branch_revision,
            pull_request_number=(
                pull_request.number if pull_request is not None else None
            ),
            pull_request_url=(
                pull_request.url if pull_request is not None else None
            ),
            recorded_at=recorded_at,
            error_code=(
                error_code.value if isinstance(error_code, PublicationErrorCode) else error_code
            ),
            reason=reason,
            remote_operations=operations,
            network_calls=self._transport.network_calls,
        )

    def _finish(self, result: PublicationResult) -> PublicationResult:
        self._write_attempt(result)
        return result

    def _refuse(
        self, result: PublicationResult
    ) -> PublicationResult:
        return self._finish(result)

    def _fail(self, result: PublicationResult) -> PublicationResult:
        return self._finish(result)

    # -- publication -----------------------------------------------------

    def publish(
        self,
        request: PublicationRequest,
        grant: PublicationGrant,
        change: PreparedChange,
        *,
        at: datetime | None = None,
    ) -> PublicationResult:
        """Publish one authorized intent, returning a separated-status result."""

        if not isinstance(request, PublicationRequest):
            raise TypeError("request must be a PublicationRequest")
        if not isinstance(grant, PublicationGrant):
            raise TypeError("grant must be a PublicationGrant")
        if not isinstance(change, PreparedChange):
            raise TypeError("change must be a PreparedChange")
        now = _require_aware(at if at is not None else self._clock(), "at")

        binding = _change_binding_error(request, change)
        if binding is not None:
            code, reason = binding
            return self._refuse(
                self._result(
                    request,
                    grant,
                    change,
                    PublicationStatus.REFUSED,
                    error_code=code,
                    reason=reason,
                )
            )

        authorization = validate_publication_grant(
            request,
            grant,
            trusted_authorities=self._trusted_authorities,
            at=now,
        )
        if not authorization.allowed:
            denial = (
                authorization.denial_code.value
                if authorization.denial_code is not None
                else PublicationErrorCode.CHANGE_BINDING_MISMATCH.value
            )
            return self._refuse(
                self._result(
                    request,
                    grant,
                    change,
                    PublicationStatus.REFUSED,
                    error_code=denial,
                    reason=authorization.reason,
                )
            )

        with _publication_lock(self._locks, request.publication_id):
            record = self._load(self._record_path(request.publication_id))
            if record is not None:
                conflict = _record_conflict(record, request, change)
                if conflict is not None:
                    return self._refuse(
                        self._result(
                            request,
                            grant,
                            change,
                            PublicationStatus.REFUSED,
                            error_code=conflict,
                            reason=(
                                "durable publication identity is bound to different "
                                "intent, digest or branch facts"
                            ),
                        )
                    )
                recorded_at = record.get("published_at")
                return self._finish(
                    self._result(
                        request,
                        grant,
                        change,
                        PublicationStatus.REUSED,
                        branch_revision=record.get("branch_revision"),
                        recorded_at=(
                            datetime.fromisoformat(recorded_at)
                            if isinstance(recorded_at, str)
                            else None
                        ),
                        reason="reused the durable publication identity",
                        pull_request=self._pull_request_from_record(record),
                    )
                )

            intent_record = self._load_intent(request.intent_id)
            if (
                intent_record is not None
                and intent_record.get("publication_id") != request.publication_id
            ):
                return self._refuse(
                    self._result(
                        request,
                        grant,
                        change,
                        PublicationStatus.REFUSED,
                        error_code=PublicationErrorCode.INTENT_CONFLICT,
                        reason=(
                            "this intent_id is already bound to a different "
                            "publication identity"
                        ),
                    )
                )

            return self._publish_locked(request, grant, change, now)

    def _pull_request_from_record(
        self, record: Mapping[str, Any]
    ) -> GitHubPullRequest | None:
        number = record.get("pull_request_number")
        url = record.get("pull_request_url")
        if number is None or not isinstance(url, str):
            return None
        return GitHubPullRequest(
            number=int(number),
            url=url,
            repository=str(record.get("repository", "")),
            head_branch=str(record.get("branch", "")),
            base_branch=str(record.get("destination", "")),
            head_revision=str(record.get("branch_revision", "")),
            base_revision=str(record.get("base_revision", "")),
            publication_id=str(record.get("publication_id", "")),
            title="",
            body="",
        )

    def _resolve_credential(
        self,
        request: PublicationRequest,
        grant: PublicationGrant,
        change: PreparedChange,
        now: datetime,
    ) -> PublicationCredential | PublicationResult:
        """Resolve exactly one opaque credential, or return the failed result.

        Provider text is never chained into the surfaced error: a provider
        message or traceback may contain credential material.
        """

        try:
            credential = self._credentials.resolve(grant, request, at=now)
        except Exception as exc:  # noqa: BLE001 - never expose provider text
            return self._fail(
                self._result(
                    request,
                    grant,
                    change,
                    PublicationStatus.FAILED,
                    error_code=PublicationErrorCode.CREDENTIAL_UNAVAILABLE,
                    reason=(
                        "publication credential provider failed closed "
                        f"({type(exc).__name__})"
                    ),
                )
            )
        if not isinstance(credential, PublicationCredential):
            return self._fail(
                self._result(
                    request,
                    grant,
                    change,
                    PublicationStatus.FAILED,
                    error_code=PublicationErrorCode.CREDENTIAL_UNAVAILABLE,
                    reason="credential provider did not return an opaque credential",
                )
            )
        return credential

    def _preflight_reads(
        self,
        request: PublicationRequest,
        grant: PublicationGrant,
        change: PreparedChange,
        now: datetime,
    ) -> PublicationResult | None:
        """Read the remote facts a publication depends on, before any mutation.

        Returns a finished result when those facts already decide the outcome (a
        moved base, a stale branch, an existing pull request to reconcile), and
        ``None`` when publication may proceed. Transport failures propagate so
        the caller records them as a failed attempt rather than crashing.
        """

        repository = request.repository
        base = self._transport.base_revision(repository, request.destination)
        if base is None:
            return self._fail(
                self._result(
                    request,
                    grant,
                    change,
                    PublicationStatus.FAILED,
                    error_code=PublicationErrorCode.BASE_MISSING,
                    reason="publication destination base ref does not exist",
                )
            )
        if base != request.base_revision:
            return self._fail(
                self._result(
                    request,
                    grant,
                    change,
                    PublicationStatus.FAILED,
                    error_code=PublicationErrorCode.BASE_MOVED,
                    reason=(
                        "publication destination base ref moved from the "
                        "authorized revision"
                    ),
                )
            )

        existing = self._transport.find_pull_requests(
            repository,
            head_branch=request.branch,
            base_branch=request.destination,
        )
        if len(existing) > 1:
            return self._fail(
                self._result(
                    request,
                    grant,
                    change,
                    PublicationStatus.FAILED,
                    error_code=PublicationErrorCode.DUPLICATE_PULL_REQUEST,
                    reason="multiple pull requests already match one head/base pair",
                )
            )
        if len(existing) == 1:
            pull_request = existing[0]
            if pull_request.publication_id != request.publication_id:
                return self._fail(
                    self._result(
                        request,
                        grant,
                        change,
                        PublicationStatus.FAILED,
                        error_code=PublicationErrorCode.PUBLICATION_CONFLICT,
                        reason=(
                            "an existing pull request for this branch belongs to "
                            "a different publication identity"
                        ),
                    )
                )
            if pull_request.head_revision != change.branch_revision:
                return self._fail(
                    self._result(
                        request,
                        grant,
                        change,
                        PublicationStatus.FAILED,
                        error_code=PublicationErrorCode.STALE_BRANCH,
                        reason=(
                            "existing pull request head does not match the "
                            "verified branch revision"
                        ),
                    )
                )
            result = self._result(
                request,
                grant,
                change,
                PublicationStatus.RECONCILED,
                branch_revision=change.branch_revision,
                pull_request=pull_request,
                recorded_at=now,
                reason="reconciled an existing pull request for this publication",
            )
            self._save_success(request, grant, change, result)
            return self._finish(result)

        remote_branch = self._transport.branch_revision(repository, request.branch)
        if remote_branch is not None and remote_branch != change.branch_revision:
            return self._fail(
                self._result(
                    request,
                    grant,
                    change,
                    PublicationStatus.FAILED,
                    error_code=PublicationErrorCode.STALE_BRANCH,
                    reason=(
                        "remote review branch is at a different revision than "
                        "the verified prepared change"
                    ),
                )
            )
        return None

    def _publish_locked(
        self,
        request: PublicationRequest,
        grant: PublicationGrant,
        change: PreparedChange,
        now: datetime,
    ) -> PublicationResult:
        repository = request.repository

        # A live transport cannot read a private repository without the
        # credential, so a live contract resolves and binds the one credential
        # *before* any read. A fake needs no credential to read, so it keeps the
        # stricter order and resolves only once a publication is certain.
        resolved: PublicationCredential | PublicationResult | None = None
        if self._transport.contract.reads_require_credential:
            resolved = self._resolve_credential(request, grant, change, now)
            if isinstance(resolved, PublicationResult):
                return resolved
            self._transport.bind_credential(resolved)

        try:
            preflight_failure = self._preflight_reads(request, grant, change, now)
        except GitHubTransportError as exc:
            # A live read can fail (401, rate limit, 5xx, network). That is a
            # failed attempt, not a crash: it must come back as a result and be
            # recorded as evidence like every other outcome.
            return self._fail(
                self._result(
                    request,
                    grant,
                    change,
                    PublicationStatus.FAILED,
                    error_code=PublicationErrorCode.TRANSPORT_FAILED,
                    reason=f"publication transport read failed ({type(exc).__name__})",
                )
            )
        if preflight_failure is not None:
            return preflight_failure

        if not isinstance(resolved, PublicationCredential):
            resolved = self._resolve_credential(request, grant, change, now)
            if isinstance(resolved, PublicationResult):
                return resolved
            self._transport.bind_credential(resolved)

        try:
            branch_revision = self._transport.ensure_branch(
                repository, request.branch, change.branch_revision
            )
        except GitHubProtectedDestinationError as exc:
            return self._refuse(
                self._result(
                    request,
                    grant,
                    change,
                    PublicationStatus.REFUSED,
                    error_code=PublicationErrorCode.PROTECTED_DESTINATION,
                    reason=(
                        "the publication transport refused a protected "
                        f"destination ({exc.ref!r}): {exc.reason}"
                    ),
                )
            )
        except GitHubBranchConflictError:
            return self._fail(
                self._result(
                    request,
                    grant,
                    change,
                    PublicationStatus.FAILED,
                    error_code=PublicationErrorCode.STALE_BRANCH,
                    reason="remote review branch conflicts with the verified revision",
                )
            )
        except GitHubPermissionDeniedError:
            return self._fail(
                self._result(
                    request,
                    grant,
                    change,
                    PublicationStatus.FAILED,
                    error_code=PublicationErrorCode.PERMISSION_DENIED,
                    reason="GitHub denied branch publication",
                )
            )
        except GitHubTransportError as exc:
            return self._fail(
                self._result(
                    request,
                    grant,
                    change,
                    PublicationStatus.FAILED,
                    error_code=PublicationErrorCode.TRANSPORT_FAILED,
                    reason=(
                        "GitHub branch publication failed "
                        f"({type(exc).__name__})"
                    ),
                )
            )

        if request.effect is PublicationEffect.PUBLISH_REVIEW_BRANCH:
            result = self._result(
                request,
                grant,
                change,
                PublicationStatus.PUBLISHED,
                branch_revision=branch_revision,
                recorded_at=now,
                reason="published the deterministic review branch only",
                operations=self._transport.operations,
            )
            self._save_success(request, grant, change, result)
            return self._finish(result)

        title = _PR_TITLE_TEMPLATE.format(work_order_id=request.work_order_id)
        body = _PR_BODY_TEMPLATE.format(
            publication_id=request.publication_id,
            work_order_id=request.work_order_id,
            prepared_change_id=change.prepared_change_id,
            patch_digest=change.patch_digest,
        )
        try:
            pull_request = self._transport.create_pull_request(
                repository,
                head_branch=request.branch,
                base_branch=request.destination,
                head_revision=branch_revision,
                expected_base_revision=request.base_revision,
                publication_id=request.publication_id,
                title=title,
                body=body,
            )
        except GitHubProtectedDestinationError as exc:
            return self._refuse(
                self._result(
                    request,
                    grant,
                    change,
                    PublicationStatus.REFUSED,
                    error_code=PublicationErrorCode.PROTECTED_DESTINATION,
                    reason=(
                        "the publication transport refused a protected "
                        f"destination ({exc.ref!r}): {exc.reason}"
                    ),
                    operations=self._transport.operations,
                )
            )
        except GitHubAcknowledgmentLostError:
            reconciled = self._reconcile_lost_acknowledgment(
                request, grant, change, now
            )
            return reconciled
        except GitHubPermissionDeniedError:
            return self._fail(
                self._result(
                    request,
                    grant,
                    change,
                    PublicationStatus.FAILED,
                    error_code=PublicationErrorCode.PERMISSION_DENIED,
                    reason="GitHub denied pull-request creation",
                    operations=self._transport.operations,
                )
            )
        except GitHubBaseMovedError:
            return self._fail(
                self._result(
                    request,
                    grant,
                    change,
                    PublicationStatus.FAILED,
                    error_code=PublicationErrorCode.BASE_MOVED,
                    reason="GitHub base ref moved during publication",
                    operations=self._transport.operations,
                )
            )
        except GitHubBranchConflictError:
            return self._fail(
                self._result(
                    request,
                    grant,
                    change,
                    PublicationStatus.FAILED,
                    error_code=PublicationErrorCode.STALE_BRANCH,
                    reason="GitHub review branch conflicts at creation time",
                    operations=self._transport.operations,
                )
            )
        except GitHubDuplicatePullRequestError:
            return self._fail(
                self._result(
                    request,
                    grant,
                    change,
                    PublicationStatus.FAILED,
                    error_code=PublicationErrorCode.DUPLICATE_PULL_REQUEST,
                    reason="GitHub already has a pull request for this head/base",
                    operations=self._transport.operations,
                )
            )
        except GitHubTransportError as exc:
            return self._fail(
                self._result(
                    request,
                    grant,
                    change,
                    PublicationStatus.FAILED,
                    error_code=PublicationErrorCode.TRANSPORT_FAILED,
                    reason=(
                        "GitHub pull-request creation failed "
                        f"({type(exc).__name__})"
                    ),
                    operations=self._transport.operations,
                )
            )

        result = self._result(
            request,
            grant,
            change,
            PublicationStatus.PUBLISHED,
            branch_revision=branch_revision,
            pull_request=pull_request,
            recorded_at=now,
            reason="published one deterministic review branch and pull request",
            operations=self._transport.operations,
        )
        self._save_success(request, grant, change, result)
        return self._finish(result)

    def _reconcile_lost_acknowledgment(
        self,
        request: PublicationRequest,
        grant: PublicationGrant,
        change: PreparedChange,
        now: datetime,
    ) -> PublicationResult:
        existing = self._transport.find_pull_requests(
            request.repository,
            head_branch=request.branch,
            base_branch=request.destination,
        )
        matching = [
            pull_request
            for pull_request in existing
            if pull_request.publication_id == request.publication_id
        ]
        if len(matching) != 1:
            return self._fail(
                self._result(
                    request,
                    grant,
                    change,
                    PublicationStatus.FAILED,
                    error_code=PublicationErrorCode.ACKNOWLEDGMENT_LOST,
                    reason=(
                        "pull-request creation acknowledgment was lost and could "
                        "not be reconciled to exactly one pull request"
                    ),
                    operations=self._transport.operations,
                )
            )
        pull_request = matching[0]
        if pull_request.head_revision != change.branch_revision:
            return self._fail(
                self._result(
                    request,
                    grant,
                    change,
                    PublicationStatus.FAILED,
                    error_code=PublicationErrorCode.STALE_BRANCH,
                    reason=(
                        "reconciled pull request head does not match the verified "
                        "branch revision"
                    ),
                    operations=self._transport.operations,
                )
            )
        result = self._result(
            request,
            grant,
            change,
            PublicationStatus.RECONCILED,
            branch_revision=pull_request.head_revision,
            pull_request=pull_request,
            recorded_at=now,
            reason="reconciled a pull request after a lost acknowledgment",
            operations=self._transport.operations,
        )
        self._save_success(request, grant, change, result)
        return self._finish(result)


class GitHubPublicationPort(_PublicationStateMachine):
    """Durable publication evidence over a deterministic fake transport.

    The transport must be non-live and non-network. This is enforced in the
    constructor and there is no flag, setting or environment value that changes
    it: a caller that wants a live publication must construct
    :class:`GitHubLivePublicationPort` explicitly, so the choice is visible in
    review rather than in configuration.
    """

    _requires_live_transport = False


class GitHubLivePublicationPort(_PublicationStateMachine):
    """Publication over a live transport: ``git push`` and ``gh api``.

    Behaviour differs from the mocked port in exactly two reviewed ways, both
    driven by the transport contract rather than by a setting:

    * the transport must be live **and** networked (refused otherwise); and
    * the single credential is resolved and bound *before* any read, because a
      live transport cannot read a private repository without it. The fake
      keeps the stricter order and resolves only once a publication is certain.

    Everything else - authorization first, one credential, one effect, durable
    idempotent records, separated implementation/publication statuses and
    redacted evidence - is the same state machine both ports share. This port
    still never merges, never force-pushes, never deploys and never restarts
    agent editing after a publication failure.
    """

    _requires_live_transport = True
