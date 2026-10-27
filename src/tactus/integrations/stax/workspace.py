"""Stax-backed workspace lifecycle: create, path, list and safe removal.

This is the Tactus runtime workspace port for one semantic attempt. It is a
**Git isolation** boundary, not an OS/security sandbox: the agent runs as the
same user with the same filesystem and network reach. Even Git-worktree
isolation does not prevent a same-user process from reading the canonical
checkout or using ambient credentials.

The port deliberately has a tiny, reviewable surface:

* ``create`` provisions or reuses exactly one deterministic worktree per
  attempt, after the pinned Stax preflight has passed;
* ``path`` reports the existing worktree only when it still matches its
  persisted identity;
* ``list`` reports Tactus-owned worktrees and their ownership state; and
* ``remove`` refuses dirty or unowned worktrees and never forces cleanup.

Safety invariants
-----------------
1. **Identity before mutation.** A workspace identity binds ``attempt_id``,
   ``base_revision``, ``branch`` and the absolute worktree ``path``. The
   deterministic workspace name depends only on the attempt id, so a changed
   base revision for the same attempt is a conflict, not a new workspace.
2. **Exact pin first.** :class:`~tactus.integrations.stax.preflight.StaxPreflight`
   runs before every Stax invocation. Unknown, off-pin or wrong-version binaries
   are rejected before a worktree command can mutate anything.
3. **Exclusive ownership.** A non-blocking ``flock`` per workspace is held for the
   life of :class:`WorkspaceLease`; a concurrent attempt fails with
   :class:`WorkspaceInUseError` without touching the workspace.
4. **Containment.** Every reported worktree path must resolve strictly inside the
   configured Tactus workspace root and outside the source repository.
5. **Dirty workspaces are preserved.** Removal relies on Stax's own dirty status
   and never passes ``--force`` or ``--delete-branch``. A refusal keeps the
   worktree, its ownership manifest and a JSON evidence record.
6. **No publication.** This module never runs ``stax submit``, ``stax pr``,
   ``stax lane``, ``stax update``, ``stax init``, ``stax sync``, ``stax restack``
   or ``git reset --hard``/``git stash``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from errno import EACCES as errno_EACCES, EAGAIN as errno_EAGAIN
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .pin import PINNED_STAX, StaxPin
from .preflight import (
    CommandResult,
    CommandRunner,
    StaxPreflight,
    StaxPreflightError,
    SubprocessCommandRunner,
    sha256_file,
)

__all__ = [
    "WORKSPACE_NAME_PREFIX",
    "WORKSPACE_STATE_DIR",
    "StaxWorkspaceError",
    "StaxWorkspacePort",
    "WorkspaceBoundaryError",
    "WorkspaceCommandError",
    "WorkspaceContainmentError",
    "WorkspaceCreateError",
    "WorkspaceDirtyError",
    "WorkspaceIdentity",
    "WorkspaceIdentityConflictError",
    "WorkspaceInUseError",
    "WorkspaceLease",
    "WorkspaceNotFoundError",
    "WorkspaceOwnershipError",
    "WorkspacePreflightError",
    "WorkspaceRecord",
    "WorkspaceRemoval",
    "WorkspaceRemoveError",
    "WorkspaceRequest",
    "WorkspaceRequestError",
    "WorkspaceSourceDriftError",
    "WorkspaceStateError",
    "WorkspaceUnsupportedPlatformError",
]

#: Prefix reserved for worktrees created through this port.
WORKSPACE_NAME_PREFIX = "tactus-wt-"

#: Tactus-owned lifecycle state, always under the configured workspace root.
WORKSPACE_STATE_DIR = ".tactus"

SCHEMA_VERSION = 1
_MAX_ATTEMPT_ID = 200
_MAX_OUTPUT_CHARS = 4000
_REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DIRTY_RE = re.compile(
    r"(?i)\b(dirty|uncommitted|local changes|modified|untracked|not clean)\b"
)


class StaxWorkspaceError(RuntimeError):
    """Base class for typed workspace lifecycle refusals."""

    def __init__(
        self, message: str, *, evidence_path: Path | None = None
    ) -> None:
        super().__init__(message)
        self.evidence_path = evidence_path


class WorkspaceRequestError(StaxWorkspaceError, ValueError):
    """The workspace request is malformed or points at an unusable repository."""


class WorkspaceUnsupportedPlatformError(StaxWorkspaceError):
    """Exclusive workspace locking is unavailable on this host."""


class WorkspaceBoundaryError(StaxWorkspaceError):
    """The workspace root or worktree path violates the Tactus write boundary."""


class WorkspaceContainmentError(WorkspaceBoundaryError):
    """A worktree path is not strictly inside the configured workspace root."""

    def __init__(self, path: Path, root: Path) -> None:
        self.path = Path(path)
        self.root = Path(root)
        super().__init__(
            f"workspace path {self.path} is not strictly inside {self.root}"
        )


class WorkspaceInUseError(StaxWorkspaceError):
    """Another live attempt holds the workspace lease."""

    def __init__(self, workspace_name: str, owner: str = "") -> None:
        self.workspace_name = workspace_name
        self.owner = owner
        detail = f"; holder={owner}" if owner else ""
        super().__init__(
            f"workspace {workspace_name!r} is already leased by another attempt"
            f"{detail}"
        )


class WorkspaceNotFoundError(StaxWorkspaceError):
    """No Tactus-owned worktree exists for the request."""


class WorkspaceOwnershipError(StaxWorkspaceError):
    """A worktree cannot be attributed to the exact requesting attempt."""


class WorkspaceIdentityConflictError(WorkspaceOwnershipError):
    """A persisted workspace identity differs from the current request."""

    def __init__(
        self, expected: Mapping[str, Any], actual: Mapping[str, Any]
    ) -> None:
        self.expected = dict(expected)
        self.actual = dict(actual)
        super().__init__(
            "workspace identity conflict: refusing to reuse or mutate a "
            f"workspace bound to different facts (expected={self.expected}, "
            f"actual={self.actual})"
        )


class WorkspaceSourceDriftError(StaxWorkspaceError):
    """The repository or worktree revision moved away from the bound base."""

    def __init__(self, expected: str, actual: str, path: Path) -> None:
        self.expected = expected
        self.actual = actual
        self.path = Path(path)
        super().__init__(
            f"source drift at {self.path}: expected revision {expected}, "
            f"found {actual}"
        )


class WorkspaceDirtyError(StaxWorkspaceError):
    """A workspace has local changes and is therefore preserved."""

    def __init__(self, identity: "WorkspaceIdentity", detail: str = "") -> None:
        self.identity = identity
        self.detail = detail
        suffix = f": {detail}" if detail else ""
        super().__init__(
            f"workspace {identity.branch!r} has uncommitted changes; "
            f"preserving it instead of forcing cleanup{suffix}"
        )


class WorkspaceCommandError(StaxWorkspaceError):
    """A Stax worktree command could not run or returned non-zero."""

    def __init__(
        self,
        argv: Sequence[str],
        reason: str,
        result: CommandResult | None = None,
    ) -> None:
        self.argv = tuple(argv)
        self.reason = reason
        self.result = result
        super().__init__(
            f"Stax worktree command failed ({' '.join(self.argv)}): {reason}"
        )


class WorkspaceCreateError(WorkspaceCommandError):
    """The worktree could not be created safely."""


class WorkspaceRemoveError(WorkspaceCommandError):
    """The worktree could not be removed safely."""


class WorkspacePreflightError(StaxWorkspaceError):
    """The pinned Stax binary/repository failed preflight before mutation."""

    def __init__(self, cause: StaxPreflightError) -> None:
        self.cause = cause
        super().__init__(f"Stax workspace preflight refused: {cause}")


class WorkspaceStateError(StaxWorkspaceError):
    """Stax or the ownership state disagreed in an unsafe way."""


def _repository_path(value: str | os.PathLike[str]) -> Path:
    """Return an absolute, existing repository directory or refuse."""

    try:
        path = Path(value).expanduser()
    except TypeError as exc:  # pragma: no cover - defensive
        raise WorkspaceRequestError("repository must be a path") from exc
    if not path.is_absolute():
        raise WorkspaceRequestError(
            f"repository must be an absolute path, got {os.fspath(path)!r}"
        )
    if not path.exists() or not path.is_dir():
        raise WorkspaceRequestError(f"repository is not a directory: {path}")
    return path.resolve()


def _validate_revision(revision: str) -> str:
    if not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision):
        raise WorkspaceRequestError(
            "base_revision must be an exact lowercase 40- or 64-character "
            "Git object id"
        )
    return revision


def _validate_attempt_id(attempt_id: str) -> str:
    if not isinstance(attempt_id, str):
        raise WorkspaceRequestError("attempt_id must be a string")
    if attempt_id != attempt_id.strip() or not attempt_id:
        raise WorkspaceRequestError("attempt_id must be a non-empty trimmed string")
    if len(attempt_id) > _MAX_ATTEMPT_ID:
        raise WorkspaceRequestError(
            f"attempt_id must be at most {_MAX_ATTEMPT_ID} characters"
        )
    if any(ord(char) < 32 for char in attempt_id):
        raise WorkspaceRequestError("attempt_id must not contain control characters")
    return attempt_id


@dataclass(frozen=True, slots=True)
class WorkspaceRequest:
    """One attempt's request for an isolated worktree.

    The deterministic workspace/branch name depends only on ``attempt_id``.
    ``base_revision`` must be an exact commit id, never a floating branch name.
    """

    attempt_id: str
    base_revision: str
    repository: Path

    def __post_init__(self) -> None:
        attempt_id = _validate_attempt_id(self.attempt_id)
        revision = _validate_revision(self.base_revision)
        repository = _repository_path(self.repository)
        object.__setattr__(self, "attempt_id", attempt_id)
        object.__setattr__(self, "base_revision", revision)
        object.__setattr__(self, "repository", repository)

    @property
    def workspace_name(self) -> str:
        """Deterministic Stax worktree/branch name for this attempt."""

        slug = _SLUG_RE.sub("-", self.attempt_id.lower()).strip("-")[:48]
        slug = slug or "attempt"
        digest = hashlib.sha256(self.attempt_id.encode("utf-8")).hexdigest()[:12]
        return f"{WORKSPACE_NAME_PREFIX}{slug}-{digest}"

    @property
    def branch(self) -> str:
        """The branch created in the worktree (same deterministic name)."""

        return self.workspace_name

    def as_dict(self) -> dict[str, str]:
        return {
            "attempt_id": self.attempt_id,
            "base_revision": self.base_revision,
            "repository": os.fspath(self.repository),
            "workspace_name": self.workspace_name,
            "branch": self.branch,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceIdentity:
    """Stable workspace identity: attempt, base revision, branch and path."""

    attempt_id: str
    base_revision: str
    branch: str
    path: Path
    repository: Path

    def __post_init__(self) -> None:
        attempt_id = _validate_attempt_id(self.attempt_id)
        revision = _validate_revision(self.base_revision)
        if not isinstance(self.branch, str) or not self.branch.strip():
            raise WorkspaceRequestError("workspace branch must be a non-empty string")
        path = Path(self.path).expanduser()
        if not path.is_absolute():
            raise WorkspaceRequestError("workspace path must be absolute")
        repository = _repository_path(self.repository)
        object.__setattr__(self, "attempt_id", attempt_id)
        object.__setattr__(self, "base_revision", revision)
        object.__setattr__(self, "branch", self.branch)
        object.__setattr__(self, "path", path.resolve())
        object.__setattr__(self, "repository", repository)

    def _canonical(self) -> dict[str, str]:
        return {
            "attempt_id": self.attempt_id,
            "base_revision": self.base_revision,
            "branch": self.branch,
            "path": os.fspath(self.path),
            "repository": os.fspath(self.repository),
        }

    @property
    def fingerprint(self) -> str:
        """Stable digest of every bound identity field."""

        payload = json.dumps(
            self._canonical(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def as_dict(self) -> dict[str, str]:
        return self._canonical()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkspaceIdentity":
        if not isinstance(data, Mapping):
            raise ValueError("workspace identity must be a mapping")
        return cls(
            attempt_id=data["attempt_id"],
            base_revision=data["base_revision"],
            branch=data["branch"],
            path=Path(data["path"]),
            repository=Path(data["repository"]),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    """One live Stax worktree and its optional Tactus ownership identity."""

    name: str
    branch: str
    path: Path
    dirty: bool
    identity: WorkspaceIdentity | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "branch": self.branch,
            "path": os.fspath(self.path),
            "dirty": self.dirty,
            "owned": self.identity is not None,
            "identity": self.identity.as_dict() if self.identity else None,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceRemoval:
    """Evidence returned after a clean, non-forced removal."""

    identity: WorkspaceIdentity
    remaining_path_exists: bool = False


class _WorkspaceLock:
    """Non-blocking exclusive lock held for the lifetime of a lease."""

    def __init__(self, path: Path, fd: int, owner: Mapping[str, Any]) -> None:
        self.path = path
        self._fd: int | None = fd
        self.owner = dict(owner)

    @classmethod
    def acquire(
        cls,
        path: Path,
        owner: Mapping[str, Any],
        *,
        workspace_name: str | None = None,
    ) -> "_WorkspaceLock":
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise WorkspaceStateError(
                f"cannot open workspace lease {path}: {exc}"
            ) from exc

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno_EACCES, errno_EAGAIN):
                os.close(fd)
                raise WorkspaceStateError(
                    f"cannot lock workspace lease {path}: {exc}"
                ) from exc
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                holder = os.read(fd, 8192).decode("utf-8", "replace").strip()
            except OSError:
                holder = ""
            finally:
                os.close(fd)
            raise WorkspaceInUseError(workspace_name or path.stem, holder) from exc
        except BaseException:
            os.close(fd)
            raise

        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(
                fd,
                json.dumps(dict(owner), sort_keys=True).encode("utf-8"),
            )
            os.fsync(fd)
        except OSError as exc:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise WorkspaceStateError(
                f"cannot record workspace lease owner in {path}: {exc}"
            ) from exc
        return cls(path, fd, owner)

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@dataclass(slots=True)
class WorkspaceLease:
    """An exclusively held, identity-bound workspace.

    The caller must use this as a context manager or call :meth:`close` when
    the attempt stops using the worktree. The lock is advisory between Tactus
    attempts, not a security sandbox.
    """

    identity: WorkspaceIdentity
    reused: bool
    _lock: _WorkspaceLock = field(repr=False)
    _closed: bool = field(default=False, repr=False)

    @property
    def root(self) -> Path:
        return self.identity.path

    @property
    def path(self) -> Path:
        return self.identity.path

    @property
    def branch(self) -> str:
        return self.identity.branch

    def close(self) -> None:
        if not self._closed:
            self._lock.release()
            self._closed = True

    def __enter__(self) -> "WorkspaceLease":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# ``errno`` constants are imported with the standard imports above so the
# lock failure path is explicit and does not use a broad catch.


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _git_head(path: Path) -> str:
    """Read the current HEAD through a bounded, read-only Git probe.

    This is identity evidence for the workspace boundary, never a fallback for
    a Stax operation: the worktree is still created, listed, addressed and
    removed exclusively through the pinned Stax CLI.
    """

    argv = ("git", "-C", os.fspath(path), "rev-parse", "--verify", "HEAD^{commit}")
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"cannot run {' '.join(argv)}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            f"git rev-parse failed with exit {completed.returncode}: {detail}"
        )
    value = completed.stdout.strip()
    if not value:
        raise RuntimeError("git rev-parse returned an empty revision")
    return value


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _storage_key(repository: Path, name: str) -> str:
    payload = f"{os.fspath(Path(repository).resolve())}\0{name}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


def _truncate(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


class StaxWorkspacePort:
    """Pinned-Stax workspace lifecycle for deterministic attempt isolation.

    ``workspace_root`` is the Tactus-owned directory that contains every
    worktree and all lifecycle state (``<root>/.tactus``). It must be outside the
    source repository. Operators therefore configure Stax's
    ``[worktree] root_dir`` to the same root and pass it here.
    """

    def __init__(
        self,
        executable: str | os.PathLike[str],
        *,
        workspace_root: str | os.PathLike[str],
        preflight: StaxPreflight | None = None,
        runner: CommandRunner | None = None,
        pin: StaxPin = PINNED_STAX,
        platform: str | None = None,
        digest: Callable[[Path], str] = sha256_file,
        revision_probe: Callable[[Path], str] | None = None,
        command_timeout: float = 120.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        executable_path = Path(executable).expanduser()
        if not executable_path.is_absolute():
            raise ValueError("Stax executable must be an absolute path")
        root = Path(workspace_root).expanduser()
        if not root.is_absolute():
            raise ValueError("workspace_root must be an absolute path")

        self._executable = os.fspath(executable_path)
        self._workspace_root = root.resolve()
        self._runner: CommandRunner = (
            runner
            if runner is not None
            else SubprocessCommandRunner(timeout=command_timeout)
        )
        self._preflight = (
            preflight
            if preflight is not None
            else StaxPreflight(
                pin,
                runner=self._runner,
                platform=platform,
                digest=digest,
                timeout=command_timeout,
            )
        )
        self._revision_probe = revision_probe or _git_head
        self._clock = clock or _utcnow

    # -- public surface ---------------------------------------------------

    def create(self, request: WorkspaceRequest) -> WorkspaceLease:
        """Provision or reuse the single worktree bound to *request*.

        The returned lease holds the exclusive workspace lock until
        :meth:`WorkspaceLease.close` or context-manager exit.
        """

        if not isinstance(request, WorkspaceRequest):
            raise TypeError("create() requires a WorkspaceRequest")
        try:
            return self._create(request)
        except StaxWorkspaceError as exc:
            self._attach_refusal("create", exc, request=request)
            raise

    def path(self, request: WorkspaceRequest) -> Path:
        """Return the existing worktree path if it still matches its identity."""

        if not isinstance(request, WorkspaceRequest):
            raise TypeError("path() requires a WorkspaceRequest")
        try:
            return self._path(request)
        except StaxWorkspaceError as exc:
            self._attach_refusal("path", exc, request=request)
            raise

    def list(self, repository: str | os.PathLike[str]) -> tuple[WorkspaceRecord, ...]:
        """List live Tactus-owned worktrees for *repository* without mutation."""

        try:
            return self._list(repository)
        except StaxWorkspaceError as exc:
            self._attach_refusal("list", exc, repository=repository)
            raise

    def remove(self, request: WorkspaceRequest) -> WorkspaceRemoval:
        """Remove a clean, exactly-owned worktree without force or publication."""

        if not isinstance(request, WorkspaceRequest):
            raise TypeError("remove() requires a WorkspaceRequest")
        try:
            return self._remove(request)
        except StaxWorkspaceError as exc:
            self._attach_refusal("remove", exc, request=request)
            raise

    # -- create -----------------------------------------------------------

    def _create(self, request: WorkspaceRequest) -> WorkspaceLease:
        _, repository = self._assert_layout(request.repository)
        self._check_binary(repository, require_repository=True)

        key = _storage_key(repository, request.workspace_name)
        lock = _WorkspaceLock.acquire(
            self._state_path("locks", f"{key}.lock", create=True),
            owner={
                "attempt_id": request.attempt_id,
                "branch": request.branch,
                "repository": os.fspath(repository),
                "pid": os.getpid(),
                "created_at": self._clock().isoformat(),
            },
            workspace_name=request.workspace_name,
        )
        try:
            manifest_path = self._state_path(
                "workspaces", f"{key}.json", create=True
            )
            identity = self._load_manifest(manifest_path)
            records = self._list_records(repository)
            record = self._record_for(records, request.workspace_name)

            if identity is not None:
                self._require_manifest_matches(identity, request)
                if record is None:
                    raise WorkspaceStateError(
                        "ownership manifest exists but Stax no longer lists the "
                        f"worktree {request.workspace_name!r}; refusing to "
                        "silently recreate it"
                    )
                self._verify_record(identity, record, repository)
                self._require_revision(repository, request.base_revision)
                self._require_revision(identity.path, request.base_revision)
                return WorkspaceLease(identity=identity, reused=True, _lock=lock)

            if record is not None:
                raise WorkspaceOwnershipError(
                    f"worktree {request.workspace_name!r} already exists without "
                    "a Tactus ownership manifest; refusing to adopt it"
                )

            # Refuse to even start Stax when its expected location would escape
            # the workspace root; the authoritative Stax-reported path is still
            # re-checked after creation.
            self._contain_path(
                self._workspace_root / request.workspace_name, repository
            )

            # Source drift is checked before the one mutating command.
            self._require_revision(repository, request.base_revision)
            self._create_worktree(request, repository)
            path = self._path_for_name(repository, request.workspace_name)
            identity = WorkspaceIdentity(
                attempt_id=request.attempt_id,
                base_revision=request.base_revision,
                branch=request.workspace_name,
                path=path,
                repository=repository,
            )
            self._require_revision(path, request.base_revision)

            records = self._list_records(repository)
            record = self._record_for(records, request.workspace_name)
            if record is None:
                raise WorkspaceStateError(
                    "Stax created the worktree but did not list it; refusing to "
                    "record an unverified workspace"
                )
            self._verify_record(identity, record, repository)
            self._write_manifest(manifest_path, identity)
            return WorkspaceLease(identity=identity, reused=False, _lock=lock)
        except BaseException:
            lock.release()
            raise

    def _create_worktree(
        self, request: WorkspaceRequest, repository: Path
    ) -> CommandResult:
        argv = (
            self._executable,
            "worktree",
            "create",
            request.workspace_name,
            "--from",
            request.base_revision,
            "--no-verify",
        )
        result = self._run_worktree(argv[2:], cwd=repository)
        if result.returncode != 0:
            raise WorkspaceCreateError(
                argv, self._detail(result), result
            )
        return result

    # -- path/list --------------------------------------------------------

    def _path(self, request: WorkspaceRequest) -> Path:
        _, repository = self._assert_layout(request.repository)
        self._check_binary(repository, require_repository=False)

        key = _storage_key(repository, request.workspace_name)
        manifest_path = self._state_path(
            "workspaces", f"{key}.json", create=False
        )
        identity = self._load_manifest(manifest_path)
        if identity is None:
            raise WorkspaceNotFoundError(
                f"no Tactus-owned workspace for attempt {request.attempt_id!r}"
            )
        self._require_manifest_matches(identity, request)

        records = self._list_records(repository)
        record = self._record_for(records, request.workspace_name)
        if record is None:
            raise WorkspaceNotFoundError(
                f"Stax does not list worktree {request.workspace_name!r}"
            )
        self._verify_record(identity, record, repository)
        self._require_revision(identity.path, request.base_revision)
        return identity.path

    def _list(
        self, repository_value: str | os.PathLike[str]
    ) -> tuple[WorkspaceRecord, ...]:
        repository = _repository_path(repository_value)
        _, repository = self._assert_layout(repository)
        self._check_binary(repository, require_repository=False)

        result: list[WorkspaceRecord] = []
        for record in self._list_records(repository):
            if not record.name.startswith(WORKSPACE_NAME_PREFIX):
                continue
            path = self._contain_path(record.path, repository)
            key = _storage_key(repository, record.name)
            manifest_path = self._state_path(
                "workspaces", f"{key}.json", create=False
            )
            identity = self._load_manifest(manifest_path)
            if identity is not None:
                if (
                    identity.repository != repository
                    or identity.branch != record.name
                    or identity.branch != record.branch
                    or identity.path != path
                ):
                    raise WorkspaceStateError(
                        "ownership manifest disagrees with the live worktree for "
                        f"{record.name!r}; refusing to report it as owned"
                    )
            result.append(
                WorkspaceRecord(
                    name=record.name,
                    branch=record.branch,
                    path=path,
                    dirty=record.dirty,
                    identity=identity,
                )
            )
        return tuple(sorted(result, key=lambda item: item.name))

    def _list_records(self, repository: Path) -> tuple[WorkspaceRecord, ...]:
        argv = (self._executable, "worktree", "list", "--json")
        result = self._run_worktree(argv[2:], cwd=repository)
        if result.returncode != 0:
            raise WorkspaceCommandError(argv, self._detail(result), result)
        return self._parse_worktree_list(result.stdout, repository)

    def _parse_worktree_list(
        self, output: str, repository: Path
    ) -> tuple[WorkspaceRecord, ...]:
        try:
            payload = json.loads(_strip_ansi(output))
        except (TypeError, ValueError) as exc:
            raise WorkspaceStateError(
                f"Stax worktree list --json returned invalid JSON: {exc}"
            ) from exc
        if not isinstance(payload, list):
            raise WorkspaceStateError(
                "Stax worktree list --json must return a JSON array"
            )

        records: list[WorkspaceRecord] = []
        seen: set[str] = set()
        for item in payload:
            if not isinstance(item, Mapping):
                raise WorkspaceStateError(
                    "Stax worktree list --json contained a non-object entry"
                )
            name = item.get("name")
            path_value = item.get("path")
            branch = item.get("branch")
            dirty = item.get("dirty")
            if not isinstance(name, str) or not name:
                raise WorkspaceStateError(
                    "Stax worktree list --json entry has no worktree name"
                )
            if name in seen:
                raise WorkspaceStateError(
                    f"Stax worktree list --json repeated worktree {name!r}"
                )
            if not isinstance(path_value, str) or not path_value:
                raise WorkspaceStateError(
                    f"Stax worktree list --json entry {name!r} has no path"
                )
            candidate = Path(path_value)
            if not candidate.is_absolute():
                raise WorkspaceStateError(
                    f"Stax worktree path for {name!r} is not absolute"
                )
            if branch is not None and not isinstance(branch, str):
                raise WorkspaceStateError(
                    f"Stax worktree branch for {name!r} is malformed"
                )
            if not isinstance(dirty, bool):
                raise WorkspaceStateError(
                    f"Stax worktree dirty flag for {name!r} is missing or "
                    "malformed; refusing to infer a clean workspace"
                )
            seen.add(name)
            records.append(
                WorkspaceRecord(
                    name=name,
                    branch=branch or "",
                    path=candidate.resolve(),
                    dirty=dirty,
                )
            )
        del repository  # parsed paths are validated by the caller
        return tuple(records)

    def _path_for_name(self, repository: Path, name: str) -> Path:
        argv = (self._executable, "worktree", "path", name)
        result = self._run_worktree(argv[2:], cwd=repository)
        if result.returncode != 0:
            raise WorkspaceCommandError(argv, self._detail(result), result)
        lines = [
            line.strip()
            for line in _strip_ansi(result.stdout)
            .replace("\r\n", "\n")
            .splitlines()
            if line.strip()
        ]
        if len(lines) != 1:
            raise WorkspaceStateError(
                "Stax worktree path must print exactly one absolute path; got "
                f"{result.stdout!r}"
            )
        candidate = Path(lines[0])
        if not candidate.is_absolute():
            raise WorkspaceStateError(
                f"Stax worktree path is not absolute: {candidate}"
            )
        path = self._contain_path(candidate, repository)
        if not path.is_dir():
            raise WorkspaceStateError(
                f"Stax reported a worktree path that is not a directory: {path}"
            )
        return path

    # -- remove -----------------------------------------------------------

    def _remove(self, request: WorkspaceRequest) -> WorkspaceRemoval:
        _, repository = self._assert_layout(request.repository)
        self._check_binary(repository, require_repository=True)

        key = _storage_key(repository, request.workspace_name)
        lock = _WorkspaceLock.acquire(
            self._state_path("locks", f"{key}.lock", create=True),
            owner={
                "attempt_id": request.attempt_id,
                "branch": request.branch,
                "repository": os.fspath(repository),
                "pid": os.getpid(),
                "operation": "remove",
                "created_at": self._clock().isoformat(),
            },
        )
        try:
            manifest_path = self._state_path(
                "workspaces", f"{key}.json", create=False
            )
            identity = self._load_manifest(manifest_path)
            if identity is None:
                raise WorkspaceOwnershipError(
                    f"no Tactus ownership manifest for {request.workspace_name!r}; "
                    "refusing to remove an unowned worktree"
                )
            self._require_manifest_matches(identity, request)

            records = self._list_records(repository)
            record = self._record_for(records, request.workspace_name)
            if record is None:
                raise WorkspaceNotFoundError(
                    f"Stax does not list worktree {request.workspace_name!r}; "
                    "preserving the stale ownership manifest"
                )
            self._verify_record(identity, record, repository)
            if record.dirty:
                raise WorkspaceDirtyError(
                    identity, "stax worktree list reports dirty=true"
                )
            self._require_revision(repository, request.base_revision)

            argv = (self._executable, "worktree", "remove", identity.branch)
            result = self._run_worktree(argv[2:], cwd=repository)
            if result.returncode != 0:
                detail = self._detail(result)
                if _DIRTY_RE.search(detail):
                    raise WorkspaceDirtyError(identity, detail)
                raise WorkspaceRemoveError(argv, detail, result)

            if identity.path.exists():
                raise WorkspaceStateError(
                    "Stax reported a successful removal but the workspace path "
                    f"still exists ({identity.path}); preserving the ownership "
                    "manifest and refusing to delete anything else"
                )
            try:
                manifest_path.unlink()
            except FileNotFoundError as exc:
                raise WorkspaceStateError(
                    "ownership manifest disappeared during removal; workspace "
                    "was not silently re-adopted"
                ) from exc
            except OSError as exc:
                raise WorkspaceStateError(
                    f"workspace was removed but its ownership manifest could not "
                    f"be cleared: {exc}"
                ) from exc
            return WorkspaceRemoval(
                identity=identity, remaining_path_exists=False
            )
        finally:
            lock.release()

    # -- boundary and state ----------------------------------------------

    def _assert_layout(self, repository: Path) -> tuple[Path, Path]:
        root = self._workspace_root
        if root == Path(root.anchor):
            raise WorkspaceBoundaryError(
                "workspace_root must not be a filesystem root"
            )
        if not root.exists() or not root.is_dir():
            raise WorkspaceBoundaryError(
                f"workspace_root is not an existing directory: {root}"
            )
        repository = Path(repository).resolve()
        if root == repository or repository in root.parents:
            raise WorkspaceBoundaryError(
                f"workspace_root {root} is inside the source repository "
                f"{repository}; refusing to write beside the canonical checkout"
            )
        return root, repository

    def _contain_path(self, path: Path, repository: Path) -> Path:
        root = self._workspace_root
        resolved = Path(path).resolve()
        if resolved == root or root not in resolved.parents:
            raise WorkspaceContainmentError(resolved, root)
        repository = Path(repository).resolve()
        if resolved == repository or repository in resolved.parents:
            raise WorkspaceBoundaryError(
                f"workspace path {resolved} is inside the source repository "
                f"{repository}"
            )
        return resolved

    def _state_dir(self, kind: str, *, create: bool) -> Path:
        root = self._workspace_root
        if not root.exists() or not root.is_dir():
            raise WorkspaceBoundaryError(
                f"workspace_root is not an existing directory: {root}"
            )
        base = root / WORKSPACE_STATE_DIR / kind
        resolved = base.resolve()
        if root not in resolved.parents:
            raise WorkspaceBoundaryError(
                f"Tactus state directory {resolved} escapes workspace_root {root}"
            )
        if create:
            try:
                base.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError as exc:
                raise WorkspaceStateError(
                    f"cannot create Tactus state directory {base}: {exc}"
                ) from exc
            resolved = base.resolve()
            if root not in resolved.parents:
                raise WorkspaceBoundaryError(
                    f"Tactus state directory {resolved} escapes workspace_root "
                    f"{root}"
                )
        return resolved

    def _state_path(self, kind: str, name: str, *, create: bool) -> Path:
        state_dir = self._state_dir(kind, create=create)
        candidate = state_dir / name
        resolved = candidate.resolve()
        if state_dir not in resolved.parents:
            raise WorkspaceBoundaryError(
                f"Tactus state path {resolved} escapes {state_dir}"
            )
        return resolved

    def _write_manifest(
        self, manifest_path: Path, identity: WorkspaceIdentity
    ) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "identity": identity.as_dict(),
        }
        temporary = manifest_path.with_name(
            f".{manifest_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(payload, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, manifest_path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise WorkspaceStateError(
                f"cannot persist workspace ownership manifest {manifest_path}: "
                f"{exc}"
            ) from exc

    def _load_manifest(self, manifest_path: Path) -> WorkspaceIdentity | None:
        if not manifest_path.exists():
            return None
        if not manifest_path.is_file():
            raise WorkspaceStateError(
                f"workspace ownership manifest is not a file: {manifest_path}"
            )
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise WorkspaceStateError(
                f"cannot read workspace ownership manifest {manifest_path}: {exc}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise WorkspaceStateError(
                f"workspace ownership manifest {manifest_path} is not an object"
            )
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise WorkspaceStateError(
                f"workspace ownership manifest {manifest_path} has an "
                "unsupported schema_version"
            )
        try:
            return WorkspaceIdentity.from_dict(payload["identity"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkspaceStateError(
                f"workspace ownership manifest {manifest_path} is malformed: {exc}"
            ) from exc

    def _require_manifest_matches(
        self, identity: WorkspaceIdentity, request: WorkspaceRequest
    ) -> None:
        expected = {
            "attempt_id": request.attempt_id,
            "base_revision": request.base_revision,
            "branch": request.branch,
            "repository": os.fspath(request.repository),
        }
        actual = {
            "attempt_id": identity.attempt_id,
            "base_revision": identity.base_revision,
            "branch": identity.branch,
            "repository": os.fspath(identity.repository),
        }
        if expected != actual:
            raise WorkspaceIdentityConflictError(expected, actual)

    def _verify_record(
        self,
        identity: WorkspaceIdentity,
        record: WorkspaceRecord,
        repository: Path,
    ) -> None:
        expected = {
            "workspace_name": identity.branch,
            "branch": identity.branch,
            "path": os.fspath(identity.path),
        }
        actual = {
            "workspace_name": record.name,
            "branch": record.branch,
            "path": os.fspath(self._contain_path(record.path, repository)),
        }
        if expected != actual:
            raise WorkspaceIdentityConflictError(expected, actual)
        if not identity.path.is_dir():
            raise WorkspaceStateError(
                f"workspace {identity.branch!r} is listed but its directory is "
                f"missing: {identity.path}"
            )

    @staticmethod
    def _record_for(
        records: Sequence[WorkspaceRecord], name: str
    ) -> WorkspaceRecord | None:
        for record in records:
            if record.name == name:
                return record
        return None

    # -- pinned Stax invocation ------------------------------------------

    def _check_binary(
        self, repository: Path, *, require_repository: bool
    ) -> None:
        try:
            if require_repository:
                self._preflight.check(self._executable, repository=repository)
            else:
                self._preflight.check(self._executable)
        except StaxPreflightError as exc:
            raise WorkspacePreflightError(exc) from exc

    def _run_worktree(
        self, args: Sequence[str], *, cwd: Path
    ) -> CommandResult:
        argv = (self._executable, "worktree", *args)
        try:
            return self._runner(argv, cwd=Path(cwd).resolve())
        except StaxPreflightError as exc:
            raise WorkspaceCommandError(argv, str(exc)) from exc
        except OSError as exc:
            raise WorkspaceCommandError(argv, str(exc)) from exc

    @staticmethod
    def _detail(result: CommandResult) -> str:
        detail = result.stderr.strip() or result.stdout.strip()
        reason = f"exit {result.returncode}"
        return f"{reason}: {detail}" if detail else reason

    def _require_revision(self, path: Path, expected: str) -> str:
        try:
            actual = self._revision_probe(Path(path))
        except Exception as exc:
            raise WorkspaceStateError(
                f"cannot verify the source revision at {path}: {exc}"
            ) from exc
        if not isinstance(actual, str) or not actual.strip():
            raise WorkspaceStateError(
                f"revision probe returned no revision for {path}"
            )
        actual = actual.strip().lower()
        if actual != expected:
            raise WorkspaceSourceDriftError(expected, actual, Path(path))
        return actual

    # -- refusal evidence -------------------------------------------------

    def _attach_refusal(
        self,
        operation: str,
        exc: StaxWorkspaceError,
        *,
        request: WorkspaceRequest | None = None,
        repository: str | os.PathLike[str] | None = None,
    ) -> None:
        if exc.evidence_path is not None:
            return
        if not self._refusal_location_allowed(request, repository):
            return
        try:
            exc.evidence_path = self._record_refusal(
                operation, exc, request=request, repository=repository
            )
        except Exception:
            # Evidence collection must never mask the primary refusal.
            exc.evidence_path = None

    def _refusal_location_allowed(
        self,
        request: WorkspaceRequest | None,
        repository: str | os.PathLike[str] | None,
    ) -> bool:
        """Never create refusal evidence inside the source checkout."""

        try:
            if request is not None:
                repo = request.repository
            elif repository is not None:
                repo = Path(repository).expanduser().resolve()
            else:
                return True
        except (OSError, TypeError, ValueError):
            return True
        root = self._workspace_root
        return not (root == repo or repo in root.parents)

    def _record_refusal(
        self,
        operation: str,
        exc: StaxWorkspaceError,
        *,
        request: WorkspaceRequest | None,
        repository: str | os.PathLike[str] | None,
    ) -> Path | None:
        try:
            refusal_dir = self._state_dir("refusals", create=True)
        except Exception:
            return None

        identity = getattr(exc, "identity", None)
        result = getattr(exc, "result", None)
        repository_value: str | None = None
        if request is not None:
            repository_value = os.fspath(request.repository)
        elif repository is not None:
            repository_value = os.fspath(repository)

        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "adapter": "tactus.integrations.stax.workspace",
            "operation": operation,
            "refusal": type(exc).__name__,
            "message": str(exc),
            "timestamp": self._clock().astimezone(timezone.utc).isoformat(),
            "request": request.as_dict() if request is not None else None,
            "identity": (
                identity.as_dict()
                if isinstance(identity, WorkspaceIdentity)
                else None
            ),
            "workspace_preserved": (
                Path(identity.path).exists()
                if isinstance(identity, WorkspaceIdentity)
                else None
            ),
            "repository": repository_value,
            "workspace_root": os.fspath(self._workspace_root),
            "argv": list(getattr(exc, "argv", ()) or ()),
            "cause": (
                type(getattr(exc, "cause", None)).__name__
                if getattr(exc, "cause", None) is not None
                else None
            ),
        }
        if isinstance(result, CommandResult):
            payload["command_result"] = {
                "returncode": result.returncode,
                "stdout": _truncate(result.stdout),
                "stderr": _truncate(result.stderr),
            }

        timestamp = self._clock().astimezone(timezone.utc).strftime(
            "%Y%m%dT%H%M%S.%fZ"
        )
        filename = (
            f"{timestamp}-{type(exc).__name__}-{uuid.uuid4().hex[:8]}.json"
        )
        target = refusal_dir / filename
        temporary = refusal_dir / f".{filename}.tmp"
        try:
            temporary.write_text(
                json.dumps(payload, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, target)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        return target
