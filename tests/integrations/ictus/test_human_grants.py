"""Durable Tactus grants -> Ictus authorization-grant fact projection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tactus.domain import HumanAction, HumanGrant, SourceRevision, WorkOrderId
from tactus.integrations.ictus import (
    StateSnapshotContractError,
    authorization_grant_fact,
    authorization_grant_from_human_grant,
)

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def grant(**overrides: object) -> HumanGrant:
    defaults: dict[str, object] = dict(
        grant_id="grant-1",
        resolution_id="res-1",
        intervention_id="req-1",
        work_order_id=WorkOrderId("WO-1"),
        actor="operator-1",
        action=HumanAction.APPROVE,
        scope=("src/**", "tests/**"),
        source_revision=SourceRevision("src-rev-1"),
        issued_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
    )
    defaults.update(overrides)
    return HumanGrant(**defaults)  # type: ignore[arg-type]


def test_grant_projection_carries_typed_action_and_binding() -> None:
    projected = authorization_grant_from_human_grant(grant())
    assert projected.grant_id == "grant-1"
    assert projected.scope == "src/**,tests/**"
    assert projected.actor == "operator-1"
    assert projected.issued_at == _NOW
    assert projected.expires_at == _NOW + timedelta(hours=1)
    assert projected.source_revision == "src-rev-1"
    assert projected.evidence == ("human-action:APPROVE", "resolution:res-1")

    fact = authorization_grant_fact(projected)
    assert fact["scope"] == "src/**,tests/**"
    assert fact["evidence"] == ["human-action:APPROVE", "resolution:res-1"]


def test_unbounded_grant_projects_the_work_order_identity() -> None:
    projected = authorization_grant_from_human_grant(grant(scope=()))
    assert projected.scope == "WO-1"


def test_projection_rejects_a_foreign_value() -> None:
    with pytest.raises(StateSnapshotContractError):
        authorization_grant_from_human_grant(object())  # type: ignore[arg-type]
