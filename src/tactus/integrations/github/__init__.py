"""GitHub integration adapters.

The work-source adapter is the anti-corruption boundary around GitHub's
canonical Issue intent. The publication transport is a deterministic fake:
it has no network path, no token store and no live GitHub client. Both keep
local imports and tests offline.
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
