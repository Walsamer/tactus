"""Publication guard: authorization ordering, one-shot credentials and redaction."""

from __future__ import annotations

import json
import logging
import pickle
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from tactus.domain import SourceRevision, WorkOrderId
from tactus.publication import (
    PublicationAuthorityError,
    PublicationBindingError,
    PublicationCredential,
    PublicationCredentialError,
    PublicationDenialCode,
    PublicationEffect,
    PublicationEvidence,
    PublicationExpiredError,
    PublicationGrant,
    PublicationGuard,
    PublicationIdentityConflictError,
    PublicationNotYetValidError,
    PublicationReceipt,
    PublicationRequest,
    PublicationRevokedError,
    PublicationScopeError,
    PublicationTransportError,
)

NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
BASE = "a" * 40
OTHER_BASE = "b" * 40
SECRET = "SENTINEL-credential-material-must-never-leak"


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


class RecordingCredentialProvider:
    def __init__(self, material: str = SECRET) -> None:
        self.material = material
        self.calls: list[tuple[PublicationGrant, PublicationRequest, datetime]] = []

    def resolve(
        self,
        grant: PublicationGrant,
        request: PublicationRequest,
        *,
        at: datetime,
    ) -> PublicationCredential:
        self.calls.append((grant, request, at))
        return PublicationCredential(self.material, provider_id="mock-credential-store")


class SlowRecordingCredentialProvider(RecordingCredentialProvider):
    def resolve(
        self,
        grant: PublicationGrant,
        request: PublicationRequest,
        *,
        at: datetime,
    ) -> PublicationCredential:
        time.sleep(0.05)
        return super().resolve(grant, request, at=at)


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[PublicationRequest, PublicationCredential, str, datetime]] = []

    def publish(
        self,
        request: PublicationRequest,
        credential: PublicationCredential,
        *,
        publication_id: str,
        at: datetime,
    ) -> PublicationReceipt:
        self.calls.append((request, credential, publication_id, at))
        return PublicationReceipt(
            external_ref=f"mock://publication/{publication_id}", published_at=at
        )


def guard(
    credentials: object | None = None,
    transport: object | None = None,
    **kwargs: Any,
) -> PublicationGuard:
    return PublicationGuard(
        credentials if credentials is not None else RecordingCredentialProvider(),
        transport if transport is not None else RecordingTransport(),
        **kwargs,
    )


def test_authorized_publication_resolves_one_credential_and_publishes_once() -> None:
    credentials = RecordingCredentialProvider()
    transport = RecordingTransport()
    guard_instance = guard(credentials, transport)

    evidence = guard_instance.publish(make_request(), make_grant(), at=NOW)

    assert isinstance(evidence, PublicationEvidence)
    assert len(credentials.calls) == 1
    assert len(transport.calls) == 1
    assert credentials.calls[0][0].grant_id == "grant-1"
    assert credentials.calls[0][1].intent_id == "intent-1"
    assert credentials.calls[0][2] == NOW
    assert transport.calls[0][2] == evidence.publication_id
    assert evidence.publication_id == make_request().publication_id
    assert evidence.external_ref == f"mock://publication/{evidence.publication_id}"
    assert evidence.as_dict()["redacted"] is True


def test_evaluate_is_pure_and_never_accesses_credentials_or_transport() -> None:
    credentials = RecordingCredentialProvider()
    transport = RecordingTransport()
    guard_instance = guard(credentials, transport)

    authorization = guard_instance.evaluate(make_request(), make_grant(), at=NOW)

    assert authorization.allowed is True
    assert authorization.publication_id == make_request().publication_id
    assert credentials.calls == []
    assert transport.calls == []


def test_repeated_same_intent_preserves_identity_and_one_effect() -> None:
    credentials = RecordingCredentialProvider()
    transport = RecordingTransport()
    guard_instance = guard(credentials, transport)
    request = make_request()
    grant = make_grant()

    first = guard_instance.publish(request, grant, at=NOW)
    second = guard_instance.publish(request, grant, at=NOW + timedelta(minutes=1))

    assert first is second
    assert first.publication_id == second.publication_id
    assert len(credentials.calls) == 1
    assert len(transport.calls) == 1
    assert guard_instance.evidence(first.publication_id) is first


