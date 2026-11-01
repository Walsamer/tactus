"""Mocked publication: idempotent PR create/reuse with zero network operations."""

from __future__ import annotations

import hashlib
import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tactus.domain import SourceRevision, WorkOrderId
from tactus.integrations.github import FakeGitHubTransport
from tactus.integrations.stax import (
    ImplementationOutcome,
    ImplementationVerification,
    PreparedChange,
    PublicationEligibility,
)
from tactus.publication import (
    GitHubPublicationPort,
    ImplementationStatus,
    PublicationCredential,
    PublicationErrorCode,
    PublicationGrant,
    PublicationRequest,
    PublicationStatus,
)

NOW = datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc)
WORK_ORDER = WorkOrderId("TCT-WO-023B2-mocked-publication")
REPOSITORY = "example/tactus"
BRANCH = "agent/tactus/TCT-WO-023B2-mocked-publication"
BASE = "a" * 40
OTHER_BASE = "b" * 40
BRANCH_REVISION = "c" * 40
PATCH_DIGEST = hashlib.sha256(b"verified patch").hexdigest()
OTHER_DIGEST = hashlib.sha256(b"different patch").hexdigest()


class FakeCredentialProvider:
    """Count credential resolutions without keeping any real secret."""

    def __init__(self) -> None:
        self.calls = 0

    def resolve(
        self, grant: PublicationGrant, request: PublicationRequest, *, at: datetime
    ) -> PublicationCredential:
        self.calls += 1
        return PublicationCredential("fake-publication-credential", provider_id="fake")


def make_request(**overrides: object) -> PublicationRequest:
    defaults: dict[str, object] = dict(
        intent_id="intent-publication-1",
        work_order_id=WORK_ORDER,
        repository=REPOSITORY,
        branch=BRANCH,
        base_revision=BASE,
        destination="main",
        scope=("src/", "tests/publication/"),
        source_revision=SourceRevision(BASE),
    )
    defaults.update(overrides)
    return PublicationRequest(**defaults)  # type: ignore[arg-type]


def make_grant(**overrides: object) -> PublicationGrant:
    defaults: dict[str, object] = dict(
        grant_id="grant-publication-1",
        authority="operator",
        work_order_id=WORK_ORDER,
        repository=REPOSITORY,
        branch=BRANCH,
        base_revision=BASE,
        destination="main",
        scope=("src", "tests/publication"),
        source_revision=SourceRevision(BASE),
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(hours=1),
    )
    defaults.update(overrides)
    return PublicationGrant(**defaults)  # type: ignore[arg-type]


def make_change(**overrides: object) -> PreparedChange:
    values: dict[str, object] = dict(
        source_revision=BASE,
        base_revision=BASE,
        patch_digest=PATCH_DIGEST,
        branch_revision=BRANCH_REVISION,
        work_order_id=str(WORK_ORDER),
        review_branch=BRANCH,
        outcome=ImplementationOutcome.VERIFIED,
    )
    values.update(overrides)
    source_revision = str(values["source_revision"])
    base_revision = str(values["base_revision"])
    patch_digest = str(values["patch_digest"])
    branch_revision = str(values["branch_revision"])
    verification = ImplementationVerification(
        outcome=values["outcome"],  # type: ignore[arg-type]
        source_revision=source_revision,
        base_revision=base_revision,
        patch_digest=patch_digest,
        branch_revision=branch_revision,
        checks=("patch_digest_matches", "patch_applied", "review_branch_created"),
    )
    return PreparedChange(
        contract_id="tactus.source-control.preparation/v1",
        contract_version="1.0.0",
        change_id="change-1",
        prepared_change_id="chg-" + hashlib.sha256(patch_digest.encode()).hexdigest()[:32],
        work_order_id=str(values["work_order_id"]),
        repository="/tmp/tactus-test-repository",
        source_revision=source_revision,
        base_revision=base_revision,
        patch_digest=patch_digest,
        patch_bytes=14,
        review_branch=str(values["review_branch"]),
        branch_revision=branch_revision,
        workspace_path="/tmp/tactus-test-workspace",
        workspace_present=True,
        implementation=verification,
        publication=PublicationEligibility.NOT_ELIGIBLE,
        publication_reason="verified implementation is not publication authority",
        prepared_at=NOW,
    )


