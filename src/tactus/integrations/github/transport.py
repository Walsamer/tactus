"""Transport-neutral GitHub publication contract.

This module holds the small GitHub surface the publication port consumes: the
versioned transport contract, the typed failures a transport may raise, the two
value objects those failures and results carry, and the protocol itself. It has
no client, no host, no token store and no network path — an implementation
either has one (a live transport) or does not (the deterministic fake).

Why this module exists: the publication port must not import its value objects
and errors from a *fake*. With the live and mock transports side by side, the
shared vocabulary belongs here and both implementations import it.

Credential discipline: :meth:`GitHubTransport.bind_credential` is the single
place a transport receives the opaque handle the guard resolved for exactly one
publication attempt. A transport must never put credential material in argv,
evidence, logs or a raised exception. ``reads_require_credential`` states
whether reads need that handle bound before they can run: a fake reads no
remote, a live transport cannot read a private repository without it, so the
port resolves the credential before reading for a live transport and after
every local check for a fake.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tactus.publication.guard import PublicationCredential

__all__ = [
    "GitHubAcknowledgmentLostError",
    "GitHubBaseMovedError",
    "GitHubBranchConflictError",
    "GitHubDuplicatePullRequestError",
    "GitHubNonFastForwardPushError",
    "GitHubPermissionDeniedError",
    "GitHubProtectedDestinationError",
    "GitHubPullRequest",
    "GitHubTransport",
    "GitHubTransportContract",
    "GitHubTransportError",
    "RemoteOperation",
]


# -- errors ---------------------------------------------------------------


class GitHubTransportError(RuntimeError):
    """Base class for typed transport failures."""


class GitHubPermissionDeniedError(GitHubTransportError):
    """The resolved credential may not perform the requested operation."""

    def __init__(self, operation: str, repository: str) -> None:
        self.operation = operation
        self.repository = repository
        super().__init__(f"GitHub denied {operation!r} on {repository!r}")


class GitHubBaseMovedError(GitHubTransportError):
    """The destination base ref moved away from the authorized revision."""

    def __init__(self, expected: str, actual: str | None) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            "GitHub base ref is no longer at the authorized revision: "
            f"expected {expected!r}, found {actual!r}"
        )


class GitHubBranchConflictError(GitHubTransportError):
    """A remote branch exists at a different revision than the verified one."""

    def __init__(self, branch: str, expected: str, actual: str | None) -> None:
        self.branch = branch
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"GitHub branch {branch!r} is at {actual!r}; expected {expected!r}"
        )


class GitHubNonFastForwardPushError(GitHubBranchConflictError):
    """A push was rejected because it would not fast-forward.

    Git refuses a non-fast-forward push unless ``--force`` is passed. Tactus
    never passes it: the refusal is the boundary, not an obstacle to route
    around, so it surfaces as a typed branch conflict and the verified revision
    stays exactly where it was.
    """

    def __init__(self, branch: str, expected: str, detail: str = "") -> None:
        self.detail = detail
        self.branch = branch
        self.expected = expected
        self.actual: str | None = None
        GitHubTransportError.__init__(
            self,
            f"refusing to push {branch!r}: the remote branch is not at the "
            f"verified revision {expected!r} and a non-fast-forward update is "
            "never forced",
        )


class GitHubProtectedDestinationError(GitHubTransportError):
    """A transport refused to publish onto a protected or unauthorized ref.

    Publication targets one review branch and one authorized base. A transport
    that is asked to write the trunk, or to open a pull request against a base
    other than the authorized one, refuses instead of rebinding.
    """

    def __init__(self, ref: str, reason: str) -> None:
        self.ref = ref
        self.reason = reason
        super().__init__(f"refusing to publish {ref!r}: {reason}")


class GitHubDuplicatePullRequestError(GitHubTransportError):
    """More than one pull request matches the same head/base pair."""

    def __init__(self, pull_requests: tuple["GitHubPullRequest", ...]) -> None:
        self.pull_requests = pull_requests
        super().__init__(
            "GitHub reported multiple pull requests for one head/base pair: "
            + ", ".join(str(pr.url) for pr in pull_requests)
        )


class GitHubAcknowledgmentLostError(GitHubTransportError):
    """Creation may have happened, but GitHub did not acknowledge it.

    The transport records the created pull request (when it can see one) before
    raising, so a caller reconciles through a read instead of blindly retrying
    a mutation.
    """

    def __init__(self, pull_request: "GitHubPullRequest | None") -> None:
        self.pull_request = pull_request
        super().__init__(
            "GitHub lost the pull-request creation acknowledgment"
            + (f" for {pull_request.url}" if pull_request is not None else "")
        )


# -- value objects --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GitHubTransportContract:
    """Versioned, machine-readable transport contract.

    ``live`` and ``network`` are what a publication port checks before it trusts
    a transport with an authorized effect. ``reads_require_credential`` states
    whether reads need a bound credential, which decides whether the port
    resolves the credential before or after its local checks.
    """

    contract_id: str
    version: str
    transport: str
    live: bool
    network: bool
    reads_require_credential: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_id": self.contract_id,
            "version": self.version,
            "transport": self.transport,
            "live": self.live,
            "network": self.network,
            "reads_require_credential": self.reads_require_credential,
        }


@dataclass(frozen=True, slots=True)
class RemoteOperation:
    """One remote mutation, recorded for boundary assertions."""

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
    """A pull request with no credential or network material."""

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
    """The narrow GitHub surface the publication port consumes."""

    @property
    def contract(self) -> GitHubTransportContract: ...

    @property
    def network_calls(self) -> int: ...

    @property
    def operations(self) -> tuple[RemoteOperation, ...]: ...

    def bind_credential(self, credential: "PublicationCredential") -> None:
        """Receive the one opaque credential resolved for this attempt."""

        ...

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
