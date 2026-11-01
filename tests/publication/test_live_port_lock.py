"""The mocked port stays locked; the live port is a separate, explicit class.

These tests pin the single most important property of the publication boundary:
no configuration, environment value or constructor argument can turn the mocked
port into a live one. A live publication is a different class, named in review.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tactus.domain import SourceRevision, WorkOrderId
from tactus.integrations.github import FakeGitHubTransport
from tactus.integrations.github.transport import GitHubTransportContract
from tactus.integrations.stax import (
    ImplementationOutcome,
    ImplementationVerification,
    PreparedChange,
    PublicationEligibility,
)
from tactus.publication import (
    GitHubLivePublicationPort,
    GitHubPublicationPort,
    PublicationCredential,
    PublicationGrant,
    PublicationRequest,
    PublicationStateError,
    PublicationStatus,
)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
WORK_ORDER = WorkOrderId("TCT-WO-023B2-live-publication")
REPOSITORY = "Walsamer/tactus"
BRANCH = "agent/tactus/TCT-WO-023B2-live-publication"
BASE = "a" * 40
BRANCH_REVISION = "b" * 40
PATCH_DIGEST = hashlib.sha256(b"verified patch").hexdigest()


class CallOrderingTransport(FakeGitHubTransport):
    """A fake that records the order of binding versus reading.

    It declares a live, credentialed-read contract so the live port can be
    exercised offline. The underlying behaviour is still the deterministic fake.
    """

    def __init__(self, *, reads_require_credential: bool) -> None:
        super().__init__()
        self.order: list[str] = []
        self._contract = GitHubTransportContract(
            contract_id="test.ordering/v1",
            version="1.0.0",
            transport="ordering",
            live=True,
            network=True,
            reads_require_credential=reads_require_credential,
        )

    def bind_credential(self, credential: PublicationCredential) -> None:
        self.order.append("bind_credential")
        super().bind_credential(credential)

    def base_revision(self, repository: str, branch: str) -> str | None:
        self.order.append("read")
        return super().base_revision(repository, branch)

    def branch_revision(self, repository: str, branch: str) -> str | None:
        self.order.append("read")
        return super().branch_revision(repository, branch)

    def find_pull_requests(self, repository: str, **kwargs: object):
        self.order.append("read")
        return super().find_pull_requests(repository, **kwargs)  # type: ignore[arg-type]


class CountingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, grant, request, *, at):
        self.calls += 1
        return PublicationCredential("test-credential", provider_id="test")


def make_request() -> PublicationRequest:
    return PublicationRequest(
        intent_id="intent-live-lock",
        work_order_id=WORK_ORDER,
        repository=REPOSITORY,
        branch=BRANCH,
        base_revision=BASE,
        destination="main",
        scope=("src/publication",),
        source_revision=SourceRevision(BASE),
    )


def make_grant() -> PublicationGrant:
    return PublicationGrant(
        grant_id="grant-publication-1",
        authority="operator",
        work_order_id=WORK_ORDER,
        repository=REPOSITORY,
        branch=BRANCH,
        base_revision=BASE,
        destination="main",
        scope=("src/publication",),
        source_revision=SourceRevision(BASE),
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(hours=1),
    )


def make_change() -> PreparedChange:
    verification = ImplementationVerification(
        outcome=ImplementationOutcome.VERIFIED,
        source_revision=BASE,
        base_revision=BASE,
        patch_digest=PATCH_DIGEST,
        branch_revision=BRANCH_REVISION,
        checks=("patch_digest_matches", "patch_applied", "review_branch_created"),
    )
    return PreparedChange(
        contract_id="tactus.source-control.preparation/v1",
        contract_version="1.0.0",
        change_id="change-1",
        prepared_change_id="chg-" + hashlib.sha256(PATCH_DIGEST.encode()).hexdigest()[:32],
        work_order_id=str(WORK_ORDER),
        repository="/tmp/tactus-test-repository",
        source_revision=BASE,
        base_revision=BASE,
        patch_digest=PATCH_DIGEST,
        patch_bytes=14,
        review_branch=BRANCH,
        branch_revision=BRANCH_REVISION,
        workspace_path="/tmp/tactus-test-workspace",
        workspace_present=True,
        implementation=verification,
        publication=PublicationEligibility.NOT_ELIGIBLE,
        publication_reason="verified implementation is not publication authority",
        prepared_at=NOW,
    )


def test_the_mocked_port_refuses_a_live_networked_transport(tmp_path: Path) -> None:
    with pytest.raises(PublicationStateError) as error:
        GitHubPublicationPort(
            CallOrderingTransport(reads_require_credential=True),
            CountingProvider(),
            state_root=tmp_path,
        )

    assert "non-live" in str(error.value)


def test_the_live_port_refuses_the_mock_transport(tmp_path: Path) -> None:
    with pytest.raises(PublicationStateError) as error:
        GitHubLivePublicationPort(
            FakeGitHubTransport(), CountingProvider(), state_root=tmp_path
        )

    assert "live" in str(error.value)


def test_the_live_port_resolves_and_binds_the_credential_before_any_read(
    tmp_path: Path,
) -> None:
    transport = CallOrderingTransport(reads_require_credential=True)
    transport.set_base_revision(REPOSITORY, "main", BASE)
    credentials = CountingProvider()
    port = GitHubLivePublicationPort(transport, credentials, state_root=tmp_path)

    result = port.publish(
        make_request(), make_grant(), make_change(), at=NOW
    )

    assert result.publication_status is PublicationStatus.PUBLISHED
    assert credentials.calls == 1
    assert transport.order[0] == "bind_credential"
    assert "read" in transport.order[1:]


def test_the_mocked_port_reads_before_it_resolves_any_credential(tmp_path: Path) -> None:
    transport = CallOrderingTransport(reads_require_credential=False)
    transport._contract = GitHubTransportContract(
        contract_id="test.ordering/v1",
        version="1.0.0",
        transport="fake",
        live=False,
        network=False,
        reads_require_credential=False,
    )
    transport.set_base_revision(REPOSITORY, "main", BASE)
    credentials = CountingProvider()
    port = GitHubPublicationPort(transport, credentials, state_root=tmp_path)

    result = port.publish(make_request(), make_grant(), make_change(), at=NOW)

    assert result.publication_status is PublicationStatus.PUBLISHED
    assert credentials.calls == 1
    # The mocked port keeps the stricter order: local checks first, credential
    # only once publication is certain.
    assert transport.order[0] == "read"
    assert transport.order.index("bind_credential") > 0


def test_a_live_port_records_the_contract_it_served(tmp_path: Path) -> None:
    transport = CallOrderingTransport(reads_require_credential=True)
    port = GitHubLivePublicationPort(transport, CountingProvider(), state_root=tmp_path)

    assert port.contract.live
    assert port.contract.network
    assert port.contract.transport == "gh"
    assert port.contract.contract_id.endswith("/v1")


def test_a_failed_read_is_a_recorded_result_not_an_exception(tmp_path: Path) -> None:
    """A live read can fail (401, rate limit, 5xx). That must not crash or vanish."""

    from tactus.integrations.github.transport import GitHubTransportError

    class FailingReadTransport(CallOrderingTransport):
        def base_revision(self, repository: str, branch: str) -> str | None:
            raise GitHubTransportError("gh: Bad credentials (HTTP 401)")

    transport = FailingReadTransport(reads_require_credential=True)
    credentials = CountingProvider()
    port = GitHubLivePublicationPort(transport, credentials, state_root=tmp_path)

    result = port.publish(make_request(), make_grant(), make_change(), at=NOW)

    assert result.publication_status is PublicationStatus.FAILED
    assert result.error_code == "transport_failed"
    assert result.implementation_verified
    attempts = list(
        (tmp_path / ".tactus" / "publications" / "attempts").glob("*.json")
    )
    assert len(attempts) == 1
    assert port.evidence(result.publication_id) is None