@pytest.fixture()
def transport() -> FakeGitHubTransport:
    fake = FakeGitHubTransport()
    fake.set_base_revision(REPOSITORY, "main", BASE)
    return fake


@pytest.fixture()
def credentials() -> FakeCredentialProvider:
    return FakeCredentialProvider()


@pytest.fixture()
def publisher(
    transport: FakeGitHubTransport,
    credentials: FakeCredentialProvider,
    tmp_path: Path,
) -> GitHubPublicationPort:
    return GitHubPublicationPort(
        transport,
        credentials,
        state_root=tmp_path / "state",
        clock=lambda: NOW,
    )


def publish(
    publisher: GitHubPublicationPort,
    request: PublicationRequest | None = None,
    grant: PublicationGrant | None = None,
    change: PreparedChange | None = None,
):
    return publisher.publish(
        request or make_request(),
        grant or make_grant(),
        change or make_change(),
        at=NOW,
    )


def operation_kinds(transport: FakeGitHubTransport) -> list[str]:
    return [operation.kind for operation in transport.operations]


def read_record(publisher: GitHubPublicationPort, publication_id: str) -> dict[str, object]:
    path = publisher.state_root / ".tactus" / "publications" / "records" / (
        publication_id + ".json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_first_publication_records_branch_and_pr_durably(
    publisher: GitHubPublicationPort,
    transport: FakeGitHubTransport,
    credentials: FakeCredentialProvider,
) -> None:
    request = make_request()
    change = make_change()
    before = change.as_dict()

    result = publish(publisher, request, make_grant(), change)

    assert result.publication_status is PublicationStatus.PUBLISHED
    assert result.implementation_status is ImplementationStatus.VERIFIED
    assert result.publication_succeeded
    assert result.branch == BRANCH
    assert result.branch_revision == BRANCH_REVISION
    assert result.pull_request_number == 1
    assert result.pull_request_url == (
        f"https://github.invalid/{REPOSITORY}/pull/1"
    )
    assert result.network_calls == 0
    assert operation_kinds(transport) == ["ensure_branch", "create_pull_request"]
    assert credentials.calls == 1
    assert len(transport.pull_requests) == 1

    record = read_record(publisher, request.publication_id)
    assert record["branch"] == BRANCH
    assert record["branch_revision"] == BRANCH_REVISION
    assert record["pull_request_url"] == result.pull_request_url
    assert record["patch_digest"] == PATCH_DIGEST
    assert record["publication_status"] == "PUBLISHED"
    # The record names the grant that authorized this pull request, so an audit
    # can answer "which grant" rather than only "a PR exists".
    assert record["grant_id"] == "grant-publication-1"
    assert record["authority"] == "operator"
    assert record["grant_issued_at"] == (NOW - timedelta(minutes=5)).isoformat()
    assert record["grant_expires_at"] == (NOW + timedelta(hours=1)).isoformat()

    attempts = list(
        (publisher.state_root / ".tactus" / "publications" / "attempts").glob("*.json")
    )
    assert len(attempts) == 1
    # Publication never mutates the verified implementation evidence.
    assert change.as_dict() == before
    assert change.implementation_verified


def test_repeated_submission_reuses_the_same_identity(
    publisher: GitHubPublicationPort,
    transport: FakeGitHubTransport,
    credentials: FakeCredentialProvider,
    tmp_path: Path,
) -> None:
    first = publish(publisher)
    mutations_after_first = len(transport.operations)

    # A fresh publisher instance proves the identity is durable, not in-process.
    second_publisher = GitHubPublicationPort(
        transport,
        credentials,
        state_root=tmp_path / "state",
        clock=lambda: NOW,
    )
    second = publish(second_publisher)

    assert second.publication_status is PublicationStatus.REUSED
    assert second.publication_succeeded
    assert second.pull_request_url == first.pull_request_url
    assert second.pull_request_number == first.pull_request_number
    assert second.branch == first.branch
    assert second.branch_revision == first.branch_revision
    assert second.remote_operations == ()
    assert len(transport.operations) == mutations_after_first
    assert credentials.calls == 1
    assert len(transport.pull_requests) == 1


