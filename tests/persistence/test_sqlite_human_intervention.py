"""Durable human-intervention persistence tests (issue #11).

These tests pin restart safety and exactly-one-effect semantics for the SQLite
adapter: request/resolution persistence, duplicate replay, revocation, rejected
wrong/expired requests and concurrent-source-edit rejection.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from tactus.domain import (
    ExpiredRequestError,
    HumanAction,
    HumanInterventionRequest,
    HumanResolution,
    ImmutableRequestConflictError,
    ImmutableResolutionConflictError,
    InterventionReason,
    RequestAlreadyResolvedError,
    ScopeWideningError,
    SourceRevision,
    StaleResolutionBindingError,
    UnknownInterventionRequestError,
    WorkOrderId,
    WorkOrderRevision,
)
from tactus.persistence import SqliteHumanIntervention

NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=1)
SOURCE = SourceRevision("src-rev-1")
REVISION = WorkOrderRevision(7)


def make_request(**overrides: object) -> HumanInterventionRequest:
    defaults: dict[str, object] = dict(
        intervention_id="req-1",
        work_order_id=WorkOrderId("WO-1"),
        reason=InterventionReason.APPROVAL_REQUIRED,
        summary="approve the tricky migration",
        permitted_actions=(HumanAction.APPROVE, HumanAction.REJECT),
        scope=("src/**",),
        source_revision=SOURCE,
        work_order_revision=REVISION,
        created_at=NOW,
    )
    defaults.update(overrides)
    return HumanInterventionRequest(**defaults)  # type: ignore[arg-type]


def make_resolution(**overrides: object) -> HumanResolution:
    defaults: dict[str, object] = dict(
        resolution_id="res-1",
        intervention_id="req-1",
        work_order_id=WorkOrderId("WO-1"),
        actor="operator-1",
        action=HumanAction.APPROVE,
        resolved_at=NOW,
        scope=("src/**",),
        source_revision=SOURCE,
        work_order_revision=REVISION,
    )
    defaults.update(overrides)
    return HumanResolution(**defaults)  # type: ignore[arg-type]


def store(tmp_path, name: str = "human.sqlite") -> SqliteHumanIntervention:
    return SqliteHumanIntervention(tmp_path / name)


def resolve(adapter: SqliteHumanIntervention, resolution: HumanResolution):
    return adapter.resolve(
        resolution, current_source_revision=SOURCE, current_work_order_revision=REVISION
    )


# -- persistence and restart ------------------------------------------------


def test_request_resolution_and_grant_survive_restart(tmp_path) -> None:
    first = store(tmp_path)
    first.record_request(make_request())
    resolution, grant = resolve(first, make_resolution())
    first.close()

    restarted = store(tmp_path)
    assert restarted.request("req-1") == make_request()
    assert restarted.resolution("res-1") == resolution
    assert restarted.resolutions_for("req-1") == (resolution,)
    assert restarted.grants_for("WO-1") == (grant,)
    assert grant.actor == "operator-1"
    assert grant.scope == ("src/**",)
    assert grant.source_revision == SOURCE
    restarted.close()


def test_unknown_request_lookup_fails_closed(tmp_path) -> None:
    adapter = store(tmp_path)
    with pytest.raises(UnknownInterventionRequestError):
        adapter.request("missing")
    adapter.close()


def test_request_identity_replay_with_changed_content_is_rejected(tmp_path) -> None:
    adapter = store(tmp_path)
    adapter.record_request(make_request())
    with pytest.raises(ImmutableRequestConflictError):
        adapter.record_request(make_request(summary="changed"))
    # A byte-identical replay returns the original record.
    assert adapter.record_request(make_request()) == make_request()
    adapter.close()


# -- exactly one effect -----------------------------------------------------


def test_duplicate_resolution_replay_preserves_one_effect_across_restart(tmp_path) -> None:
    adapter = store(tmp_path)
    adapter.record_request(make_request())
    resolution, grant = resolve(adapter, make_resolution())
    _, replayed = resolve(adapter, make_resolution())
    assert replayed.grant_id == grant.grant_id
    adapter.close()

    restarted = store(tmp_path)
    rebuilt_resolution, rebuilt_grant = resolve(restarted, make_resolution())
    assert rebuilt_resolution == resolution
    assert rebuilt_grant.grant_id == grant.grant_id
    assert len(restarted.grants_for("WO-1")) == 1
    restarted.close()


def test_replayed_resolution_with_changed_content_is_rejected(tmp_path) -> None:
    adapter = store(tmp_path)
    adapter.record_request(make_request())
    resolve(adapter, make_resolution())
    with pytest.raises(ImmutableResolutionConflictError):
        resolve(adapter, make_resolution(actor="someone-else"))
    adapter.close()


def test_second_active_resolution_for_the_same_request_is_rejected(tmp_path) -> None:
    adapter = store(tmp_path)
    adapter.record_request(make_request())
    resolve(adapter, make_resolution())
    with pytest.raises(RequestAlreadyResolvedError):
        resolve(adapter, make_resolution(resolution_id="res-2"))
    assert len(adapter.grants_for("WO-1")) == 1
    adapter.close()


def test_concurrent_resolvers_produce_exactly_one_effective_grant(tmp_path) -> None:
    seed = store(tmp_path)
    seed.record_request(make_request())
    seed.close()

    def attempt(resolution_id: str):
        adapter = SqliteHumanIntervention(tmp_path / "human.sqlite")
        try:
            return resolve(adapter, make_resolution(resolution_id=resolution_id))
        except RequestAlreadyResolvedError:
            return None
        finally:
            adapter.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ("res-a", "res-b")))

    assert sum(item is not None for item in results) == 1
    check = store(tmp_path)
    assert len(check.grants_for("WO-1")) == 1
    check.close()


# -- rejected requests ------------------------------------------------------


def test_wrong_request_is_rejected_and_leaves_no_grant(tmp_path) -> None:
    adapter = store(tmp_path)
    adapter.record_request(make_request())
    with pytest.raises(UnknownInterventionRequestError):
        resolve(adapter, make_resolution(intervention_id="other-request"))
    assert adapter.grants_for("WO-1") == ()
    adapter.close()


def test_expired_request_is_rejected_and_leaves_no_grant(tmp_path) -> None:
    adapter = store(tmp_path)
    adapter.record_request(make_request(expires_at=NOW + timedelta(minutes=5)))
    with pytest.raises(ExpiredRequestError):
        resolve(adapter, make_resolution(resolved_at=NOW + timedelta(minutes=6)))
    assert adapter.grants_for("WO-1") == ()
    adapter.close()


def test_concurrent_source_edit_rejects_resolution(tmp_path) -> None:
    adapter = store(tmp_path)
    adapter.record_request(make_request())
    changed = HumanResolution(
        resolution_id="res-1",
        intervention_id="req-1",
        work_order_id=WorkOrderId("WO-1"),
        actor="operator-1",
        action=HumanAction.APPROVE,
        resolved_at=NOW,
        scope=("src/**",),
        source_revision=SourceRevision("src-rev-2"),
        work_order_revision=REVISION,
    )
    with pytest.raises(StaleResolutionBindingError):
        adapter.resolve(
            changed,
            current_source_revision=SourceRevision("src-rev-2"),
            current_work_order_revision=REVISION,
        )
    assert adapter.grants_for("WO-1") == ()
    adapter.close()


def test_scope_widening_is_rejected_at_the_persistence_boundary(tmp_path) -> None:
    adapter = store(tmp_path)
    adapter.record_request(make_request())
    with pytest.raises(ScopeWideningError):
        resolve(adapter, make_resolution(scope=("src/**", "docs/**")))
    assert adapter.grants_for("WO-1") == ()
    adapter.close()


# -- revocation -------------------------------------------------------------


def test_revoked_resolution_is_excluded_and_can_be_replaced(tmp_path) -> None:
    adapter = store(tmp_path)
    adapter.record_request(make_request())
    resolution, grant = resolve(adapter, make_resolution())

    revoked_resolution, revoked_grant = adapter.revoke("res-1", at=LATER, reason="operator withdrew")
    assert revoked_resolution.is_revoked
    assert revoked_resolution.revoked_at == LATER
    assert revoked_resolution.revocation_reason == "operator withdrew"
    assert revoked_grant.is_revoked
    assert revoked_grant.grant_id == grant.grant_id

    assert adapter.grants_for("WO-1") == ()
    assert adapter.grants_for("WO-1", include_revoked=True) == (revoked_grant,)

    # Revocation is idempotent.
    assert adapter.revoke("res-1", at=LATER + timedelta(minutes=1))[0] == revoked_resolution

    # A replaced resolution is allowed once the prior one is revoked.
    replacement, replacement_grant = resolve(adapter, make_resolution(resolution_id="res-2"))
    assert not replacement.is_revoked
    assert replacement_grant.grant_id != revoked_grant.grant_id
    assert adapter.grants_for("WO-1") == (replacement_grant,)
    assert adapter.grants_for("WO-1", include_revoked=True) == (revoked_grant, replacement_grant)
    adapter.close()


def test_revocation_survives_restart(tmp_path) -> None:
    adapter = store(tmp_path)
    adapter.record_request(make_request())
    resolve(adapter, make_resolution())
    adapter.revoke("res-1", at=LATER, reason="withdrawn")
    adapter.close()

    restarted = store(tmp_path)
    assert restarted.resolution("res-1").is_revoked
    assert restarted.grants_for("WO-1") == ()
    restarted.close()
