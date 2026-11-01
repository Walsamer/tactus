"""Publication grant/intent value-object invariants."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tactus.domain import SourceRevision, WorkOrderId
from tactus.publication import (
    PublicationEffect,
    PublicationError,
    PublicationGrant,
    PublicationRequest,
    normalize_scope,
    scope_covers,
)

NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
BASE = "a" * 40
NEXT_BASE = "b" * 40


def make_request(**overrides: object) -> PublicationRequest:
    defaults: dict[str, object] = dict(
        intent_id="intent-1",
        work_order_id=WorkOrderId("WO-1"),
        repository="Walsamer/tactus",
        branch="agent/tactus/publication",
        base_revision=BASE,
        destination="main",
        scope=("src/publication/guard.py",),
        source_revision=SourceRevision("src-rev-1"),
    )
    defaults.update(overrides)
    return PublicationRequest(**defaults)  # type: ignore[arg-type]


def make_grant(**overrides: object) -> PublicationGrant:
    defaults: dict[str, object] = dict(
        grant_id="grant-1",
        authority="operator",
        work_order_id=WorkOrderId("WO-1"),
        repository="Walsamer/tactus",
        branch="agent/tactus/publication",
        base_revision=BASE,
        destination="main",
        scope=("src", "tests/publication"),
        source_revision=SourceRevision("src-rev-1"),
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(hours=1),
    )
    defaults.update(overrides)
    return PublicationGrant(**defaults)  # type: ignore[arg-type]


def test_request_normalizes_scope_and_commit_identity() -> None:
    request = make_request(scope=("src/**", "src/", "tests/publication/**"))

    assert request.scope == ("src", "tests/publication")
    assert request.base_revision == BASE
    assert request.effect is PublicationEffect.OPEN_PULL_REQUEST


def test_publication_identity_is_content_addressed_and_stable() -> None:
    first = make_request()
    second = make_request(scope=("src/publication/guard.py",))

    assert first.publication_id == second.publication_id
    assert first.intent_digest == second.intent_digest
    assert first.publication_id.startswith("pub-")
    assert len(first.publication_id) == len("pub-") + 32


def test_publication_identity_changes_with_any_bound_field() -> None:
    original = make_request()
    changed = [
        make_request(intent_id="intent-2"),
        make_request(work_order_id=WorkOrderId("WO-2")),
        make_request(repository="Walsamer/other"),
        make_request(branch="agent/tactus/other"),
        make_request(base_revision=NEXT_BASE),
        make_request(destination="release"),
        make_request(scope=("src",)),
        make_request(source_revision=SourceRevision("src-rev-2")),
        make_request(effect=PublicationEffect.PUBLISH_REVIEW_BRANCH),
    ]

    assert len({item.publication_id for item in changed} | {original.publication_id}) == len(
        changed
    ) + 1


def test_scope_coverage_is_segment_prefix_not_string_prefix() -> None:
    assert scope_covers(("src",), ("src/publication/guard.py",))
    assert scope_covers(("src/**",), ("src/publication/**",))
    assert not scope_covers(("src",), ("src2/guard.py",))
    assert not scope_covers((), ("src/guard.py",))


def test_scope_rejects_bare_wildcards_and_traversal() -> None:
    for entry in ("**", "*", "src/../../etc", "/absolute", "..", "../outside"):
        with pytest.raises(PublicationError):
            normalize_scope((entry,))


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository", "not-a-slug"),
        ("branch", "-bad-branch"),
        ("branch", "bad..branch"),
        ("base_revision", "main"),
        ("base_revision", "A" * 40),
        ("destination", ""),
        ("intent_id", ""),
    ],
)
def test_request_rejects_unsafe_or_floating_identity(field: str, value: str) -> None:
    with pytest.raises(PublicationError):
        make_request(**{field: value})


def test_grant_is_fresh_only_before_expiry_and_never_when_revoked() -> None:
    grant = make_grant(expires_at=NOW)

    assert grant.is_fresh_at(NOW - timedelta(seconds=1))
    assert not grant.is_fresh_at(NOW)

    revoked = make_grant(revoked_at=NOW - timedelta(seconds=1))
    assert revoked.is_revoked
    assert not revoked.is_fresh_at(NOW)


def test_grant_rejects_inverted_expiry_and_naive_timestamps() -> None:
    with pytest.raises(PublicationError):
        make_grant(expires_at=NOW - timedelta(hours=1))

    with pytest.raises(PublicationError):
        make_grant(issued_at=datetime(2026, 1, 1, 12))