def test_changed_digest_refuses_without_republishing(
    publisher: GitHubPublicationPort,
    transport: FakeGitHubTransport,
    credentials: FakeCredentialProvider,
) -> None:
    first = publish(publisher)
    mutations_after_first = len(transport.operations)

    changed = make_change(patch_digest=OTHER_DIGEST)
    second = publish(publisher, make_request(), make_grant(), changed)

    assert first.publication_succeeded
    assert second.publication_status is PublicationStatus.REFUSED
    assert second.implementation_status is ImplementationStatus.VERIFIED
    assert second.error_code == PublicationErrorCode.DIGEST_CONFLICT.value
    assert not second.publication_succeeded
    assert len(transport.operations) == mutations_after_first
    assert credentials.calls == 1
    assert len(transport.pull_requests) == 1


def test_stale_source_refuses_before_publication(
    publisher: GitHubPublicationPort,
    transport: FakeGitHubTransport,
    credentials: FakeCredentialProvider,
) -> None:
    change = make_change(source_revision=OTHER_BASE)

    result = publish(publisher, make_request(), make_grant(), change)

    assert result.publication_status is PublicationStatus.REFUSED
    assert result.implementation_status is ImplementationStatus.VERIFIED
    assert result.error_code == PublicationErrorCode.STALE_SOURCE.value
    assert transport.operations == ()
    assert credentials.calls == 0


def test_revoked_grant_refuses_without_credential_or_transport(
    publisher: GitHubPublicationPort,
    transport: FakeGitHubTransport,
    credentials: FakeCredentialProvider,
) -> None:
    grant = make_grant(revoked_at=NOW - timedelta(minutes=1))

    result = publish(publisher, make_request(), grant)

    assert result.publication_status is PublicationStatus.REFUSED
    assert result.error_code == "REVOKED"
    assert result.implementation_status is ImplementationStatus.VERIFIED
    assert transport.operations == ()
    assert credentials.calls == 0


def test_permission_failure_keeps_verified_patch_evidence(
    publisher: GitHubPublicationPort,
    transport: FakeGitHubTransport,
    credentials: FakeCredentialProvider,
) -> None:
    denied = FakeGitHubTransport(permission_denied=True)
    denied.set_base_revision(REPOSITORY, "main", BASE)
    publisher = GitHubPublicationPort(
        denied,
        credentials,
        state_root=publisher.state_root,
        clock=lambda: NOW,
    )
    change = make_change()
    before = change.as_dict()

    result = publish(publisher, make_request(), make_grant(), change)

    assert result.publication_status is PublicationStatus.FAILED
    assert result.implementation_status is ImplementationStatus.VERIFIED
    assert result.error_code == PublicationErrorCode.PERMISSION_DENIED.value
    assert not result.publication_succeeded
    assert denied.pull_requests == ()
    assert denied.network_calls == 0
    assert change.as_dict() == before
    assert change.implementation_verified
    assert not hasattr(publisher, "restart")


def test_base_moved_refuses_before_credential_resolution(
    publisher: GitHubPublicationPort,
    transport: FakeGitHubTransport,
    credentials: FakeCredentialProvider,
) -> None:
    transport.set_base_revision(REPOSITORY, "main", OTHER_BASE)

    result = publish(publisher)

    assert result.publication_status is PublicationStatus.FAILED
    assert result.error_code == PublicationErrorCode.BASE_MOVED.value
    assert result.implementation_status is ImplementationStatus.VERIFIED
    assert transport.operations == ()
    assert credentials.calls == 0
    assert transport.pull_requests == ()


