"""GitHub work-source integration.

The adapter is the anti-corruption boundary around GitHub's canonical Issue
intent. It normalizes a checked Issue revision into a Tactus
:class:`~tactus.domain.work_source.WorkSourceRevision` and never mutates GitHub
state. The default implementation is a deterministic in-memory mock so local
import and tests require no network access.
"""

from .work_source import (
    GitHubIssueSource,
    MockGitHubIssueSource,
    load_issue_payload,
    parse_issue_payload,
)

__all__ = [
    "GitHubIssueSource",
    "MockGitHubIssueSource",
    "load_issue_payload",
    "parse_issue_payload",
]