def test_concurrent_same_intent_resolves_one_credential_and_publishes_once() -> None:
    credentials = SlowRecordingCredentialProvider()
    transport = RecordingTransport()
    guard_instance = guard(credentials, transport)
    request = make_request()
    grant = make_grant()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(guard_instance.publish, request, grant, at=NOW) for _ in range(2)
        ]
        evidence = [future.result(timeout=5) for future in futures]

    assert evidence[0] is evidence[1]
    assert len(credentials.calls) == 1
    assert len(transport.calls) == 1


def test_publication_identity_is_stable_across_fresh_guard_instances() -> None:
    request = make_request()
    grant = make_grant()
    first_guard = guard()
    second_guard = guard()

    first = first_guard.publish(request, grant, at=NOW)
    second = second_guard.publish(request, grant, at=NOW)

    assert first.publication_id == second.publication_id
    assert first.intent_digest == second.intent_digest


def test_reused_intent_id_with_changed_content_fails_closed() -> None:
    credentials = RecordingCredentialProvider()
    transport = RecordingTransport()
    guard_instance = guard(credentials, transport)

    first = guard_instance.publish(make_request(), make_grant(), at=NOW)
    changed_request = make_request(branch="agent/tactus/other")
    changed_grant = make_grant(branch="agent/tactus/other")

    with pytest.raises(PublicationIdentityConflictError):
        guard_instance.publish(changed_request, changed_grant, at=NOW)

    assert len(credentials.calls) == 1
    assert len(transport.calls) == 1
    assert guard_instance.evidence(first.publication_id) is first
    assert changed_request.publication_id != first.publication_id


@pytest.mark.parametrize(
    "overrides",
    [
        {"work_order_id": WorkOrderId("WO-2")},
        {"repository": "Walsamer/other"},
        {"branch": "agent/tactus/other"},
        {"base_revision": OTHER_BASE},
        {"destination": "release"},
        {"source_revision": SourceRevision("src-rev-2")},
        {"effect": PublicationEffect.PUBLISH_REVIEW_BRANCH},
    ],
    ids=[
        "work-order",
        "repository",
        "branch",
        "base-revision",
        "destination",
        "source-revision",
        "effect",
    ],
)
def test_each_binding_mismatch_is_denied_before_credentials(overrides: dict[str, object]) -> None:
    credentials = RecordingCredentialProvider()
    transport = RecordingTransport()
    guard_instance = guard(credentials, transport)
    request = make_request()
    grant = make_grant(**overrides)

    authorization = guard_instance.evaluate(request, grant, at=NOW)
    assert authorization.allowed is False
    assert authorization.denial_code is PublicationDenialCode.BINDING_MISMATCH

    with pytest.raises(PublicationBindingError):
        guard_instance.authorize(request, grant, at=NOW)
    with pytest.raises(PublicationBindingError):
        guard_instance.publish(request, grant, at=NOW)

    assert credentials.calls == []
    assert transport.calls == []


def test_untrusted_authority_never_accesses_a_credential() -> None:
    credentials = RecordingCredentialProvider()
    transport = RecordingTransport()
    guard_instance = guard(credentials, transport)
    request = make_request()
    grant = make_grant(authority="coding-agent")

    authorization = guard_instance.evaluate(request, grant, at=NOW)
    assert authorization.allowed is False
    assert authorization.denial_code is PublicationDenialCode.UNTRUSTED_AUTHORITY

    with pytest.raises(PublicationAuthorityError):
        guard_instance.publish(request, grant, at=NOW)
    assert credentials.calls == []
    assert transport.calls == []


def test_scope_widening_is_denied_before_credentials() -> None:
    credentials = RecordingCredentialProvider()
    transport = RecordingTransport()
    guard_instance = guard(credentials, transport)
    request = make_request(scope=("docs/architecture/publication.md",))
    grant = make_grant(scope=("src",))

    authorization = guard_instance.evaluate(request, grant, at=NOW)
    assert authorization.allowed is False
    assert authorization.denial_code is PublicationDenialCode.SCOPE_NOT_AUTHORIZED

    with pytest.raises(PublicationScopeError):
        guard_instance.publish(request, grant, at=NOW)
    assert credentials.calls == []
    assert transport.calls == []


