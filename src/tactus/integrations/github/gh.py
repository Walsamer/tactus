"""Live GitHub publication transport over ``git push`` and ``gh api``.

This is the one implementation of
:class:`tactus.integrations.github.transport.GitHubTransport` that talks to a
real remote. It exists only to be handed to
``tactus.publication.publisher.GitHubLivePublicationPort``, which is a separate,
explicitly reviewed class; the mocked port refuses a live transport outright.

Design facts worth knowing before changing it:

* **No shell.** Every command is an argv list. The credential never appears in
  an argv (it would be visible in ``ps``); it is put in the environment of one
  subprocess — ``GH_TOKEN`` for ``gh``, and the same variable consumed by a
  ``credential.helper`` for ``git`` that also resets any ambient helper.
* **Never ``--force``.** A rejected non-fast-forward push is the boundary
  working: it surfaces as
  :class:`~tactus.integrations.github.transport.GitHubNonFastForwardPushError`
  and the verified revision stays where it was.
* **One review branch, one authorized base.** ``ensure_branch`` refuses the
  trunk and the authorized base; ``create_pull_request`` refuses any base other
  than the authorized one, and any base revision other than the authorized one.
* **The port owns idempotency.** This transport does not decide whether a
  publication already happened: it reports what GitHub says, and the port
  reconciles by the ``publication_id`` marker in the pull-request body.
* **No credential store.** It holds one bound handle for one attempt and never
  writes it anywhere.

Runtime dependencies: none beyond the standard library and the operator's own
``git`` and ``gh`` binaries, which must already be installed and new enough to
support ``gh api``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .transport import (
    GitHubAcknowledgmentLostError,
    GitHubBaseMovedError,
    GitHubBranchConflictError,
    GitHubDuplicatePullRequestError,
    GitHubNonFastForwardPushError,
    GitHubPermissionDeniedError,
    GitHubProtectedDestinationError,
    GitHubPullRequest,
    GitHubTransportContract,
    GitHubTransportError,
    RemoteOperation,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tactus.publication.guard import PublicationCredential

__all__ = [
    "GH_CONTRACT_ID",
    "GH_VERSION",
    "PROTECTED_BRANCHES",
    "GhCommandError",
    "GhCommandResult",
    "GhCommandRunner",
    "GhGitHubTransport",
    "SubprocessGhCommandRunner",
]

GH_CONTRACT_ID = "tactus.integrations.github.gh/v1"
GH_VERSION = "1.0.0"

#: Branches a publication transport never writes and never targets as a head.
PROTECTED_BRANCHES: frozenset[str] = frozenset({"main", "master"})

#: The marker the port reconciles a lost acknowledgment with.
_MARKER_RE = re.compile(r"Tactus publication marker:\s*(pub-[0-9a-f]{32})")

_REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

#: A credential helper for one `git push` that reads the token from the child
#: environment. It resets any ambient helper first (see ``_push_argv``), so an
#: operator's keychain or credential-manager entry cannot be used instead of the
#: narrow publication token.
_CREDENTIAL_HELPER = (
    '!f() { test "$1" = get && echo username=x-access-token && '
    'echo "password=${GH_TOKEN}"; }; f'
)

_NON_FAST_FORWARD_MARKERS = (
    "non-fast-forward",
    "fetch first",
    "[rejected]",
    "failed to push some refs",
)

_PERMISSION_MARKERS = (
    "permission to",
    "permission denied",
    "403",
    "must have push access",
    "protected branch",
)


@dataclass(frozen=True, slots=True)
class GhCommandResult:
    """Captured result of one command invocation."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class GhCommandError(RuntimeError):
    """A command could not be run at all (missing binary, timeout)."""


