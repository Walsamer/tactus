"""GitHub integration adapters.

The work-source adapter is the anti-corruption boundary around GitHub's
canonical Issue intent. Publication has two transports behind one contract
(``transport``): a deterministic fake with no network path, and a live one over
``git push`` and ``gh api`` (``gh``) that is only reachable through the live
publication port. The fake keeps every test offline.
"""

from .fake import (
    FAKE_GITHUB_CONTRACT_ID,
    FAKE_GITHUB_VERSION,
    FakeGitHubTransport,
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
from .work_source import (
    GitHubIssueSource,
    MockGitHubIssueSource,
    load_issue_payload,
    parse_issue_payload,
)

__all__ = [
    "FAKE_GITHUB_CONTRACT_ID",
    "FAKE_GITHUB_VERSION",
    "FakeGitHubTransport",
    "GitHubAcknowledgmentLostError",
    "GitHubBaseMovedError",
    "GitHubBranchConflictError",
    "GitHubDuplicatePullRequestError",
    "GitHubIssueSource",
    "GitHubPermissionDeniedError",
    "GitHubPullRequest",
    "GitHubTransport",
    "GitHubTransportContract",
    "GitHubTransportError",
    "MockGitHubIssueSource",
    "RemoteOperation",
    "load_issue_payload",
    "parse_issue_payload",
]