def test_empty_request_scope_is_denied_before_credentials() -> None:
    credentials = RecordingCredentialProvider()
    transport = RecordingTransport()
    guard_instance = guard(credentials, transport)
    request = make_request(scope=())
    grant = make_grant(scope=("src",))

    authorization = guard_instance.evaluate(request, grant, at=NOW)
    assert authorization.allowed is False
    assert authorization.denial_code is PublicationDenialCode.SCOPE_NOT_AUTHORIZED

    with pytest.raises(PublicationScopeError):
        guard_instance.publish(request, grant, at=NOW)
    assert credentials.calls == []
    assert transport.calls == []


@pytest.mark.parametrize(
    "grant_overrides,expected_error",
    [
        ({"expires_at": NOW}, PublicationExpiredError),
        ({"revoked_at": NOW - timedelta(seconds=1)}, PublicationRevokedError),
        ({"issued_at": NOW + timedelta(seconds=1)}, PublicationNotYetValidError),
    ],
    ids=["expired", "revoked", "not-yet-valid"],
)
def test_stale_grants_never_access_a_credential(
    grant_overrides: dict[str, object],
    expected_error: type[Exception],
) -> None:
    credentials = RecordingCredentialProvider()
    transport = RecordingTransport()
    guard_instance = guard(credentials, transport)
    request = make_request()
    grant = make_grant(**grant_overrides)

    with pytest.raises(expected_error):
        guard_instance.publish(request, grant, at=NOW)

    assert credentials.calls == []
    assert transport.calls == []


def test_expired_grant_is_denied_at_the_exact_expiry_instant() -> None:
    guard_instance = guard()
    request = make_request()
    grant = make_grant(expires_at=NOW)

    authorization = guard_instance.evaluate(request, grant, at=NOW)

    assert authorization.allowed is False
    assert authorization.denial_code is PublicationDenialCode.EXPIRED


def test_credential_provider_returning_raw_material_is_rejected_without_leaking() -> None:
    class RawProvider:
        def resolve(self, grant, request, *, at):  # type: ignore[no-untyped-def]
            return SECRET

    transport = RecordingTransport()
    guard_instance = guard(RawProvider(), transport)

    with pytest.raises(PublicationCredentialError) as excinfo:
        guard_instance.publish(make_request(), make_grant(), at=NOW)

    assert SECRET not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert transport.calls == []


def test_credential_provider_failure_is_wrapped_without_chaining_the_secret() -> None:
    class FailingProvider:
        def resolve(self, grant, request, *, at):  # type: ignore[no-untyped-def]
            raise RuntimeError(f"provider failed with {SECRET}")

    transport = RecordingTransport()
    guard_instance = guard(FailingProvider(), transport)

    with pytest.raises(PublicationCredentialError) as excinfo:
        guard_instance.publish(make_request(), make_grant(), at=NOW)

    assert SECRET not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert transport.calls == []


def test_transport_failure_is_typed_and_does_not_record_evidence() -> None:
    class FailingTransport:
        def publish(self, request, credential, *, publication_id, at):  # type: ignore[no-untyped-def]
            raise RuntimeError("mock transport failure")

    credentials = RecordingCredentialProvider()
    guard_instance = guard(credentials, FailingTransport())

    with pytest.raises(PublicationTransportError) as excinfo:
        guard_instance.publish(make_request(), make_grant(), at=NOW)

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert len(credentials.calls) == 1
    assert guard_instance.evidence(make_request().publication_id) is None


def test_returned_evidence_and_credential_repr_are_fully_redacted(caplog) -> None:
    credentials = RecordingCredentialProvider()
    transport = RecordingTransport()
    guard_instance = guard(credentials, transport)

    with caplog.at_level(logging.DEBUG):
        evidence = guard_instance.publish(make_request(), make_grant(), at=NOW)

    credential = transport.calls[0][1]
    serialized = json.dumps(evidence.as_dict(), sort_keys=True)

    assert SECRET not in repr(credential)
    assert SECRET not in str(credential)
    assert SECRET not in f"{credential}"
    assert SECRET not in repr(evidence)
    assert SECRET not in serialized
    assert SECRET not in caplog.text
    assert "credential" not in evidence.as_dict()
    assert not hasattr(evidence, "credential")
    assert credential == PublicationCredential(SECRET, provider_id="mock-credential-store")

    with pytest.raises(TypeError):
        pickle.dumps(credential)


def test_empty_trusted_authority_configuration_is_rejected() -> None:
    with pytest.raises(Exception):
        PublicationGuard(
            RecordingCredentialProvider(),
            RecordingTransport(),
            trusted_authorities=(),
        )
