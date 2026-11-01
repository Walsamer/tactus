"""Deterministic fake GitHub transport for mocked publication.

This module is deliberately a **fake**. It contains no HTTP client, no socket,
no token store and no URL that resolves to a real remote. It models only the
small GitHub surface a Tactus publication port needs so tests can prove
create/reuse, failure and reconciliation behaviour without touching GitHub:

* a remote branch ref map (simulated ``git push``/branch publication);
* a remote pull-request list (simulated PR creation);
* explicit, typed failure modes for permission denial, a moved base, branch
  conflicts, duplicate pull requests and a lost create acknowledgment.

Every mutation is recorded as a :class:`RemoteOperation`. ``network_calls`` is
always zero: the fake cannot accidentally become a live transport. A future
live adapter must be a separate, reviewed implementation behind the same
protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = [
    "FAKE_GITHUB_CONTRACT_ID",
    "FAKE_GITHUB_VERSION",
    "FakeGitHubTransport",
    "GitHubAcknowledgmentLostError",
    "GitHubBaseMovedError",
    "GitHubBranchConflictError",
    "GitHubDuplicatePullRequestError",
    "GitHubPermissionDeniedError",
    "GitHubPullRequest",
    "GitHubTransport",
    "GitHubTransportContract",
    "GitHubTransportError",
    "RemoteOperation",
]


FAKE_GITHUB_CONTRACT_ID = "tactus.integrations.github.fake/v1"
FAKE_GITHUB_VERSION = "1.0.0"

_PR_URL_TEMPLATE = "https://github.invalid/{repository}/pull/{number}"


def _require_text(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


# -- errors ---------------------------------------------------------------


class GitHubTransportError(RuntimeError):
    """Base class for typed fake-transport failures."""


class GitHubPermissionDeniedError(GitHubTransportError):
    """The simulated credential may not perform the requested operation."""

    def __init__(self, operation: str, repository: str) -> None:
        self.operation = operation
        self.repository = repository
        super().__init__(
            f"fake GitHub denied {operation!r} on {repository!r}"
        )


class GitHubBaseMovedError(GitHubTransportError):
    """The destination base ref moved away from the authorized revision."""

    def __init__(self, expected: str, actual: str | None) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            "fake GitHub base ref is no longer at the authorized revision: "
            f"expected {expected!r}, found {actual!r}"
        )


class GitHubBranchConflictError(GitHubTransportError):
    """A remote branch exists at a different revision than the verified one."""

    def __init__(self, branch: str, expected: str, actual: str | None) -> None:
        self.branch = branch
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"fake GitHub branch {branch!r} is at {actual!r}; expected {expected!r}"
        )


class GitHubDuplicatePullRequestError(GitHubTransportError):
    """More than one open pull request matches the same head/base pair."""

    def __init__(self, pull_requests: tuple["GitHubPullRequest", ...]) -> None:
        self.pull_requests = pull_requests
        super().__init__(
            "fake GitHub found multiple pull requests for one head/base pair: "
            + ", ".join(str(pr.url) for pr in pull_requests)
        )


class GitHubAcknowledgmentLostError(GitHubTransportError):
    """Creation may have happened, but GitHub did not acknowledge it.

    The fake records the created pull request *before* raising so a caller can
    prove it reconciles through a read instead of blindly retrying a mutation.
    """

    def __init__(self, pull_request: "GitHubPullRequest | None") -> None:
        self.pull_request = pull_request
        super().__init__(
            "fake GitHub lost the pull-request creation acknowledgment"
            + (f" for {pull_request.url}" if pull_request is not None else "")
        )


# -- value objects --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GitHubTransportContract:
    """Versioned, machine-readable fake-transport contract."""

    contract_id: str = FAKE_GITHUB_CONTRACT_ID
    version: str = FAKE_GITHUB_VERSION
    transport: str = "fake"
    live: bool = False
    network: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_id": self.contract_id,
            "version": self.version,
            "transport": self.transport,
            "live": self.live,
            "network": self.network,
        }


@dataclass(frozen=True, slots=True)
class RemoteOperation:
    """One simulated remote mutation, recorded for boundary assertions."""

    kind: str
    repository: str
    target: str
    revision: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "repository": self.repository,
            "target": self.target,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class GitHubPullRequest:
    """A fake pull request with no credential or network material."""

    number: int
    url: str
    repository: str
    head_branch: str
    base_branch: str
    head_revision: str
    base_revision: str
    publication_id: str
    title: str
    body: str

    def as_dict(self) -> dict[str, object]:
        return {
            "number": self.number,
            "url": self.url,
            "repository": self.repository,
            "head_branch": self.head_branch,
            "base_branch": self.base_branch,
            "head_revision": self.head_revision,
            "base_revision": self.base_revision,
            "publication_id": self.publication_id,
            "title": self.title,
        }


@runtime_checkable
class GitHubTransport(Protocol):
    """The narrow fake-GitHub surface the publication port consumes."""

    @property
    def contract(self) -> GitHubTransportContract: ...

    @property
    def network_calls(self) -> int: ...

    @property
    def operations(self) -> tuple[RemoteOperation, ...]: ...

    def branch_revision(self, repository: str, branch: str) -> str | None: ...

    def base_revision(self, repository: str, branch: str) -> str | None: ...

    def find_pull_requests(
        self,
        repository: str,
        *,
        head_branch: str,
        base_branch: str,
    ) -> tuple[GitHubPullRequest, ...]: ...

    def ensure_branch(
        self, repository: str, branch: str, revision: str
    ) -> str: ...

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
    ) -> GitHubPullRequest: ...


class FakeGitHubTransport:
    """In-memory GitHub stand-in with deterministic failure injection.

    There is intentionally no constructor parameter for a host, URL, token or
    HTTP client. The only external effects are entries in local dictionaries.
    """

    def __init__(
        self,
        *,
        permission_denied: bool = False,
        lose_next_acknowledgment: bool = False,
    ) -> None:
        self._branches: dict[tuple[str, str], str] = {}
        self._base_revisions: dict[tuple[str, str], str] = {}
        self._pull_requests: list[GitHubPullRequest] = []
        self._operations: list[RemoteOperation] = []
        self._reads = 0
        self._permission_denied = permission_denied
        self._lose_next_acknowledgment = lose_next_acknowledgment
        self._next_number = 1
        self._contract = GitHubTransportContract()

    # -- contract / evidence ---------------------------------------------

    @property
    def contract(self) -> GitHubTransportContract:
        return self._contract

    @property
    def network_calls(self) -> int:
        """Always zero: this fake has no network path at all."""

        return 0

    @property
    def reads(self) -> int:
        return self._reads

    @property
    def operations(self) -> tuple[RemoteOperation, ...]:
        return tuple(self._operations)

    @property
    def pull_requests(self) -> tuple[GitHubPullRequest, ...]:
        return tuple(self._pull_requests)

    def clear_operations(self) -> None:
        self._operations.clear()

    def _record(self, kind: str, repository: str, target: str, revision: str | None = None) -> None:
        self._operations.append(
            RemoteOperation(
                kind=kind,
                repository=repository,
                target=target,
                revision=revision,
            )
        )

    # -- test setup (never counted as a Tactus operation) -----------------

    def seed_branch(self, repository: str, branch: str, revision: str) -> None:
        self._branches[
            (_require_text(repository, "repository"), _require_text(branch, "branch"))
        ] = _require_text(revision, "revision")

    def set_base_revision(self, repository: str, branch: str, revision: str) -> None:
        self._base_revisions[
            (_require_text(repository, "repository"), _require_text(branch, "branch"))
        ] = _require_text(revision, "revision")

    def seed_pull_request(
        self,
        repository: str,
        *,
        head_branch: str,
        base_branch: str,
        head_revision: str,
        base_revision: str,
        publication_id: str,
        title: str = "seeded",
        body: str = "seeded",
        number: int | None = None,
    ) -> GitHubPullRequest:
        repository = _require_text(repository, "repository")
        number = self._next_number if number is None else number
        self._next_number = max(self._next_number, number) + 1
        pull_request = GitHubPullRequest(
            number=number,
            url=_PR_URL_TEMPLATE.format(repository=repository, number=number),
            repository=repository,
            head_branch=_require_text(head_branch, "head_branch"),
            base_branch=_require_text(base_branch, "base_branch"),
            head_revision=_require_text(head_revision, "head_revision"),
            base_revision=_require_text(base_revision, "base_revision"),
            publication_id=_require_text(publication_id, "publication_id"),
            title=title,
            body=body,
        )
        self._pull_requests.append(pull_request)
        return pull_request

    # -- reads ------------------------------------------------------------

    def branch_revision(self, repository: str, branch: str) -> str | None:
        self._reads += 1
        return self._branches.get((repository, branch))

    def base_revision(self, repository: str, branch: str) -> str | None:
        self._reads += 1
        return self._base_revisions.get((repository, branch))

    def find_pull_requests(
        self,
        repository: str,
        *,
        head_branch: str,
        base_branch: str,
    ) -> tuple[GitHubPullRequest, ...]:
        self._reads += 1
        return tuple(
            pull_request
            for pull_request in self._pull_requests
            if pull_request.repository == repository
            and pull_request.head_branch == head_branch
            and pull_request.base_branch == base_branch
        )

    # -- simulated mutations ---------------------------------------------

    def ensure_branch(
        self, repository: str, branch: str, revision: str
    ) -> str:
        repository = _require_text(repository, "repository")
        branch = _require_text(branch, "branch")
        revision = _require_text(revision, "revision")
        self._record("ensure_branch", repository, branch, revision)
        existing = self._branches.get((repository, branch))
        if existing is not None and existing != revision:
            raise GitHubBranchConflictError(branch, revision, existing)
        self._branches[(repository, branch)] = revision
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
        publication_id = _require_text(publication_id, "publication_id")
        self._record("create_pull_request", repository, head_branch, head_revision)

        if self._permission_denied:
            raise GitHubPermissionDeniedError("create_pull_request", repository)

        actual_base = self._base_revisions.get((repository, base_branch))
        if actual_base is None or actual_base != expected_base_revision:
            raise GitHubBaseMovedError(expected_base_revision, actual_base)

        actual_head = self._branches.get((repository, head_branch))
        if actual_head is None or actual_head != head_revision:
            raise GitHubBranchConflictError(head_branch, head_revision, actual_head)

        existing = self.find_pull_requests(
            repository,
            head_branch=head_branch,
            base_branch=base_branch,
        )
        if existing:
            raise GitHubDuplicatePullRequestError(existing)

        number = self._next_number
        self._next_number += 1
        pull_request = GitHubPullRequest(
            number=number,
            url=_PR_URL_TEMPLATE.format(repository=repository, number=number),
            repository=repository,
            head_branch=head_branch,
            base_branch=base_branch,
            head_revision=head_revision,
            base_revision=actual_base,
            publication_id=publication_id,
            title=title,
            body=body,
        )
        self._pull_requests.append(pull_request)

        # A lost acknowledgment is a *response* failure, not a creation
        # failure: the fake remote already holds the pull request.
        if self._lose_next_acknowledgment:
            self._lose_next_acknowledgment = False
            raise GitHubAcknowledgmentLostError(pull_request)
        return pull_request
