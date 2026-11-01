"""Human grant -> publication grant projection: pure, bounded, fail-closed."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from tactus.domain import SourceRevision, WorkOrderId
from tactus.domain.human_intervention import HumanAction, HumanGrant
from tactus.integrations.stax import (
    ImplementationOutcome,
    ImplementationVerification,
    PreparedChange,
    PublicationEligibility,
)
from tactus.publication import (
    EmptyGrantScopeError,
    GrantProjectionError,
    ProtectedHeadBranchError,
    PublicationActionRequiredError,
    PublicationEffect,
    PublicationGrant,
    PublicationRequest,
    StaleGrantBindingError,
    publication_grant_from_human_grant,
    validate_publication_grant,
)

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
WORK_ORDER = WorkOrderId("TCT-WO-023B2-live-publication")
REPOSITORY = "Walsamer/tactus"
BRANCH = "agent/tactus/TCT-WO-023B2-live-publication"
BASE = "a" * 40
BRANCH_REVISION = "b" * 40
PATCH_DIGEST = hashlib.sha256(b"verified patch").hexdigest()
SCOPE = ("src/publication", "tests/publication")


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
    verification = ImplementationVerification(
        outcome=values["outcome"],  # type: ignore[arg-type]
        source_revision=str(values["source_revision"]),
        base_revision=str(values["base_revision"]),
        patch_digest=str(values["patch_digest"]),
        branch_revision=str(values["branch_revision"]),
        checks=("patch_digest_matches", "patch_applied", "review_branch_created"),
    )
    return PreparedChange(
        contract_id="tactus.source-control.preparation/v1",
        contract_version="1.0.0",
        change_id="change-1",
        prepared_change_id="chg-" + hashlib.sha256(PATCH_DIGEST.encode()).hexdigest()[:32],
        work_order_id=str(values["work_order_id"]),
        repository="/tmp/tactus-test-repository",
        source_revision=str(values["source_revision"]),
        base_revision=str(values["base_revision"]),
        patch_digest=str(values["patch_digest"]),
        patch_bytes=14,
        review_branch=str(values["review_branch"]),
        branch_revision=str(values["branch_revision"]),
        workspace_path="/tmp/tactus-test-workspace",
        workspace_present=True,
        implementation=verification,
        publication=PublicationEligibility.NOT_ELIGIBLE,
        publication_reason="verified implementation is not publication authority",
        prepared_at=NOW,
    )


def make_grant(**overrides: object) -> HumanGrant:
    defaults: dict[str, object] = dict(
        grant_id="grant-human-1",
        resolution_id="resolution-1",
        intervention_id="intervention-1",
        work_order_id=WORK_ORDER,
        actor="Walsamer",
        action=HumanAction.PUBLISH,
        scope=SCOPE,
        source_revision=SourceRevision(BASE),
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(hours=1),
    )
    defaults.update(overrides)
    return HumanGrant(**defaults)  # type: ignore[arg-type]


def project(**overrides: object) -> PublicationGrant:
    grant = overrides.pop("grant", None) or make_grant()
    change = overrides.pop("change", None) or make_change()
    options: dict[str, object] = dict(repository=REPOSITORY, destination="main")
    options.update(overrides)
    return publication_grant_from_human_grant(grant, change, **options)  # type: ignore[arg-type]


def request_for(grant: PublicationGrant, change: PreparedChange) -> PublicationRequest:
    return PublicationRequest(
        intent_id="intent-live-publication-1",
        work_order_id=grant.work_order_id,
        repository=grant.repository,
        branch=grant.branch,
        base_revision=grant.base_revision,
        destination=grant.destination,
        scope=grant.scope,
        source_revision=grant.source_revision,
        effect=grant.effect,
    )


def test_projection_binds_the_prepared_change_and_the_guard_accepts_it() -> None:
    change = make_change()
    grant = project(change=change)

    assert grant.grant_id == "grant-human-1"
    assert grant.authority == "operator"
    assert grant.branch == BRANCH
    assert grant.base_revision == change.base_revision
    assert grant.source_revision.value == change.source_revision
    assert grant.scope == ("src/publication", "tests/publication")
    assert grant.effect is PublicationEffect.OPEN_PULL_REQUEST
    assert grant.issued_at == NOW - timedelta(minutes=5)
    assert grant.expires_at == NOW + timedelta(hours=1)

    authorization = validate_publication_grant(
        request_for(grant, change), grant, at=NOW
    )
    assert authorization.allowed
    assert authorization.publication_id.startswith("pub-")


def test_projection_refuses_an_action_that_does_not_authorize_publication() -> None:
    with pytest.raises(PublicationActionRequiredError):
        project(grant=make_grant(action=HumanAction.APPROVE))
    with pytest.raises(PublicationActionRequiredError):
        project(grant=make_grant(action=HumanAction.AUTHORIZE_ROUTE_CHANGE))


def test_projection_refuses_a_stale_source_revision_binding() -> None:
    with pytest.raises(StaleGrantBindingError):
        project(grant=make_grant(source_revision=SourceRevision("d" * 40)))


def test_projection_refuses_a_grant_recorded_for_another_work_order() -> None:
    with pytest.raises(StaleGrantBindingError):
        project(grant=make_grant(work_order_id=WorkOrderId("TCT-WO-OTHER")))


def test_projection_refuses_the_trunk_as_a_head_branch() -> None:
    for trunk in ("main", "master", "Main"):
        with pytest.raises(ProtectedHeadBranchError):
            project(change=make_change(review_branch=trunk))


def test_projection_refuses_an_empty_scope() -> None:
    with pytest.raises(EmptyGrantScopeError):
        project(grant=make_grant(scope=()))


def test_projection_refuses_a_change_that_is_not_a_prepared_change() -> None:
    with pytest.raises(GrantProjectionError):
        project(change=object())  # type: ignore[arg-type]
    with pytest.raises(GrantProjectionError):
        project(grant=object())  # type: ignore[arg-type]


def test_a_revoked_grant_still_projects_so_the_guard_can_record_the_refusal() -> None:
    grant = project(grant=make_grant(revoked_at=NOW - timedelta(minutes=1)))
    change = make_change()

    authorization = validate_publication_grant(request_for(grant, change), grant, at=NOW)

    assert not authorization.allowed
    assert authorization.denial_code is not None
    assert authorization.denial_code.value == "REVOKED"
