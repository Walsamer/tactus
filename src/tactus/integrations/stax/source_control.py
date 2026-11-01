"""Source-control preparation for one verified patch (no publication).

This module owns the narrow boundary between a verified implementation and a
review branch: it validates the exact source/base revision, the exact patch
digest and an authorized review-branch name, then stages the patch as a local
commit in a disposable Git worktree. It deliberately has **no publication
path**: there is no ``git push``, ``stax submit``, ``stax pr``, PR creation,
merge or credential handling here. A prepared change is therefore *not*
publication-eligible merely because it is verified.

The Stax workspace lifecycle (``StaxWorkspacePort``) provides the Git-isolated
worktree. This adapter adds:

1. request validation before any workspace mutation (exact revisions, patch
   digest, review-branch name);
2. a clean-workspace check that preserves dirty user work;
3. atomic patch application, commit provenance and target-branch ref creation;
4. a content-addressed change identity that is reused on repeated preparation
   and rejected when the same ``change_id`` is reused with different facts; and
5. typed outcomes that keep implementation verification separate from
   publication eligibility.

A Git worktree shares refs and object storage with its repository. That is a
Git isolation boundary, not an OS sandbox. Local branch refs are created in
that repository; ``main``/``master`` are never written and no remote is
contacted.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from tactus.domain import WorkOrderId

from .preflight import CommandResult, CommandRunner, SubprocessCommandRunner
from .workspace import WorkspaceLease, WorkspaceRequest

__all__ = [
    "CHANGE_ID_PREFIX",
    "DEFAULT_COMMIT_MESSAGE",
    "DEFAULT_MAX_PATCH_BYTES",
    "PROTECTED_BRANCHES",
    "REVIEW_BRANCH_PREFIX",
    "SOURCE_CONTROL_CONTRACT_ID",
    "SOURCE_CONTROL_VERSION",
    "ChangeIdentityConflictError",
    "ChangePreparationRequest",
    "ChangeRequestError",
    "DirtyWorkspaceError",
    "EmptyPatchError",
    "GitOperationError",
    "ImplementationOutcome",
    "ImplementationVerification",
    "PatchApplyError",
    "PatchDigestMismatchError",
    "PreparedChange",
    "PreparedChangeStateError",
    "PublicationEligibility",
    "RevisionBindingError",
    "ReviewBranchConflictError",
    "ReviewBranchError",
    "SourceControlBusyError",
    "SourceControlContract",
    "SourceControlError",
    "SourceControlPort",
    "SourceControlRequestError",
    "SourceRevisionMismatchError",
    "StaxSourceControlPort",
    "load_prepared_change",
    "WorkspaceProvider",
    "review_branch_for",
]

#: Version of the source-control preparation protocol. Bump for incompatible
#: request, identity or outcome changes.
SOURCE_CONTROL_VERSION = "1.0.0"

#: Stable identifier for the protocol family.
SOURCE_CONTROL_CONTRACT_ID = "tactus.source-control.preparation/v1"

#: Review branches must live under this namespace. A caller cannot name a
#: protected or arbitrary destination branch.
REVIEW_BRANCH_PREFIX = "agent/tactus/"

#: Branches that are never a valid review target. They are additionally
#: protected by never being written by this adapter.
PROTECTED_BRANCHES = frozenset({"main", "master"})

#: Prefix for the stable, content-addressed prepared-change identity.
CHANGE_ID_PREFIX = "chg-"

#: Default hard cap for a patch artifact (4 MiB).
DEFAULT_MAX_PATCH_BYTES = 4 * 1024 * 1024

#: Default local commit subject.
DEFAULT_COMMIT_MESSAGE = "Tactus: stage verified change"

_REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_PUBLICATION_REASON = (
    "verified implementation is not publication authority; a separate authorized "
    "publication grant is required and no transport or credential is consulted here"
)
_SCHEMA_VERSION = 1


# -- errors ---------------------------------------------------------------


class SourceControlError(RuntimeError):
    """Base class for typed source-control preparation refusals."""

    def __init__(
        self, message: str, *, evidence_path: Path | None = None
    ) -> None:
        super().__init__(message)
        self.evidence_path = evidence_path


class SourceControlRequestError(SourceControlError, ValueError):
    """The preparation request is malformed or violates the branch contract."""


class ChangeRequestError(SourceControlRequestError):
    """Compatibility alias for callers that name the request boundary."""


class RevisionBindingError(SourceControlRequestError):
    """The source and base revisions cannot be the same verified change."""


class SourceRevisionMismatchError(SourceControlError):
    """The workspace HEAD does not match the exact requested source revision."""

    def __init__(self, expected: str, actual: str, path: Path) -> None:
        self.expected = expected
        self.actual = actual
        self.path = Path(path)
        super().__init__(
            f"workspace source revision at {self.path} is {actual!r}; "
            f"expected exactly {expected!r}"
        )


class PatchDigestMismatchError(SourceControlError):
    """The supplied patch bytes do not match the exact expected digest."""

    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            "patch digest mismatch: expected "
            f"{expected}, got {actual}; refusing to stage unverified bytes"
        )


class ReviewBranchError(SourceControlRequestError):
    """The requested review branch is not an authorized, safe branch name."""


class ReviewBranchConflictError(SourceControlError):
    """The exact review branch already exists with different content."""

    def __init__(self, branch: str, expected: str, actual: str) -> None:
        self.branch = branch
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"review branch {branch!r} already exists at {actual!r}; "
            f"refusing to overwrite it (expected {expected!r})"
        )


class DirtyWorkspaceError(SourceControlError):
    """The workspace has uncommitted changes and is preserved untouched."""

    def __init__(self, path: Path, detail: str = "") -> None:
        self.path = Path(path)
        self.detail = detail
        suffix = f": {detail}" if detail else ""
        super().__init__(
            f"workspace {self.path} is dirty; preserving local changes and "
            f"refusing to stage or reset{suffix}"
        )


class EmptyPatchError(SourceControlError):
    """The patch was well-formed but changed no paths."""


class PatchApplyError(SourceControlError):
    """The patch could not be applied atomically to the exact base revision."""

    def __init__(self, detail: str, *, result: CommandResult | None = None) -> None:
        self.detail = detail
        self.result = result
        super().__init__(f"verified patch did not apply cleanly: {detail}")


class GitOperationError(SourceControlError):
    """A local Git command failed or returned an unexpected result."""

    def __init__(
        self,
        argv: Sequence[str],
        detail: str,
        *,
        result: CommandResult | None = None,
    ) -> None:
        self.argv = tuple(argv)
        self.detail = detail
        self.result = result
        super().__init__(f"git {' '.join(self.argv)} failed: {detail}")


class ChangeIdentityConflictError(SourceControlError):
    """One ``change_id`` was reused with different preparation facts."""

    def __init__(self, expected: Mapping[str, Any], actual: Mapping[str, Any]) -> None:
        self.expected = dict(expected)
        self.actual = dict(actual)
        super().__init__(
            "prepared-change identity conflict: the same change_id is already "
            f"bound to different facts (expected={self.expected}, "
            f"actual={self.actual})"
        )


class PreparedChangeStateError(SourceControlError):
    """Persisted preparation state and the shared repository disagree."""


class SourceControlBusyError(SourceControlError):
    """Another process holds the preparation lock for this change identity."""


# -- value objects --------------------------------------------------------


class ImplementationOutcome(str, Enum):
    """Typed result of validating and staging a patch.

    ``VERIFIED`` means the exact digest applied cleanly and a new local commit
    was staged. ``REUSED`` means an identical earlier preparation was found and
    nothing was re-applied. Neither value implies publication authority.
    """

    VERIFIED = "VERIFIED"
    REUSED = "REUSED"


class PublicationEligibility(str, Enum):
    """Publication state of a prepared change.

    This adapter has no publication path, so it only ever returns
    ``NOT_ELIGIBLE``. A later adapter may only become eligible with a separately
    validated publication grant; verified implementation is not enough.
    """

    NOT_ELIGIBLE = "NOT_ELIGIBLE"


@dataclass(frozen=True, slots=True)
class SourceControlContract:
    """Declarative description of the preparation boundary (versioned data)."""

    contract_id: str = SOURCE_CONTROL_CONTRACT_ID
    version: str = SOURCE_CONTROL_VERSION
    review_branch_prefix: str = REVIEW_BRANCH_PREFIX
    protected_branches: tuple[str, ...] = tuple(sorted(PROTECTED_BRANCHES))
    operations: tuple[str, ...] = ("prepare",)
    published: bool = False
    publication_authority: bool = False
    remaining_gates: tuple[str, ...] = (
        "source-control preparation creates only local refs; no push is authorized",
        "publication requires a separately validated grant and live transport",
        "a Git worktree shares refs/objects with its repository and is not an OS sandbox",
    )

    def __post_init__(self) -> None:
        if self.published:
            raise ValueError("source-control preparation may not publish")
        if self.publication_authority:
            raise ValueError("source-control preparation carries no publication authority")

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "version": self.version,
            "review_branch_prefix": self.review_branch_prefix,
            "protected_branches": list(self.protected_branches),
            "operations": list(self.operations),
            "published": self.published,
            "publication_authority": self.publication_authority,
            "remaining_gates": list(self.remaining_gates),
        }


@dataclass(frozen=True, slots=True)
class ImplementationVerification:
    """What the preparation actually proved about the implementation."""

    outcome: ImplementationOutcome
    source_revision: str
    base_revision: str
    patch_digest: str
    branch_revision: str
    checks: tuple[str, ...]

    @property
    def verified(self) -> bool:
        return self.outcome in {
            ImplementationOutcome.VERIFIED,
            ImplementationOutcome.REUSED,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "verified": self.verified,
            "source_revision": self.source_revision,
            "base_revision": self.base_revision,
            "patch_digest": self.patch_digest,
            "branch_revision": self.branch_revision,
            "checks": list(self.checks),
        }


@dataclass(frozen=True, slots=True)
class ChangePreparationRequest:
    """One verified patch to stage on one authorized review branch.

    ``source_revision`` is the exact commit the verified implementation was
    produced from; ``base_revision`` is the exact commit the review branch must
    start at. They must be equal here, so a patch can never be silently rebased.
    ``patch_digest`` is the exact lowercase SHA-256 of ``patch``. The review
    branch must live under :data:`REVIEW_BRANCH_PREFIX`.
    """

    change_id: str
    work_order_id: WorkOrderId | str
    repository: Path
    source_revision: str
    base_revision: str
    patch: bytes
    patch_digest: str
    review_branch: str
    commit_message: str = DEFAULT_COMMIT_MESSAGE
    attempt_id: str | None = None

    def __post_init__(self) -> None:
        change_id = _require_text(self.change_id, "change_id")
        if len(change_id) > 200:
            raise ChangeRequestError("change_id must be at most 200 characters")
        object.__setattr__(self, "change_id", change_id)

        if not isinstance(self.work_order_id, WorkOrderId):
            try:
                work_order_id = WorkOrderId(self.work_order_id)
            except (TypeError, ValueError) as exc:
                raise ChangeRequestError("work_order_id is required") from exc
        else:
            work_order_id = self.work_order_id
        object.__setattr__(self, "work_order_id", work_order_id)

        repository = _require_repository(self.repository)
        object.__setattr__(self, "repository", repository)

        object.__setattr__(
            self,
            "source_revision",
            _require_revision(self.source_revision, "source_revision"),
        )
        object.__setattr__(
            self,
            "base_revision",
            _require_revision(self.base_revision, "base_revision"),
        )
        if self.source_revision != self.base_revision:
            raise RevisionBindingError(
                "source_revision and base_revision must be the same exact commit; "
                f"got {self.source_revision!r} and {self.base_revision!r}"
            )

        if not isinstance(self.patch, (bytes, bytearray, memoryview)):
            raise ChangeRequestError("patch must be bytes")
        patch = bytes(self.patch)
        if not patch:
            raise ChangeRequestError("patch must not be empty")
        if len(patch) > DEFAULT_MAX_PATCH_BYTES:
            raise ChangeRequestError(
                f"patch is {len(patch)} bytes; limit is {DEFAULT_MAX_PATCH_BYTES}"
            )
        object.__setattr__(self, "patch", patch)

        object.__setattr__(
            self, "patch_digest", _require_digest(self.patch_digest, "patch_digest")
        )
        object.__setattr__(
            self, "review_branch", _require_review_branch(self.review_branch)
        )
        object.__setattr__(
            self, "commit_message", _require_text(self.commit_message, "commit_message")
        )
        if self.attempt_id is not None:
            attempt_id = _require_text(self.attempt_id, "attempt_id")
            if len(attempt_id) > 200:
                raise ChangeRequestError(
                    "attempt_id must be at most 200 characters"
                )
            object.__setattr__(self, "attempt_id", attempt_id)

    @property
    def resolved_attempt_id(self) -> str:
        """Stable workspace attempt id derived from the semantic change id."""

        if self.attempt_id:
            return self.attempt_id
        return "prepare-" + _digest_text(self.change_id)[:20]

    def _canonical(self) -> dict[str, Any]:
        return {
            "change_id": self.change_id,
            "work_order_id": str(self.work_order_id),
            "repository": os.fspath(self.repository),
            "source_revision": self.source_revision,
            "base_revision": self.base_revision,
            "patch_digest": self.patch_digest,
            "patch_bytes": len(self.patch),
            "review_branch": self.review_branch,
            "commit_message": self.commit_message,
            "attempt_id": self.resolved_attempt_id,
        }

    @property
    def fingerprint(self) -> str:
        """Digest of every preparation fact, including the exact patch digest."""

        return _digest_json(self._canonical())

    @property
    def prepared_change_id(self) -> str:
        """Stable, content-addressed prepared-change identity."""

        return CHANGE_ID_PREFIX + self.fingerprint[:32]

    def as_dict(self) -> dict[str, Any]:
        """Serializable request projection; never contains the raw patch."""

        return {
            **self._canonical(),
            "prepared_change_id": self.prepared_change_id,
        }


@dataclass(frozen=True, slots=True)
class PreparedChange:
    """Typed result of preparing one verified patch.

    ``implementation`` records what was verified/staged. ``publication`` is a
    separate, closed state and is always ``NOT_ELIGIBLE`` here: verifying a
    patch does not authorize publication.
    """

    contract_id: str
    contract_version: str
    change_id: str
    prepared_change_id: str
    work_order_id: str
    repository: str
    source_revision: str
    base_revision: str
    patch_digest: str
    patch_bytes: int
    review_branch: str
    branch_revision: str
    workspace_path: str
    workspace_present: bool
    implementation: ImplementationVerification
    publication: PublicationEligibility
    publication_reason: str
    prepared_at: datetime

    @property
    def implementation_verified(self) -> bool:
        return self.implementation.verified

    @property
    def implementation_outcome(self) -> ImplementationOutcome:
        return self.implementation.outcome

    @property
    def publication_eligibility(self) -> PublicationEligibility:
        return self.publication

    @property
    def publication_eligible(self) -> bool:
        # Verified implementation is never publication authorization.
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "change_id": self.change_id,
            "prepared_change_id": self.prepared_change_id,
            "work_order_id": self.work_order_id,
            "repository": self.repository,
            "source_revision": self.source_revision,
            "base_revision": self.base_revision,
            "patch_digest": self.patch_digest,
            "patch_bytes": self.patch_bytes,
            "review_branch": self.review_branch,
            "branch_revision": self.branch_revision,
            "workspace_path": self.workspace_path,
            "workspace_present": self.workspace_present,
            "implementation": self.implementation.as_dict(),
            "publication": self.publication.value,
            "publication_eligible": False,
            "publication_reason": self.publication_reason,
            "prepared_at": self.prepared_at.isoformat(),
        }


# -- protocols ------------------------------------------------------------


@runtime_checkable
class WorkspaceProvider(Protocol):
    """The subset of ``StaxWorkspacePort`` this adapter depends on."""

    def create(self, request: WorkspaceRequest) -> WorkspaceLease: ...


@runtime_checkable
class SourceControlPort(Protocol):
    """Versioned source-control preparation surface."""

    @property
    def contract(self) -> SourceControlContract: ...

    def prepare(self, request: ChangePreparationRequest) -> PreparedChange: ...


# -- validation helpers ---------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_text(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ChangeRequestError(f"{label} must be a string")
    value = value.strip()
    if not value:
        raise ChangeRequestError(f"{label} must be a non-empty string")
    if _CONTROL_RE.search(value):
        raise ChangeRequestError(f"{label} must not contain control characters")
    return value


def _require_repository(value: str | os.PathLike[str]) -> Path:
    try:
        path = Path(value).expanduser()
    except TypeError as exc:  # pragma: no cover - defensive
        raise ChangeRequestError("repository must be a path") from exc
    if not path.is_absolute():
        raise ChangeRequestError(
            f"repository must be an absolute path, got {os.fspath(path)!r}"
        )
    if not path.exists() or not path.is_dir():
        raise ChangeRequestError(f"repository is not a directory: {path}")
    return path.resolve()


def _require_revision(value: str, label: str) -> str:
    if not isinstance(value, str) or not _REVISION_RE.fullmatch(value):
        raise ChangeRequestError(
            f"{label} must be an exact lowercase 40- or 64-character Git object id"
        )
    return value


def _require_digest(value: str, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ChangeRequestError(
            f"{label} must be an exact lowercase 64-character SHA-256 digest"
        )
    return value


def _require_review_branch(value: str) -> str:
    branch = _require_text(value, "review_branch")
    if branch in PROTECTED_BRANCHES or branch.startswith("refs/"):
        raise ReviewBranchError(
            f"review_branch {branch!r} is protected; use a review branch under "
            f"{REVIEW_BRANCH_PREFIX!r}"
        )
    if not branch.startswith(REVIEW_BRANCH_PREFIX):
        raise ReviewBranchError(
            f"review_branch must start with {REVIEW_BRANCH_PREFIX!r}, got {branch!r}"
        )
    if (
        branch.startswith("-")
        or branch.startswith("/")
        or branch.endswith("/")
        or branch.endswith(".")
        or branch.endswith(".lock")
        or ".." in branch
        or "//" in branch
        or "@{" in branch
        or "\\" in branch
        or not _BRANCH_RE.fullmatch(branch)
    ):
        raise ReviewBranchError(f"review_branch {branch!r} is not a usable ref name")
    return branch


def _digest_json(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "change"


def review_branch_for(change_id: str) -> str:
    """Return the deterministic review branch name for a semantic change id."""

    change_id = _require_text(change_id, "change_id")
    return f"{REVIEW_BRANCH_PREFIX}{_slug(change_id)[:80]}"


# -- locking --------------------------------------------------------------


class _ChangeLock:
    """Non-blocking per-change flock held for one preparation."""

    def __init__(self, path: Path, fd: int) -> None:
        self.path = path
        self._fd = fd

    @classmethod
    def acquire(cls, path: Path, change_id: str) -> "_ChangeLock":
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise SourceControlBusyError(
                f"another preparation already holds {path}"
            ) from exc
        try:
            os.ftruncate(fd, 0)
            os.write(
                fd,
                json.dumps(
                    {"change_id": change_id, "pid": os.getpid()},
                    sort_keys=True,
                ).encode("utf-8"),
            )
        except OSError:
            pass
        return cls(path, fd)

    def release(self) -> None:
        if self._fd < 0:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = -1


# -- adapter --------------------------------------------------------------


class StaxSourceControlPort:
    """Validate and stage one verified patch without ever publishing it."""

    def __init__(
        self,
        workspace_port: WorkspaceProvider,
        *,
        state_root: Path,
        git_executable: str = "git",
        runner: CommandRunner | None = None,
        clock: Any = None,
        contract: SourceControlContract | None = None,
    ) -> None:
        if not isinstance(workspace_port, WorkspaceProvider):
            raise TypeError("a StaxWorkspacePort-compatible workspace provider is required")
        state_root = Path(state_root).expanduser()
        if not state_root.is_absolute():
            raise ValueError("state_root must be an absolute path")
        if not state_root.exists() or not state_root.is_dir():
            raise ValueError(f"state_root is not an existing directory: {state_root}")
        self._workspace_port = workspace_port
        self._state_root = state_root.resolve()
        self._git = os.fspath(git_executable)
        self._runner: CommandRunner = runner or SubprocessCommandRunner()
        self._clock = clock or _utcnow
        self._contract = contract or SourceControlContract()

    @property
    def contract(self) -> SourceControlContract:
        return self._contract

    def prepare(self, request: ChangePreparationRequest) -> PreparedChange:
        """Validate *request* and stage it once, reusing a stable identity."""

        if not isinstance(request, ChangePreparationRequest):
            raise ChangeRequestError("a ChangePreparationRequest is required")
        self._assert_state_root(request.repository)

        actual_digest = hashlib.sha256(request.patch).hexdigest()
        if actual_digest != request.patch_digest:
            error = PatchDigestMismatchError(request.patch_digest, actual_digest)
            raise self._attach_refusal("prepare", error, request)

        key = _digest_text(request.change_id)
        lock = _ChangeLock.acquire(
            self._state_path("locks", f"{key}.lock", create=True),
            request.change_id,
        )
        try:
            return self._prepare_locked(request, key)
        except Exception as exc:
            raise self._attach_refusal("prepare", exc, request) from exc
        finally:
            lock.release()

    # -- main flow -------------------------------------------------------

    def _prepare_locked(
        self, request: ChangePreparationRequest, key: str
    ) -> PreparedChange:
        manifest_path = self._state_path("changes", f"{key}.json", create=False)
        manifest = self._load_manifest(manifest_path)
        if manifest is not None:
            return self._reuse(request, manifest, manifest_path)

        self._require_repository(request.repository)
        self._require_branch_available(request.repository, request.review_branch)
        self._require_base_commit(request.repository, request.base_revision)

        try:
            lease = self._workspace_port.create(
                WorkspaceRequest(
                    attempt_id=request.resolved_attempt_id,
                    base_revision=request.base_revision,
                    repository=request.repository,
                )
            )
        except Exception as exc:  # typed workspace refusals pass through
            raise self._attach_refusal("prepare", exc, request) from exc

        workspace = Path(lease.path)
        try:
            self._require_workspace_clean(workspace, request.base_revision)
            patch_path = self._write_patch(request)
            self._apply_patch(workspace, patch_path)
            branch_revision = self._commit_patch(workspace, request)
            self._require_provenance(
                request.repository, branch_revision, request.base_revision
            )
            self._create_review_branch(
                request.repository, request.review_branch, branch_revision
            )
            prepared = PreparedChange(
                contract_id=self._contract.contract_id,
                contract_version=self._contract.version,
                change_id=request.change_id,
                prepared_change_id=request.prepared_change_id,
                work_order_id=str(request.work_order_id),
                repository=os.fspath(request.repository),
                source_revision=request.source_revision,
                base_revision=request.base_revision,
                patch_digest=request.patch_digest,
                patch_bytes=len(request.patch),
                review_branch=request.review_branch,
                branch_revision=branch_revision,
                workspace_path=os.fspath(workspace),
                workspace_present=workspace.is_dir(),
                implementation=ImplementationVerification(
                    outcome=ImplementationOutcome.VERIFIED,
                    source_revision=request.source_revision,
                    base_revision=request.base_revision,
                    patch_digest=request.patch_digest,
                    branch_revision=branch_revision,
                    checks=(
                        "source_revision==base_revision",
                        "workspace_head==base_revision",
                        "workspace_clean_before_apply",
                        "patch_digest_matches_exact_bytes",
                        "patch_applied_atomically",
                        "commit_parent==base_revision",
                        "review_branch_ref==branch_revision",
                    ),
                ),
                publication=PublicationEligibility.NOT_ELIGIBLE,
                publication_reason=_PUBLICATION_REASON,
                prepared_at=self._clock(),
            )
            self._write_manifest(manifest_path, request, prepared)
            return prepared
        except Exception as exc:
            raise self._attach_refusal("prepare", exc, request) from exc
        finally:
            lease.close()

    def _reuse(
        self,
        request: ChangePreparationRequest,
        manifest: Mapping[str, Any],
        manifest_path: Path,
    ) -> PreparedChange:
        recorded = manifest.get("facts")
        if not isinstance(recorded, Mapping):
            raise PreparedChangeStateError(
                f"preparation manifest {manifest_path} has no facts"
            )
        actual = request._canonical()
        if dict(recorded) != actual:
            raise ChangeIdentityConflictError(recorded, actual)
        if manifest.get("request_fingerprint") != request.fingerprint:
            raise ChangeIdentityConflictError(recorded, actual)

        branch = str(manifest.get("review_branch", ""))
        branch_revision = str(manifest.get("branch_revision", ""))
        if branch != request.review_branch:
            raise ChangeIdentityConflictError(recorded, actual)
        if not _REVISION_RE.fullmatch(branch_revision):
            raise PreparedChangeStateError(
                f"manifest {manifest_path} has an invalid branch revision"
            )

        self._require_repository(request.repository)
        self._require_branch_at(request.repository, branch, branch_revision)
        self._require_provenance(
            request.repository, branch_revision, request.base_revision
        )

        workspace = Path(str(manifest.get("workspace_path", "")))
        if not workspace.is_absolute():
            raise PreparedChangeStateError(
                f"manifest {manifest_path} has an invalid workspace path"
            )
        present = workspace.is_dir()
        if present:
            self._require_workspace_clean(workspace, branch_revision)
        return PreparedChange(
            contract_id=self._contract.contract_id,
            contract_version=self._contract.version,
            change_id=request.change_id,
            prepared_change_id=request.prepared_change_id,
            work_order_id=str(request.work_order_id),
            repository=os.fspath(request.repository),
            source_revision=request.source_revision,
            base_revision=request.base_revision,
            patch_digest=request.patch_digest,
            patch_bytes=len(request.patch),
            review_branch=branch,
            branch_revision=branch_revision,
            workspace_path=os.fspath(workspace),
            workspace_present=present,
            implementation=ImplementationVerification(
                outcome=ImplementationOutcome.REUSED,
                source_revision=request.source_revision,
                base_revision=request.base_revision,
                patch_digest=request.patch_digest,
                branch_revision=branch_revision,
                checks=(
                    "request_fingerprint_matches_manifest",
                    "review_branch_ref==recorded_branch_revision",
                    "commit_parent==base_revision",
                    "workspace_clean_before_reuse" if present else "workspace_path_absent",
                ),
            ),
            publication=PublicationEligibility.NOT_ELIGIBLE,
            publication_reason=_PUBLICATION_REASON,
            prepared_at=self._clock(),
        )

    # -- preflight -------------------------------------------------------

    def _assert_state_root(self, repository: Path) -> None:
        root = self._state_root
        if root == Path(root.anchor):
            raise SourceControlRequestError(
                "state_root must not be a filesystem root"
            )
        if root == repository or repository in root.parents:
            raise SourceControlRequestError(
                f"state_root {root} must be outside the source repository {repository}"
            )

    def _require_repository(self, repository: Path) -> None:
        result = self._run_git(
            ("rev-parse", "--git-dir"),
            cwd=Path(repository),
            allow_failure=True,
        )
        if result.returncode != 0:
            raise SourceControlRequestError(
                f"repository is not a usable Git repository: {repository}"
            )

    def _require_base_commit(self, repository: Path, revision: str) -> None:
        result = self._run_git(
            ("cat-file", "-e", f"{revision}^{{commit}}"),
            cwd=Path(repository),
            allow_failure=True,
        )
        if result.returncode != 0:
            raise SourceRevisionMismatchError(
                revision, "missing commit", Path(repository)
            )

    def _require_workspace_clean(self, workspace: Path, base_revision: str) -> None:
        if not workspace.is_dir():
            raise PreparedChangeStateError(
                f"workspace path is missing: {workspace}"
            )
        status = self._run_git(
            ("status", "--porcelain=v1", "--untracked-files=all"),
            cwd=workspace,
        )
        if status.stdout.strip():
            raise DirtyWorkspaceError(
                workspace, "workspace has uncommitted changes"
            )
        head = self._git_rev_parse(workspace, "HEAD^{commit}")
        if head != base_revision:
            raise SourceRevisionMismatchError(base_revision, head, workspace)

    def _require_branch_available(self, repository: Path, branch: str) -> None:
        existing = self._git_rev_parse_optional(
            Path(repository), f"refs/heads/{branch}"
        )
        if existing is not None:
            raise ReviewBranchConflictError(
                branch, "a new review branch", existing
            )

    def _require_branch_at(
        self, repository: Path, branch: str, revision: str
    ) -> None:
        actual = self._git_rev_parse_optional(
            Path(repository), f"refs/heads/{branch}"
        )
        if actual is None:
            raise PreparedChangeStateError(
                f"review branch {branch!r} is missing from the shared repository"
            )
        if actual != revision:
            raise ReviewBranchConflictError(branch, revision, actual)

    def _require_provenance(
        self, repository: Path, commit: str, base_revision: str
    ) -> None:
        parent = self._run_git(
            ("rev-parse", "--verify", f"{commit}^"),
            cwd=Path(repository),
        ).stdout.strip()
        if parent != base_revision:
            raise PreparedChangeStateError(
                f"commit {commit} is not a single-step child of {base_revision}; "
                f"found parent {parent!r}"
            )

    # -- staging ---------------------------------------------------------

    def _write_patch(self, request: ChangePreparationRequest) -> Path:
        patch_dir = self._state_path("changes", "patches", create=True)
        patch_dir.mkdir(parents=True, exist_ok=True)
        target = patch_dir / f"{request.patch_digest}.patch"
        temporary = patch_dir / f".{request.patch_digest}.tmp"
        try:
            temporary.write_bytes(request.patch)
            os.replace(temporary, target)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise SourceControlError(f"cannot persist patch evidence: {exc}") from exc
        return target

    def _apply_patch(self, workspace: Path, patch_path: Path) -> None:
        result = self._run_git(
            (
                "apply",
                "--index",
                "--whitespace=nowarn",
                "--",
                os.fspath(patch_path),
            ),
            cwd=workspace,
            allow_failure=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "git apply failed"
            raise PatchApplyError(detail, result=result)
        changed = self._run_git(
            ("diff", "--cached", "--name-only", "-z"), cwd=workspace
        ).stdout
        if not [item for item in changed.split("\x00") if item]:
            raise EmptyPatchError("patch applied but changed no paths")

    def _commit_patch(
        self, workspace: Path, request: ChangePreparationRequest
    ) -> str:
        result = self._run_git(
            (
                "-c",
                "user.name=Tactus",
                "-c",
                "user.email=tactus@tactus.invalid",
                "commit",
                "-m",
                request.commit_message,
            ),
            cwd=workspace,
            allow_failure=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "git commit failed"
            raise GitOperationError(("commit",), detail, result=result)
        commit = self._git_rev_parse(workspace, "HEAD^{commit}")
        if commit == request.base_revision:
            raise EmptyPatchError("patch commit did not create a new revision")
        return commit

    def _create_review_branch(
        self, repository: Path, branch: str, revision: str
    ) -> None:
        result = self._run_git(
            ("branch", branch, revision),
            cwd=Path(repository),
            allow_failure=True,
        )
        if result.returncode != 0:
            existing = self._git_rev_parse_optional(
                Path(repository), f"refs/heads/{branch}"
            )
            if existing is not None and existing != revision:
                raise ReviewBranchConflictError(branch, revision, existing)
            detail = result.stderr.strip() or result.stdout.strip() or "git branch failed"
            raise GitOperationError(("branch", branch, revision), detail, result=result)
        actual = self._git_rev_parse(
            Path(repository), f"refs/heads/{branch}"
        )
        if actual != revision:
            raise PreparedChangeStateError(
                f"review branch {branch!r} points at {actual}, expected {revision}"
            )

    # -- git -------------------------------------------------------------

    def _run_git(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        allow_failure: bool = False,
    ) -> CommandResult:
        argv = (self._git, *args)
        try:
            result = self._runner(argv, cwd=Path(cwd).resolve())
        except Exception as exc:
            raise GitOperationError(argv, str(exc)) from exc
        if result.returncode != 0 and not allow_failure:
            detail = result.stderr.strip() or result.stdout.strip() or "non-zero exit"
            raise GitOperationError(argv, detail, result=result)
        return result

    def _git_rev_parse(self, cwd: Path, ref: str) -> str:
        return self._run_git(
            ("rev-parse", "--verify", ref), cwd=Path(cwd)
        ).stdout.strip()

    def _git_rev_parse_optional(self, cwd: Path, ref: str) -> str | None:
        result = self._run_git(
            ("rev-parse", "--verify", "--quiet", ref),
            cwd=Path(cwd),
            allow_failure=True,
        )
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        return value or None

    # -- state -----------------------------------------------------------

    def _state_path(self, kind: str, name: str, *, create: bool) -> Path:
        directory = self._state_root / ".tactus" / kind
        if create:
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise SourceControlError(
                    f"cannot create preparation state directory {directory}: {exc}"
                ) from exc
        return directory / name

    def _load_manifest(self, path: Path) -> Mapping[str, Any] | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PreparedChangeStateError(
                f"cannot read preparation manifest {path}: {exc}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise PreparedChangeStateError(
                f"preparation manifest {path} is not an object"
            )
        if payload.get("schema_version") != _SCHEMA_VERSION:
            raise PreparedChangeStateError(
                f"preparation manifest {path} has an unsupported schema"
            )
        return payload

    def _write_manifest(
        self,
        path: Path,
        request: ChangePreparationRequest,
        prepared: PreparedChange,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "change_id": request.change_id,
            "prepared_change_id": request.prepared_change_id,
            "request_fingerprint": request.fingerprint,
            "facts": request._canonical(),
            "review_branch": prepared.review_branch,
            "branch_revision": prepared.branch_revision,
            "workspace_path": prepared.workspace_path,
            "patch_digest": prepared.patch_digest,
            "patch_bytes": prepared.patch_bytes,
            "created_at": prepared.prepared_at.isoformat(),
        }
        temporary = path.parent / f".{path.name}.tmp"
        try:
            temporary.write_text(
                json.dumps(payload, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise SourceControlError(
                f"cannot persist preparation manifest {path}: {exc}"
            ) from exc

    def _attach_refusal(
        self,
        operation: str,
        exc: Exception,
        request: ChangePreparationRequest,
    ) -> Exception:
        if isinstance(exc, SourceControlError) and exc.evidence_path is not None:
            return exc
        try:
            directory = self._state_path("refusals", "", create=True)
            stamp = self._clock().strftime("%Y%m%dT%H%M%S%fZ")
            name = f"{stamp}-{_digest_text(request.change_id)[:12]}-{operation}.json"
            target = directory / name
            payload = {
                "schema_version": _SCHEMA_VERSION,
                "operation": operation,
                "reason": type(exc).__name__,
                "message": str(exc),
                "change_id": request.change_id,
                "prepared_change_id": request.prepared_change_id,
                "work_order_id": str(request.work_order_id),
                "repository": os.fspath(request.repository),
                "source_revision": request.source_revision,
                "base_revision": request.base_revision,
                "patch_digest": request.patch_digest,
                "review_branch": request.review_branch,
                "recorded_at": self._clock().isoformat(),
            }
            temporary = directory / f".{name}.tmp"
            temporary.write_text(
                json.dumps(payload, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, target)
        except Exception:
            return exc
        if isinstance(exc, SourceControlError):
            exc.evidence_path = target
        return exc


def load_prepared_change(
    state_root: str | os.PathLike[str], change_id: str
) -> PreparedChange:
    """Load one durably recorded preparation by change identity.

    Read-only, and deliberately not a second preparation: it reconstructs the
    facts the adapter already recorded and never applies a patch, touches a
    workspace, creates a ref or re-runs a check. A publication composition needs
    *the* verified change the operator prepared, addressed by identity.

    The recorded outcome is ``REUSED`` with manifest-derived checks, because a
    record proves what was prepared, not that verification ran again now.
    """

    if not isinstance(change_id, str) or not change_id.strip():
        raise PreparedChangeStateError("change_id must be a non-empty string")
    root = Path(os.fspath(state_root)).expanduser()
    if not root.is_absolute():
        raise PreparedChangeStateError("state_root must be an absolute path")
    root = root.resolve()
    if root == Path(root.anchor):
        raise PreparedChangeStateError("state_root must not be a filesystem root")

    path = root / ".tactus" / "changes" / f"{_digest_text(change_id.strip())}.json"
    if not path.exists():
        raise PreparedChangeStateError(
            f"no recorded preparation for change {change_id!r} under {root}"
        )
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PreparedChangeStateError(
            f"cannot read preparation manifest {path}: {exc}"
        ) from exc
    if not isinstance(manifest, Mapping):
        raise PreparedChangeStateError(f"preparation manifest {path} is not an object")
    if manifest.get("schema_version") != _SCHEMA_VERSION:
        raise PreparedChangeStateError(
            f"preparation manifest {path} has an unsupported schema"
        )
    facts = manifest.get("facts")
    if not isinstance(facts, Mapping):
        raise PreparedChangeStateError(f"preparation manifest {path} has no facts")
    if facts.get("change_id") != change_id:
        raise PreparedChangeStateError(
            f"manifest {path} records change {facts.get('change_id')!r}, "
            f"not {change_id!r}"
        )

    branch = str(manifest.get("review_branch", ""))
    branch_revision = str(manifest.get("branch_revision", ""))
    if not _REVISION_RE.fullmatch(branch_revision):
        raise PreparedChangeStateError(
            f"manifest {path} has an invalid branch revision"
        )
    workspace = Path(str(manifest.get("workspace_path", "")))
    if not workspace.is_absolute():
        raise PreparedChangeStateError(f"manifest {path} has an invalid workspace path")
    created_at = manifest.get("created_at")
    try:
        prepared_at = datetime.fromisoformat(str(created_at))
    except ValueError as exc:
        raise PreparedChangeStateError(
            f"manifest {path} has an invalid preparation time"
        ) from exc
    if prepared_at.tzinfo is None or prepared_at.utcoffset() is None:
        raise PreparedChangeStateError(
            f"manifest {path} has a timezone-naive preparation time"
        )

    source_revision = str(facts.get("source_revision", ""))
    base_revision = str(facts.get("base_revision", ""))
    patch_digest = str(facts.get("patch_digest", ""))
    return PreparedChange(
        contract_id=SOURCE_CONTROL_CONTRACT_ID,
        contract_version=SOURCE_CONTROL_VERSION,
        change_id=change_id,
        prepared_change_id=str(manifest.get("prepared_change_id", "")),
        work_order_id=str(facts.get("work_order_id", "")),
        repository=str(facts.get("repository", "")),
        source_revision=source_revision,
        base_revision=base_revision,
        patch_digest=patch_digest,
        patch_bytes=int(manifest.get("patch_bytes", 0)),
        review_branch=branch,
        branch_revision=branch_revision,
        workspace_path=os.fspath(workspace),
        workspace_present=workspace.is_dir(),
        implementation=ImplementationVerification(
            outcome=ImplementationOutcome.REUSED,
            source_revision=source_revision,
            base_revision=base_revision,
            patch_digest=patch_digest,
            branch_revision=branch_revision,
            checks=(
                "loaded_from_recorded_manifest",
                "manifest_schema_supported",
                "manifest_change_identity_matches",
            ),
        ),
        publication=PublicationEligibility.NOT_ELIGIBLE,
        publication_reason=_PUBLICATION_REASON,
        prepared_at=prepared_at,
    )