class GhCommandRunner(Protocol):
    """Minimal command-execution seam so tests never run ``git`` or ``gh``."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> GhCommandResult: ...


class SubprocessGhCommandRunner:
    """Default runner: no shell, bounded timeout, captured output."""

    def __init__(self, *, timeout: float = 60.0) -> None:
        self.timeout = timeout

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> GhCommandResult:
        try:
            completed = subprocess.run(
                list(argv),
                cwd=cwd,
                env=dict(env) if env is not None else None,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GhCommandError(f"cannot run {argv[0]!r}: {type(exc).__name__}") from exc
        return GhCommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def _require_text(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


class GhGitHubTransport:
    """One live GitHub transport, bound to one repository and one base."""

    def __init__(
        self,
        *,
        source_path: str | os.PathLike[str],
        authorized_base_branch: str,
        authorized_base_revision: str,
        remote: str = "origin",
        runner: GhCommandRunner | None = None,
        protected_branches: Iterable[str] = PROTECTED_BRANCHES,
    ) -> None:
        path = Path(os.fspath(source_path)).expanduser()
        if not path.is_absolute():
            raise GitHubTransportError("source_path must be an absolute path")
        if not path.is_dir():
            raise GitHubTransportError(f"source_path is not a directory: {path}")
        base_revision = _require_text(authorized_base_revision, "authorized_base_revision")
        if not _REVISION_RE.fullmatch(base_revision):
            raise GitHubTransportError(
                "authorized_base_revision must be an exact lowercase 40- or "
                "64-character Git object id"
            )
        self._source_path = path
        self._authorized_base_branch = _require_text(
            authorized_base_branch, "authorized_base_branch"
        )
        self._authorized_base_revision = base_revision
        self._remote = _require_text(remote, "remote")
        self._runner: GhCommandRunner = (
            runner if runner is not None else SubprocessGhCommandRunner()
        )
        self._protected = frozenset(
            {_require_text(branch, "protected branch").casefold() for branch in protected_branches}
        )
        self._operations: list[RemoteOperation] = []
        self._calls = 0
        self._credential: PublicationCredential | None = None
        self._contract = GitHubTransportContract(
            contract_id=GH_CONTRACT_ID,
            version=GH_VERSION,
            transport="gh",
            live=True,
            network=True,
            reads_require_credential=True,
        )

    # -- contract / evidence ---------------------------------------------

    @property
    def contract(self) -> GitHubTransportContract:
        return self._contract

    @property
    def network_calls(self) -> int:
        """Every ``gh``/``git`` invocation this transport made."""

        return self._calls

    @property
    def operations(self) -> tuple[RemoteOperation, ...]:
        return tuple(self._operations)

    @property
    def authorized_base_branch(self) -> str:
        return self._authorized_base_branch

    @property
    def authorized_base_revision(self) -> str:
        return self._authorized_base_revision

    def bind_credential(self, credential: "PublicationCredential") -> None:
        """Receive the one opaque handle resolved for this attempt."""

        self._credential = credential

    # -- command plumbing -------------------------------------------------

    def _material(self) -> str:
        if self._credential is None:
            raise GitHubTransportError(
                "no publication credential is bound to this transport"
            )
        return self._credential.reveal()

    def _child_env(self) -> dict[str, str]:
        """Ambient environment plus the narrow token, for one subprocess.

        ``GH_TOKEN`` makes ``gh`` use this token instead of an operator's stored
        authentication, and the credential helper reads it for ``git push``.
        It is never placed in an argv.
        """

        env = dict(os.environ)
        env["GH_TOKEN"] = self._material()
        env["GH_PROMPT_DISABLED"] = "1"
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def _run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        authenticated: bool = True,
    ) -> GhCommandResult:
        self._calls += 1
        env = self._child_env() if authenticated else None
        try:
            return self._runner(list(argv), cwd=cwd, env=env)
        except GhCommandError as exc:
            raise GitHubTransportError(str(exc)) from None

    def _api(self, path: str, *, fields: Sequence[str] = ()) -> GhCommandResult:
        argv: list[str] = ["gh", "api", path]
        if fields:
            argv.append("--method")
            argv.append("POST")
            for field in fields:
                argv.extend(["-f", field])
        return self._run(argv)

    @staticmethod
    def _json(result: GhCommandResult, what: str) -> object:
        try:
            return json.loads(result.stdout)
        except (TypeError, ValueError):
            raise GitHubTransportError(
                f"GitHub returned an unreadable {what} response"
            ) from None

    @staticmethod
    def _is_not_found(result: GhCommandResult) -> bool:
        text = f"{result.stderr}\n{result.stdout}".lower()
        return result.returncode != 0 and ("404" in text or "not found" in text)

    # -- reads ------------------------------------------------------------

    def branch_revision(self, repository: str, branch: str) -> str | None:
        repository = _require_text(repository, "repository")
        branch = _require_text(branch, "branch")
        result = self._api(f"repos/{repository}/git/ref/heads/{branch}")
        if result.returncode != 0:
            if self._is_not_found(result):
                return None
            raise GitHubTransportError(
                f"cannot read GitHub ref {branch!r}: {_last_line(result.stderr)}"
            )
        payload = self._json(result, "ref")
        sha = (
            payload.get("object", {}).get("sha")
            if isinstance(payload, Mapping)
            else None
        )
        if not isinstance(sha, str) or not sha.strip():
            raise GitHubTransportError(
                f"GitHub ref {branch!r} response carried no revision"
            )
        return sha.strip()

    def base_revision(self, repository: str, branch: str) -> str | None:
        return self.branch_revision(repository, branch)

    def find_pull_requests(
        self,
        repository: str,
        *,
        head_branch: str,
        base_branch: str,
    ) -> tuple[GitHubPullRequest, ...]:
        repository = _require_text(repository, "repository")
        head_branch = _require_text(head_branch, "head_branch")
        base_branch = _require_text(base_branch, "base_branch")
        owner = repository.split("/", 1)[0]
        query = (
            f"repos/{repository}/pulls?state=all&per_page=100"
            f"&head={owner}:{head_branch}&base={base_branch}"
        )
        result = self._api(query)
        if result.returncode != 0:
            raise GitHubTransportError(
                f"cannot list GitHub pull requests: {_last_line(result.stderr)}"
            )
        payload = self._json(result, "pull-request list")
        if not isinstance(payload, list):
            raise GitHubTransportError(
                "GitHub returned a non-list pull-request response"
            )
        return tuple(
            self._pull_request(repository, item)
            for item in payload
            if isinstance(item, Mapping)
        )

    def _pull_request(
        self, repository: str, item: Mapping[str, object]
    ) -> GitHubPullRequest:
        def text(*keys: str) -> str:
            cursor: object = item
            for key in keys:
                cursor = cursor.get(key) if isinstance(cursor, Mapping) else None
            return cursor.strip() if isinstance(cursor, str) else ""

        number = item.get("number")
        url = text("html_url")
        body = text("body")
        if not isinstance(number, int) or isinstance(number, bool) or not url:
            raise GitHubTransportError("GitHub returned an unusable pull request")
        marker = _MARKER_RE.search(body)
        return GitHubPullRequest(
            number=number,
            url=url,
            repository=repository,
            head_branch=text("head", "ref"),
            base_branch=text("base", "ref"),
            head_revision=text("head", "sha"),
            base_revision=text("base", "sha"),
            publication_id=marker.group(1) if marker is not None else "",
            title=text("title"),
            body=body,
        )

    # -- mutations --------------------------------------------------------

    def _refuse_protected(self, ref: str, reason: str) -> None:
        if ref.casefold() in self._protected or ref == self._authorized_base_branch:
            raise GitHubProtectedDestinationError(ref, reason)

    def ensure_branch(self, repository: str, branch: str, revision: str) -> str:
        repository = _require_text(repository, "repository")
        branch = _require_text(branch, "branch")
        revision = _require_text(revision, "revision")
        if not _REVISION_RE.fullmatch(revision):
            raise GitHubTransportError("revision must be an exact Git object id")
        self._refuse_protected(
            branch, "publication never writes the trunk or the authorized base"
        )

        argv = [
            "git",
            "-c",
            "credential.helper=",
            "-c",
            f"credential.helper={_CREDENTIAL_HELPER}",
            "push",
            self._remote,
            f"{revision}:refs/heads/{branch}",
        ]
        result = self._run(argv, cwd=self._source_path)
        if result.returncode != 0:
            detail = f"{result.stdout}\n{result.stderr}".lower()
            if any(marker in detail for marker in _NON_FAST_FORWARD_MARKERS):
                raise GitHubNonFastForwardPushError(
                    branch, revision, _last_line(result.stderr)
                )
            if any(marker in detail for marker in _PERMISSION_MARKERS):
                raise GitHubPermissionDeniedError("ensure_branch", repository)
            raise GitHubTransportError(
                f"git push failed: {_last_line(result.stderr)}"
            )

        self._record("ensure_branch", repository, branch, revision)
        remote = self.branch_revision(repository, branch)
        if remote != revision:
            raise GitHubBranchConflictError(branch, revision, remote)
        return revision

    def create_pull_request(
        self,
        repository: str,
        *,
        head_branch: str,
        base_branch: str,
        head_revision: str,
        expected_base_revision: str,
        publication_id: str,
        title: str,
        body: str,
    ) -> GitHubPullRequest:
        repository = _require_text(repository, "repository")
        head_branch = _require_text(head_branch, "head_branch")
        base_branch = _require_text(base_branch, "base_branch")
        head_revision = _require_text(head_revision, "head_revision")
        expected_base_revision = _require_text(
            expected_base_revision, "expected_base_revision"
        )

        self._refuse_protected(
            head_branch, "a pull request head is a review branch, never the trunk"
        )
        if base_branch != self._authorized_base_branch:
            raise GitHubProtectedDestinationError(
                base_branch,
                f"authorized base is {self._authorized_base_branch!r}",
            )
        if expected_base_revision != self._authorized_base_revision:
            raise GitHubProtectedDestinationError(
                base_branch,
                "the authorized base revision does not match this transport",
            )

        existing = self.find_pull_requests(
            repository, head_branch=head_branch, base_branch=base_branch
        )
        for pull_request in existing:
            if pull_request.publication_id == publication_id:
                return pull_request

        actual_base = self.base_revision(repository, base_branch)
        if actual_base != expected_base_revision:
            raise GitHubBaseMovedError(expected_base_revision, actual_base)

        result = self._api(
            f"repos/{repository}/pulls",
            fields=(
                f"head={head_branch}",
                f"base={base_branch}",
                f"title={title}",
                f"body={body}",
            ),
        )
        if result.returncode != 0:
            self._classify_create_failure(
                repository,
                head_branch=head_branch,
                base_branch=base_branch,
                publication_id=publication_id,
                result=result,
            )
            raise GitHubTransportError(
                f"gh api could not create the pull request: {_last_line(result.stderr)}"
            )

        payload = self._json(result, "pull-request creation")
        if not isinstance(payload, Mapping):
            raise GitHubTransportError(
                "GitHub returned a non-object pull-request creation response"
            )
        self._record("create_pull_request", repository, head_branch, head_revision)
        return self._pull_request(repository, payload)

    def _classify_create_failure(
        self,
        repository: str,
        *,
        head_branch: str,
        base_branch: str,
        publication_id: str,
        result: GhCommandResult,
    ) -> None:
        """Turn a failed creation into the most specific typed failure.

        A creation that GitHub actually performed but did not acknowledge is
        reconciled by the port, so it is reported as a lost acknowledgment
        rather than retried blindly.
        """

        text = f"{result.stderr}\n{result.stdout}".lower()
        try:
            existing = self.find_pull_requests(
                repository, head_branch=head_branch, base_branch=base_branch
            )
        except GitHubTransportError:
            existing = ()
        matching = tuple(
            pull_request
            for pull_request in existing
            if pull_request.publication_id == publication_id
        )
        if len(matching) == 1:
            raise GitHubAcknowledgmentLostError(matching[0]) from None
        if len(existing) > 1:
            raise GitHubDuplicatePullRequestError(existing) from None
        if any(marker in text for marker in _PERMISSION_MARKERS):
            raise GitHubPermissionDeniedError("create_pull_request", repository) from None

    def _record(
        self, kind: str, repository: str, target: str, revision: str | None = None
    ) -> None:
        self._operations.append(
            RemoteOperation(
                kind=kind,
                repository=repository,
                target=target,
                revision=revision,
            )
        )
