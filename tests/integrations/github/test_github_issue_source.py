"""Mocked GitHub Issue input adapter tests (issue #21).

The pinned fixture is the Tactus half of a deterministic source revision: the
same payload must always normalize to the same body/specification hashes so an
import can be replayed and rechecked offline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tactus.domain import SourceUnavailableError
from tactus.integrations.github import (
    GitHubIssueSource,
    MockGitHubIssueSource,
    load_issue_payload,
    parse_issue_payload,
)
from tactus.integrations.github.work_source import parse_issue_reference

FIXTURE = Path(__file__).parent / "data" / "issue_21.json"
OBSERVED_AT = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)

#: Pinned digests for the deterministic fixture. Any accidental fixture edit
#: fails this test so provenance can never drift silently.
PINNED_BODY_SHA256 = "53400965cfbbbabde62aa34cca4aebe4ce5a673d2b1df8e6dffba5eae4ed3cd6"
PINNED_SPECIFICATION_DIGEST = (
    "e1a67ea83ee6f18cadbf234c0272ea1227e986a1664f896553bc01005ddbf44b"
)


def source() -> MockGitHubIssueSource:
    payload = load_issue_payload(FIXTURE)
    return MockGitHubIssueSource(
        {("walsamer/tactus", 21): payload},
        clock=lambda: OBSERVED_AT,
    )


def test_mock_source_satisfies_github_source_protocol() -> None:
    assert isinstance(source(), GitHubIssueSource)


def test_pinned_fixture_normalizes_to_stable_provenance() -> None:
    revision = source().fetch("walsamer/tactus", 21)
    assert revision.repository.owner == "walsamer"
    assert revision.repository.name == "tactus"
    assert revision.repository.repository_id == 1404239931
    assert revision.number == 21
    assert revision.issue_state == "open"
    assert revision.body_sha256 == PINNED_BODY_SHA256
    assert revision.specification_digest == PINNED_SPECIFICATION_DIGEST
    assert revision.import_key.value == f"walsamer/tactus#21@{PINNED_SPECIFICATION_DIGEST}"
    assert revision.observed_at == OBSERVED_AT
    assert revision.is_authorized


def test_dependencies_are_parsed_into_stable_references() -> None:
    revision = source().fetch("walsamer/tactus", 21)
    assert [(dep.repository, dep.number) for dep in revision.dependencies] == [
        ("walsamer/tactus", 3),
        ("walsamer/tactus", 4),
    ]


def test_unknown_source_is_explicitly_unavailable() -> None:
    with pytest.raises(SourceUnavailableError):
        source().fetch("walsamer/tactus", 999)


def test_explicitly_unavailable_source_raises() -> None:
    payload = load_issue_payload(FIXTURE)
    adapter = MockGitHubIssueSource(
        {("walsamer/tactus", 21): payload},
        unavailable=frozenset({("walsamer/tactus", 21)}),
    )
    with pytest.raises(SourceUnavailableError):
        adapter.fetch("walsamer/tactus", 21)


def test_parse_issue_reference_rejects_non_issue_urls() -> None:
    with pytest.raises(ValueError):
        parse_issue_reference("https://github.com/walsamer/tactus/pull/3")
    with pytest.raises(ValueError):
        parse_issue_reference("not-a-url")


def test_parse_issue_payload_requires_repository_and_issue() -> None:
    with pytest.raises(ValueError):
        parse_issue_payload({"issue": {}}, observed_at=OBSERVED_AT)
