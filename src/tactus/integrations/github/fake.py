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
always zero: the fake cannot accidentally become a live transport.

The vocabulary it speaks — the transport contract, its value objects and its
typed failures — lives in :mod:`tactus.integrations.github.transport`, so the
live transport and this fake are different implementations of exactly one
contract rather than two dialects. The public names below are re-exported here
because that is where callers have always imported them from.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .transport import (
    GitHubAcknowledgmentLostError,
    GitHubBaseMovedError,
    GitHubBranchConflictError,
    GitHubDuplicatePullRequestError,
    GitHubPermissionDeniedError,
    GitHubPullRequest,
    GitHubTransport,
    GitHubTransportContract,
    GitHubTransportError,
    RemoteOperation,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tactus.publication.guard import PublicationCredential

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

from .transport import GitHubTransport  # noqa: F401  (re-exported; see __all__)

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
        self._credentials_bound = 0
        self._permission_denied = permission_denied
        self._lose_next_acknowledgment = lose_next_acknowledgment
        self._next_number = 1
        self._contract = GitHubTransportContract(
            contract_id=FAKE_GITHUB_CONTRACT_ID,
            version=FAKE_GITHUB_VERSION,
            transport="fake",
            live=False,
            network=False,
            reads_require_credential=False,
        )

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
    def credentials_bound(self) -> int:
        """How many credentials were bound; the fake keeps no material."""

        return self._credentials_bound

    @property
    def operations(self) -> tuple[RemoteOperation, ...]:
        return tuple(self._operations)

    @property
    def pull_requests(self) -> tuple[GitHubPullRequest, ...]:
        return tuple(self._pull_requests)

    def clear_operations(self) -> None:
        self._operations.clear()

    def bind_credential(self, credential: "PublicationCredential") -> None:
        """Accept the resolved handle and retain nothing.

        The fake has no remote to authenticate against, so it only records that
        a credential reached the transport boundary for this attempt.
        """

        self._credentials_bound += 1

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