def test_duplicate_pull_request_refuses_before_credential_resolution(
    publisher: GitHubPublicationPort,
    transport: FakeGitHubTransport,
    credentials: FakeCredentialProvider,
) -> None:
    transport.seed_branch(REPOSITORY, BRANCH, BRANCH_REVISION)
    transport.seed_pull_request(
        REPOSITORY,
        head_branch=BRANCH,
        base_branch="main",
        head_revision=BRANCH_REVISION,
        base_revision=BASE,
        publication_id="pub-other-1",
        number=10,
    )
    transport.seed_pull_request(
        REPOSITORY,
        head_branch=BRANCH,
        base_branch="main",
        head_revision=BRANCH_REVISION,
        base_revision=BASE,
        publication_id="pub-other-2",
        number=11,
    )

    result = publish(publisher)

    assert result.publication_status is PublicationStatus.FAILED
    assert result.error_code == PublicationErrorCode.DUPLICATE_PULL_REQUEST.value
    assert result.implementation_status is ImplementationStatus.VERIFIED
    assert transport.operations == ()
    assert credentials.calls == 0
    assert len(transport.pull_requests) == 2


def test_lost_acknowledgment_reconciles_exactly_one_pull_request(
    publisher: GitHubPublicationPort,
    credentials: FakeCredentialProvider,
    tmp_path: Path,
) -> None:
    transport = FakeGitHubTransport(lose_next_acknowledgment=True)
    transport.set_base_revision(REPOSITORY, "main", BASE)
    publisher = GitHubPublicationPort(
        transport,
        credentials,
        state_root=tmp_path / "lost-ack-state",
        clock=lambda: NOW,
    )

    result = publish(publisher)

    assert result.publication_status is PublicationStatus.RECONCILED
    assert result.publication_succeeded
    assert result.pull_request_url == transport.pull_requests[0].url
    assert len(transport.pull_requests) == 1
    assert operation_kinds(transport) == ["ensure_branch", "create_pull_request"]
    assert credentials.calls == 1

    record = read_record(publisher, make_request().publication_id)
    assert record["pull_request_url"] == result.pull_request_url
    assert record["publication_status"] == "RECONCILED"

    repeated = publish(publisher)
    assert repeated.publication_status is PublicationStatus.REUSED
    assert repeated.pull_request_url == result.pull_request_url
    assert len(transport.pull_requests) == 1


def test_lost_acknowledgment_without_reconciliation_fails_closed(
    publisher: GitHubPublicationPort,
    credentials: FakeCredentialProvider,
    tmp_path: Path,
) -> None:
    class AmbiguousTransport(FakeGitHubTransport):
        def create_pull_request(self, repository: str, **kwargs: object):  # type: ignore[override]
            from tactus.integrations.github import GitHubAcknowledgmentLostError

            self._record("create_pull_request", repository, str(kwargs["head_branch"]))
            raise GitHubAcknowledgmentLostError(None)

    transport = AmbiguousTransport()
    transport.set_base_revision(REPOSITORY, "main", BASE)
    publisher = GitHubPublicationPort(
        transport,
        credentials,
        state_root=tmp_path / "ambiguous-state",
        clock=lambda: NOW,
    )

    result = publish(publisher)

    assert result.publication_status is PublicationStatus.FAILED
    assert result.error_code == PublicationErrorCode.ACKNOWLEDGMENT_LOST.value
    assert result.implementation_status is ImplementationStatus.VERIFIED
    assert transport.pull_requests == ()


def test_fake_transport_has_no_network_path(
    publisher: GitHubPublicationPort,
    transport: FakeGitHubTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("publication attempted a network operation")

    monkeypatch.setattr(socket, "socket", refuse_network)
    monkeypatch.setattr(socket, "create_connection", refuse_network)

    result = publish(publisher)

    assert result.publication_succeeded
    assert result.network_calls == 0
    assert transport.network_calls == 0
    assert transport.contract.live is False
    assert transport.contract.network is False
    assert publisher.contract.live is False
    assert publisher.contract.network is False
    assert set(operation_kinds(transport)) == {
        "ensure_branch",
        "create_pull_request",
    }


def test_result_separates_statuses_and_stays_redacted(
    publisher: GitHubPublicationPort,
) -> None:
    result = publish(publisher)
    payload = result.as_dict()

    assert payload["implementation_status"] == "VERIFIED"
    assert payload["publication_status"] == "PUBLISHED"
    assert payload["implementation_verified"] is True
    assert payload["publication_succeeded"] is True
    assert payload["network_calls"] == 0
    assert payload["redacted"] is True
    assert "fake-publication-credential" not in json.dumps(payload)
    assert publisher.contract.separate_statuses is True
