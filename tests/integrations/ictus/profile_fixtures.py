"""Shared, deterministic fixtures for the versioned StateSnapshot profile.

These builders intentionally pin every non-deterministic input (clock, ids) so
that the emitted profile and ``snapshot.digest`` are reproducible. They are the
Tactus half of the cross-repository pinned fixtures: Ictus validates the same
payloads at its schema/policy boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tactus.backends import (
    BackendDescriptor,
    BackendHealth,
    BackendId,
    BackendStatusObservation,
    ProviderCapacityObservation,
)
from tactus.domain import (
    DagsterRunId,
    ExecutionAttempt,
    ExecutionAttemptId,
    ExecutionIntentId,
    OpenStatus,
    WorkOrder,
)
from tactus.integrations.ictus import (
    AuthorizationGrant,
    BackendFacts,
    SemanticBudget,
    SnapshotPhase,
    StepRetryDiagnostic,
    TactusObservation,
    build_state_snapshot,
    parse_observation,
)

#: Pinned clock for every fixture.
FIXTURE_TIMESTAMP = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)

#: Pinned second clock used for freshness/expiry.
FIXTURE_EXPIRY = datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc)

DATA_DIR = Path(__file__).parent / "data"


def active_work_order(work_order_id: str = "WO-1") -> WorkOrder:
    work_order = WorkOrder.create(work_order_id)
    work_order.admit(reason="admitted", at=FIXTURE_TIMESTAMP)
    work_order.set_readiness(OpenStatus.READY)
    work_order.claim(reason="claimed", at=FIXTURE_TIMESTAMP)
    return work_order


def open_work_order(work_order_id: str = "WO-1") -> WorkOrder:
    work_order = WorkOrder.create(work_order_id)
    work_order.admit(reason="admitted", at=FIXTURE_TIMESTAMP)
    work_order.set_readiness(OpenStatus.READY)
    return work_order


def sample_observation(
    category: str = "WORKER_TIMEOUT",
    **overrides: object,
) -> TactusObservation:
    raw: dict[str, object] = {
        "schema_version": 1,
        "observation_id": "obs-0001",
        "execution_id": "exec-0001",
        "intent_id": "intent-0001",
        "category": category,
        "message": "worker exceeded its timeout class",
        "observed_at": "2026-10-01T00:00:00Z",
        "retryable": True,
        "evidence": [
            {
                "kind": "test_report",
                "uri": "file:///artifacts/report.xml",
                "sha256": "a" * 64,
                "note": "verifier output",
            }
        ],
    }
    raw.update(overrides)
    return parse_observation(raw)


def sample_attempt(
    work_order_id: str = "WO-1",
    intent_id: str = "intent-0001",
    dagster_run_id: str = "exec-0001",
    attempt_id: str = "attempt-0001",
) -> ExecutionAttempt:
    return ExecutionAttempt(
        attempt_id=ExecutionAttemptId(attempt_id),
        work_order_id=active_work_order(work_order_id).id,
        intent_id=ExecutionIntentId(intent_id),
        dagster_run_id=DagsterRunId(dagster_run_id),
        accepted_at=FIXTURE_TIMESTAMP,
    )


def sample_backend_facts() -> BackendFacts:
    return BackendFacts(
        descriptors=(
            BackendDescriptor(
                backend_id=BackendId("backend-a"),
                capabilities=frozenset({"demo.verify"}),
                agent_runtime="local-subprocess",
                model="model-x",
                provider="provider-x",
            ),
            BackendDescriptor(
                backend_id=BackendId("backend-b"),
                capabilities=frozenset({"demo.verify"}),
            ),
        ),
        statuses=(
            BackendStatusObservation(
                backend=BackendId("backend-a"),
                status=BackendHealth.AVAILABLE,
                observed_at=FIXTURE_TIMESTAMP,
                expires_at=FIXTURE_EXPIRY,
            ),
            BackendStatusObservation(
                backend=BackendId("backend-b"),
                status=BackendHealth.DISABLED,
                observed_at=FIXTURE_TIMESTAMP,
                reason="administratively excluded",
            ),
        ),
        quotas=(
            ProviderCapacityObservation(
                backend=BackendId("backend-a"),
                observed_at=FIXTURE_TIMESTAMP,
                limit=10,
                unit="runs",
                source="provider-api",
                expires_at=FIXTURE_EXPIRY,
            ),
        ),
        previous_backend="backend-a",
    )


def sample_grant() -> AuthorizationGrant:
    return AuthorizationGrant(
        grant_id="grant-0001",
        scope="WO-1",
        actor="operator-1",
        issued_at=FIXTURE_TIMESTAMP,
        expires_at=FIXTURE_EXPIRY,
        source_revision="src-rev-1",
        evidence=("sig:abc123",),
    )


def build_initial_snapshot() -> dict[str, object]:
    return build_state_snapshot(
        work_order=active_work_order(),
        phase=SnapshotPhase.INITIAL,
        snapshot_id="snap-initial-0001",
        timestamp=FIXTURE_TIMESTAMP,
        semantic_budget=SemanticBudget(semantic_attempts=0, max_semantic_attempts=3),
        step_retry=StepRetryDiagnostic(index=0),
        capability_id="demo.verify",
        backend_facts=sample_backend_facts(),
        authorization_grants=(sample_grant(),),
        dependencies_satisfied=True,
        scope_constraints=("src/**", "tests/**"),
        constraints=("approval_required",),
        source_revision="src-rev-1",
        work_order_revision=2,
    )


def build_recovery_snapshot() -> dict[str, object]:
    return build_state_snapshot(
        work_order=active_work_order(),
        phase=SnapshotPhase.RECOVERY,
        observation=sample_observation(),
        attempt=sample_attempt(),
        snapshot_id="snap-recovery-0001",
        timestamp=FIXTURE_TIMESTAMP,
        semantic_budget=SemanticBudget(semantic_attempts=1, max_semantic_attempts=3),
        step_retry=StepRetryDiagnostic(index=2),
        capability_id="demo.verify",
        backend_facts=sample_backend_facts(),
        authorization_grants=(sample_grant(),),
        dependencies_satisfied=True,
        scope_constraints=("src/**", "tests/**"),
        constraints=("approval_required",),
        source_revision="src-rev-1",
        work_order_revision=3,
    )


__all__ = [
    "DATA_DIR",
    "FIXTURE_EXPIRY",
    "FIXTURE_TIMESTAMP",
    "active_work_order",
    "build_initial_snapshot",
    "build_recovery_snapshot",
    "open_work_order",
    "sample_attempt",
    "sample_backend_facts",
    "sample_grant",
    "sample_observation",
]
