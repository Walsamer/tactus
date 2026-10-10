"""Mocked GitHub Issue input for the Tactus WorkSource path.

The mock consumes the same normalized payload shape a future transport adapter
would produce:

.. code-block:: json

    {
      "repository": {"id": 1, "owner": "walsamer", "name": "tactus",
                     "url": "https://github.com/walsamer/tactus"},
      "issue": {"number": 21, "url": "https://github.com/walsamer/tactus/issues/21",
                "title": "...", "body": "...", "state": "open",
                "updated_at": "2026-10-10T14:35:38Z",
                "labels": ["fleet:ready"], "dependencies": [".../issues/4"],
                "author": "walsamer", "milestone": null}
    }

It is deliberately deterministic and offline: an unknown or explicitly
unavailable Issue raises :class:`SourceUnavailableError` rather than silently
returning no source.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from tactus.domain._clock import utcnow
from tactus.domain.admission import SourceRevision
from tactus.domain.work_source import (
    IssueReference,
    RepositoryIdentity,
    SourceUnavailableError,
    WorkSourceError,
    WorkSourceRevision,
)


def _parse_timestamp(value: str, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise WorkSourceError(f"{label} must be a non-empty ISO-8601 timestamp")
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise WorkSourceError(f"{label} is not a valid ISO-8601 timestamp: {value!r}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _require_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkSourceError(f"{label} must be a JSON object")
    return value


def parse_issue_reference(url: str) -> IssueReference:
    """Parse a full GitHub Issue URL into a stable reference."""

    if not isinstance(url, str) or not url.strip():
        raise WorkSourceError("dependency URL must be a non-empty string")
    parsed = urlparse(url.strip())
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[2] != "issues":
        raise WorkSourceError(f"dependency URL is not a GitHub Issue URL: {url!r}")
    owner, name, _, number_text = parts[0], parts[1], parts[2], parts[3]
    if not number_text.isdigit():
        raise WorkSourceError(f"dependency URL has no Issue number: {url!r}")
    return IssueReference(
        repository=f"{owner}/{name}", number=int(number_text), url=url.strip()
    )


def parse_issue_payload(
    payload: Mapping[str, Any], *, observed_at: datetime | None = None
) -> WorkSourceRevision:
    """Normalize one mocked GitHub payload into an immutable source revision."""

    root = _require_mapping(payload, "payload")
    repository = _require_mapping(root.get("repository"), "repository")
    issue = _require_mapping(root.get("issue"), "issue")

    labels_raw = issue.get("labels", [])
    if not isinstance(labels_raw, (list, tuple)):
        raise WorkSourceError("issue.labels must be a list")
    dependencies_raw = issue.get("dependencies", [])
    if not isinstance(dependencies_raw, (list, tuple)):
        raise WorkSourceError("issue.dependencies must be a list")

    observed = observed_at if observed_at is not None else utcnow()
    updated_at = _parse_timestamp(str(issue.get("updated_at", "")), "issue.updated_at")

    return WorkSourceRevision(
        repository=RepositoryIdentity(
            owner=str(repository.get("owner", "")),
            name=str(repository.get("name", "")),
            repository_id=repository.get("id"),  # type: ignore[arg-type]
            url=str(repository.get("url", "")),
        ),
        number=issue.get("number"),  # type: ignore[arg-type]
        url=str(issue.get("url", "")),
        title=str(issue.get("title", "")),
        body=str(issue.get("body", "") or ""),
        source_revision=SourceRevision(str(issue.get("revision", issue.get("updated_at", "")))),
        updated_at=updated_at,
        observed_at=observed,
        labels=tuple(str(label) for label in labels_raw),
        dependencies=tuple(parse_issue_reference(str(url)) for url in dependencies_raw),
        issue_state=str(issue.get("state", "open")),
        author=str(issue["author"]) if issue.get("author") is not None else None,
        milestone=str(issue["milestone"]) if issue.get("milestone") is not None else None,
    )


def load_issue_payload(path: str | Path) -> Mapping[str, Any]:
    """Load a deterministic local import fixture from JSON."""

    text = Path(path).read_text(encoding="utf-8")
    payload = json.loads(text)
    return _require_mapping(payload, "fixture")


@runtime_checkable
class GitHubIssueSource(Protocol):
    """Narrow boundary that fetches one checked source revision."""

    def fetch(self, repository: str, number: int) -> WorkSourceRevision: ...


@dataclass
class MockGitHubIssueSource:
    """Deterministic, offline GitHub Issue input."""

    payloads: Mapping[tuple[str, int], Mapping[str, Any]]
    unavailable: frozenset[tuple[str, int]] = field(default_factory=frozenset)
    clock: Callable[[], datetime] = utcnow

    def __post_init__(self) -> None:
        self.payloads = {
            (str(repo), int(number)): payload
            for (repo, number), payload in self.payloads.items()
        }
        self.unavailable = frozenset(
            (str(repo), int(number)) for repo, number in self.unavailable
        )

    @classmethod
    def from_files(
        cls,
        fixtures: Mapping[tuple[str, int], str | Path],
        *,
        unavailable: frozenset[tuple[str, int]] = frozenset(),
        clock: Callable[[], datetime] = utcnow,
    ) -> "MockGitHubIssueSource":
        return cls(
            payloads={key: load_issue_payload(path) for key, path in fixtures.items()},
            unavailable=unavailable,
            clock=clock,
        )

    def fetch(self, repository: str, number: int) -> WorkSourceRevision:
        key = (str(repository), int(number))
        if key in self.unavailable:
            raise SourceUnavailableError(f"source {key[0]}#{key[1]} is unavailable")
        payload = self.payloads.get(key)
        if payload is None:
            raise SourceUnavailableError(f"source {key[0]}#{key[1]} is not available")
        return parse_issue_payload(payload, observed_at=self.clock())


__all__ = [
    "GitHubIssueSource",
    "MockGitHubIssueSource",
    "load_issue_payload",
    "parse_issue_payload",
    "parse_issue_reference",
]
