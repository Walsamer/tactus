"""Publication composition: one grant, one verified change, one effect."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tactus.application.publish_change import (
    publication_intent_id,
    publication_request_for,
    publish_change,
)
from tactus.domain import SourceRevision, WorkOrderId
from tactus.domain.human_intervention import HumanAction, HumanGrant
from tactus.integrations.github import FakeGitHubTransport
from tactus.integrations.stax.source_control import (
    PreparedChangeStateError,
    load_prepared_change,
)
from tactus.publication import (
    GitHubPublicationPort,
    PublicationCredential,
    PublicationStatus,
)

NOW = datetime(2026, 11, 1, 14, 0, tzinfo=timezone.utc)
WORK_ORDER = WorkOrderId("TCT-WO-023B2-live-publication")
REPOSITORY = "Walsamer/tactus"
BRANCH = "agent/tactus/TCT-WO-023B2-live-publication"
BASE = "a" * 40
BRANCH_REVISION = "b" * 40
PATCH_DIGEST = hashlib.sha256(b"verified patch").hexdigest()
PREPARED_CHANGE_ID = "chg-" + hashlib.sha256(PATCH_DIGEST.encode()).hexdigest()[:32]


class CountingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, grant, request, *, at):
        self.calls += 1
        return PublicationCredential("test-credential", provider_id="test")


def make_grant(**overrides: object) -> HumanGrant:
    defaults: dict[str, object] = dict(
        grant_id="grant-human-1",
        resolution_id="resolution-1",
        intervention_id="intervention-1",
        work_order_id=WORK_ORDER,
        actor="Walsamer",
        action=HumanAction.PUBLISH,
        scope=("src/publication", "docs"),
        source_revision=SourceRevision(BASE),
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(hours=1),
    )
    defaults.update(overrides)
    return HumanGrant(**defaults)  # type: ignore[arg-type]


def write_manifest(
    state_root: Path,
    *,
    change_id: str = "change-1",
    work_order_id: str = str(WORK_ORDER),
    source_revision: str = BASE,
    base_revision: str = BASE,
    review_branch: str = BRANCH,
    branch_revision: str = BRANCH_REVISION,
    schema_version: int = 1,
) -> Path:
    directory = state_root / ".tactus" / "changes"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{hashlib.sha256(change_id.encode()).hexdigest()}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "change_id": change_id,
                "prepared_change_id": PREPARED_CHANGE_ID,
                "request_fingerprint": "f" * 64,
                "facts": {
                    "change_id": change_id,
                    "work_order_id": work_order_id,
                    "repository": "/tmp/tactus-test-repository",
                    "source_revision": source_revision,
                    "base_revision": base_revision,
                    "patch_digest": PATCH_DIGEST,
                    "patch_bytes": 14,
                    "review_branch": review_branch,
                    "commit_message": "Tactus: verified change",
                    "attempt_id": "attempt-1",
                },
                "review_branch": review_branch,
                "branch_revision": branch_revision,
                "workspace_path": "/tmp/tactus-test-workspace",
                "patch_digest": PATCH_DIGEST,
                "patch_bytes": 14,
                "created_at": NOW.isoformat(),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def make_port(tmp_path: Path, transport: FakeGitHubTransport, credentials: CountingProvider):
    return GitHubPublicationPort(transport, credentials, state_root=tmp_path)


def test_one_grant_publishes_one_review_branch_and_pull_request(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    write_manifest(state_root)
    change = load_prepared_change(state_root, "change-1")

    transport = FakeGitHubTransport()
    transport.set_base_revision(REPOSITORY, "main", BASE)
    credentials = CountingProvider()
    port = make_port(tmp_path / "publication", transport, credentials)

    result = publish_change(
        grant=make_grant(),
        change=change,
        repository=REPOSITORY,
        destination="main",
        port=port,
        at=NOW,
    )

    assert result.publication_status is PublicationStatus.PUBLISHED
    assert result.grant_id == "grant-human-1"
    assert result.pull_request_url is not None
    assert credentials.calls == 1
    assert len(transport.pull_requests) == 1

    record = port.evidence(result.publication_id)
    assert record is not None
    assert record["grant_id"] == "grant-human-1"
    assert record["authority"] == "operator"
    assert record["branch"] == BRANCH
    assert record["base_revision"] == BASE


def test_repeating_the_same_join_reuses_the_same_pull_request(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    write_manifest(state_root)
    change = load_prepared_change(state_root, "change-1")

    transport = FakeGitHubTransport()
    transport.set_base_revision(REPOSITORY, "main", BASE)
    publication_root = tmp_path / "publication"
    first = publish_change(
        grant=make_grant(),
        change=change,
        repository=REPOSITORY,
        destination="main",
        port=make_port(publication_root, transport, CountingProvider()),
        at=NOW,
    )

    credentials = CountingProvider()
    second = publish_change(
        grant=make_grant(),
        change=change,
        repository=REPOSITORY,
        destination="main",
        port=make_port(publication_root, transport, credentials),
        at=NOW,
    )

    assert second.publication_status is PublicationStatus.REUSED
    assert second.pull_request_url == first.pull_request_url
    assert second.publication_id == first.publication_id
    assert credentials.calls == 0
    assert len(transport.pull_requests) == 1


def test_a_revoked_grant_is_refused_without_touching_the_credential(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    write_manifest(state_root)
    change = load_prepared_change(state_root, "change-1")
    transport = FakeGitHubTransport()
    transport.set_base_revision(REPOSITORY, "main", BASE)
    credentials = CountingProvider()
    port = make_port(tmp_path / "publication", transport, credentials)

    result = publish_change(
        grant=make_grant(revoked_at=NOW - timedelta(minutes=1)),
        change=change,
        repository=REPOSITORY,
        destination="main",
        port=port,
        at=NOW,
    )

    assert result.publication_status is PublicationStatus.REFUSED
    assert result.error_code == "REVOKED"
    assert credentials.calls == 0
    assert transport.pull_requests == ()
    assert result.implementation_verified


def test_the_intent_identity_is_derived_only_from_durable_facts(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    write_manifest(state_root)
    change = load_prepared_change(state_root, "change-1")

    first = publication_intent_id(make_grant(), change)
    second = publication_intent_id(make_grant(), change)
    other = publication_intent_id(make_grant(grant_id="grant-human-2"), change)

    assert first == second
    assert first != other
    assert PREPARED_CHANGE_ID in first


def test_the_request_and_grant_bind_the_same_change(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    write_manifest(state_root)
    change = load_prepared_change(state_root, "change-1")

    request, grant = publication_request_for(
        make_grant(), change, repository=REPOSITORY, destination="main"
    )

    assert request.branch == grant.branch == BRANCH
    assert request.base_revision == grant.base_revision == BASE
    assert request.intent_id == publication_intent_id(make_grant(), change)
    assert request.repository == REPOSITORY
    assert request.destination == "main"


def test_a_recorded_preparation_loads_without_re_preparing(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    write_manifest(state_root)

    change = load_prepared_change(state_root, "change-1")

    assert change.change_id == "change-1"
    assert change.review_branch == BRANCH
    assert change.branch_revision == BRANCH_REVISION
    assert change.base_revision == BASE
    assert change.implementation_verified
    assert change.publication_eligible is False


def test_an_unknown_preparation_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PreparedChangeStateError):
        load_prepared_change(tmp_path, "change-unknown")


def test_a_manifest_for_another_change_is_refused(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    path = write_manifest(state_root, change_id="change-1")
    other = state_root / ".tactus" / "changes" / f"{hashlib.sha256(b'change-2').hexdigest()}.json"
    other.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(PreparedChangeStateError):
        load_prepared_change(state_root, "change-2")


def test_an_unsupported_manifest_schema_is_refused(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    write_manifest(state_root, schema_version=99)

    with pytest.raises(PreparedChangeStateError):
        load_prepared_change(state_root, "change-1")
