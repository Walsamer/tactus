"""Tactus -> Ictus ``StateSnapshot`` v1 adapter tests.

Contract tests validate the emitted payload against the shape of the
authoritative Ictus ``contracts/state-snapshot.schema.json`` (Ictus commit
``833175d``) without vendoring that file: the ``jsonschema`` package is not a
Tactus dependency, so the schema constraints that matter at this edge are
encoded in :func:`_assert_state_snapshot_v1`.

Purity tests assert the adapter has no side effects on the Work Order lifecycle
and contains no recovery-decision logic.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

import tactus.integrations.ictus.state_snapshot as state_snapshot
from tactus.backends import (
    BackendAdministrativeEnablementFact,
    BackendDescriptorFact,
    BackendHealth,
    BackendHealthFact,
    BackendId,
    BackendProviderQuotaFact,
)
from tactus.domain import OpenStatus, WorkOrder, WorkOrderState
from tactus.integrations.ictus import (
    FACT_ATTEMPT_NUMBER,
    FACT_BACKEND_ADMINISTRATIVE_ENABLEMENT,
    FACT_BACKEND_DESCRIPTORS,
    FACT_BACKEND_HEALTH,
    FACT_BACKEND_PREVIOUS_BACKEND,
    FACT_BACKEND_PROVIDER_QUOTA,
    FACT_CAPABILITY_ID,
    FACT_DOMAIN_DEPENDENCIES_SATISFIED,
    FACT_DOMAIN_SCOPE_CONSTRAINTS,
    FACT_OBSERVATION_CATEGORY,
    FACT_OBSERVATION_MESSAGE,
    FACT_RECOVERY_SEMANTIC_ATTEMPTS,
    FACT_WORK_ORDER_ID,
    FACT_WORK_ORDER_STATE,
    IctusObservationCategory,
    StateSnapshotContractError,
    TactusObservation,
    AttemptHistory,
    BackendFacts,
    build_state_snapshot,
    parse_observation,
)

_TIMESTAMP = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)

# The closed Ictus v1 observation vocabulary (must never be widened by Tactus).
_ICTUS_CATEGORIES = {member.value for member in IctusObservationCategory}

# The authoritative Ictus ``StateSnapshot`` v1 top-level property set.
_SCHEMA_TOP_LEVEL_KEYS = {
    "schema_version",
    "snapshot_id",
    "timestamp",
    "domain",
    "subject",
    "facts",
    "capabilities",
    "constraints",
}


# -- schema-shape validator -------------------------------------------------


def _assert_state_snapshot_v1(payload: Any) -> None:
    """Validate the shape of an Ictus ``StateSnapshot`` v1 payload.

    Mirrors ``contracts/state-snapshot.schema.json`` at Ictus commit ``833175d``:
    required fields, primitive types, ``schema_version`` const, ``date-time``
    timestamp and the fact/subject/capabilities/constraints shapes.
    """

    assert isinstance(payload, dict)
    assert set(payload) == _SCHEMA_TOP_LEVEL_KEYS

    assert payload["schema_version"] == 1
    assert isinstance(payload["schema_version"], int)
    assert not isinstance(payload["schema_version"], bool)

    assert isinstance(payload["snapshot_id"], str) and payload["snapshot_id"]
    assert isinstance(payload["domain"], str) and payload["domain"]
    assert isinstance(payload["timestamp"], str) and payload["timestamp"]
    # ``format: date-time``.
    datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))

    subject = payload["subject"]
    assert isinstance(subject, dict)
    assert set(subject) >= {"type", "id"}
    assert isinstance(subject["type"], str) and subject["type"]
    assert isinstance(subject["id"], str) and subject["id"]

    facts = payload["facts"]
    assert isinstance(facts, list)
    for fact in facts:
        assert isinstance(fact, dict)
        assert set(fact) >= {"key", "value"}
        assert isinstance(fact["key"], str) and fact["key"]

    capabilities = payload["capabilities"]
    assert isinstance(capabilities, list)
    assert all(isinstance(item, str) and item for item in capabilities)

    constraints = payload["constraints"]
    assert isinstance(constraints, list)
    assert all(isinstance(item, str) and item for item in constraints)


def _facts(payload: dict[str, Any]) -> dict[str, Any]:
    keys = [fact["key"] for fact in payload["facts"]]
    assert len(keys) == len(set(keys)), "fact keys must be unique"
    return {fact["key"]: fact["value"] for fact in payload["facts"]}


# -- fixtures ---------------------------------------------------------------


def _observation(category: str = "WORKER_TIMEOUT", **overrides: Any) -> TactusObservation:
    raw: dict[str, Any] = {
        "schema_version": 1,
        "observation_id": "obs-0001",
        "execution_id": "exec-0001",
        "intent_id": "intent:proposal-0001",
        "category": category,
        "message": "worker exceeded its timeout class",
        "observed_at": "2026-10-01T00:00:00Z",
        "retryable": True,
    }
    raw.update(overrides)
    return parse_observation(raw)


def _active_work_order(work_order_id: str = "WO-1") -> WorkOrder:
    work_order = WorkOrder.create(work_order_id)
    work_order.admit(reason="admitted")
    work_order.set_readiness(OpenStatus.READY)
    work_order.claim(reason="claimed")
    return work_order


def _build(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "work_order": _active_work_order(),
        "observation": _observation(),
        "snapshot_id": "snap-0001",
        "timestamp": _TIMESTAMP,
    }
    kwargs.update(overrides)
    return build_state_snapshot(**kwargs)


# -- contract: valid StateSnapshot v1 --------------------------------------


def test_minimal_snapshot_is_valid_ictus_state_snapshot_v1() -> None:
    payload = _build()

    _assert_state_snapshot_v1(payload)
    assert payload["schema_version"] == 1
    assert payload["subject"] == {"type": "work_order", "id": "WO-1"}
    assert payload["domain"] == "software"
    assert payload["timestamp"] == "2026-10-01T00:00:00Z"
    assert payload["capabilities"] == []
    assert payload["constraints"] == []


def test_full_snapshot_is_valid_ictus_state_snapshot_v1() -> None:
    payload = _build(
        capability_id="demo.verify",
        attempt_history=AttemptHistory(
            dagster_micro_retry_attempt=1, semantic_attempts=3
        ),
        backend_facts=BackendFacts(
            descriptors=(
                BackendDescriptorFact(
                    BackendId("backend-a"),
                    capabilities=("shell",),
                    runtime="local",
                    model="model-a",
                    provider="provider-a",
                    constraints=("workspace:repo",),
                    provenance="declaration",
                    observed_at=_TIMESTAMP,
                ),
                BackendDescriptorFact(
                    BackendId("backend-b"),
                    capabilities=("git", "shell"),
                    provenance="declaration",
                    observed_at=_TIMESTAMP,
                ),
            ),
            health=(
                BackendHealthFact(
                    BackendId("backend-a"),
                    BackendHealth.AVAILABLE,
                    provenance="probe",
                    observed_at=_TIMESTAMP,
                ),
            ),
            administrative_enablement=(
                BackendAdministrativeEnablementFact(
                    BackendId("backend-a"),
                    enabled=False,
                    provenance="operator",
                    observed_at=_TIMESTAMP,
                    reason="maintenance",
                ),
            ),
            provider_quota=(
                BackendProviderQuotaFact(
                    BackendId("backend-a"),
                    limit=10,
                    unit="requests_per_minute",
                    provenance="provider_api",
                    observed_at=_TIMESTAMP,
                ),
            ),
            previous_backend="backend-a",
        ),
        dependencies_satisfied=False,
        scope_constraints=("src/**", "tests/**"),
        constraints=("approval_required",),
    )

    _assert_state_snapshot_v1(payload)
    assert payload["capabilities"] == ["demo.verify"]
    assert payload["constraints"] == ["approval_required"]

    facts = _facts(payload)
    assert facts[FACT_CAPABILITY_ID] == "demo.verify"
    assert facts[FACT_BACKEND_DESCRIPTORS] == [
        {
            "schema_version": 1,
            "backend_id": "backend-a",
            "capabilities": ["shell"],
            "runtime": "local",
            "model": "model-a",
            "provider": "provider-a",
            "constraints": ["workspace:repo"],
            "provenance": "declaration",
            "observed_at": "2026-10-01T00:00:00Z",
            "expires_at": None,
        },
        {
            "schema_version": 1,
            "backend_id": "backend-b",
            "capabilities": ["git", "shell"],
            "runtime": None,
            "model": None,
            "provider": None,
            "constraints": [],
            "provenance": "declaration",
            "observed_at": "2026-10-01T00:00:00Z",
            "expires_at": None,
        },
    ]
    assert facts[FACT_BACKEND_HEALTH][0]["health"] == "AVAILABLE"
    assert facts[FACT_BACKEND_ADMINISTRATIVE_ENABLEMENT][0]["enabled"] is False
    assert facts[FACT_BACKEND_PROVIDER_QUOTA][0]["limit"] == 10
    assert facts[FACT_BACKEND_PREVIOUS_BACKEND] == "backend-a"
    assert facts[FACT_DOMAIN_DEPENDENCIES_SATISFIED] is False
    assert facts[FACT_DOMAIN_SCOPE_CONSTRAINTS] == ["src/**", "tests/**"]


def test_snapshot_is_json_serializable() -> None:
    payload = _build(
        capability_id="demo.verify",
        backend_facts=BackendFacts(
            descriptors=(BackendDescriptorFact(BackendId("backend-a")),),
        ),
        constraints=("approval_required",),
    )

    assert json.loads(json.dumps(payload)) == payload


def test_snapshot_id_defaults_to_a_fresh_non_empty_id() -> None:
    first = build_state_snapshot(
        work_order=_active_work_order(), observation=_observation(), timestamp=_TIMESTAMP
    )
    second = build_state_snapshot(
        work_order=_active_work_order(), observation=_observation(), timestamp=_TIMESTAMP
    )

    _assert_state_snapshot_v1(first)
    _assert_state_snapshot_v1(second)
    assert first["snapshot_id"] and second["snapshot_id"]
    assert first["snapshot_id"] != second["snapshot_id"]


def test_naive_timestamp_is_treated_as_utc() -> None:
    payload = _build(timestamp=datetime(2026, 10, 1, 12, 30))

    _assert_state_snapshot_v1(payload)
    assert payload["timestamp"] == "2026-10-01T12:30:00Z"


def test_work_order_facts_are_present_and_reflect_the_entity() -> None:
    work_order = _active_work_order("WO-42")
    payload = _build(work_order=work_order)

    facts = _facts(payload)
    assert facts[FACT_WORK_ORDER_ID] == "WO-42"
    assert facts[FACT_WORK_ORDER_STATE] == WorkOrderState.ACTIVE.value


def test_open_work_order_readiness_is_exposed_as_a_fact() -> None:
    work_order = WorkOrder.create("WO-1")
    work_order.admit(reason="admitted")
    work_order.set_readiness(OpenStatus.BLOCKED)

    payload = _build(work_order=work_order)

    facts = _facts(payload)
    assert facts[FACT_WORK_ORDER_STATE] == WorkOrderState.OPEN.value
    assert facts["work_order.readiness"] == OpenStatus.BLOCKED.value


# -- execution observation included without reclassification ---------------


@pytest.mark.parametrize("category", [member.value for member in IctusObservationCategory])
def test_observation_category_is_copied_verbatim(category: str) -> None:
    payload = _build(observation=_observation(category=category))

    facts = _facts(payload)
    assert facts[FACT_OBSERVATION_CATEGORY] == category
    assert facts[FACT_OBSERVATION_CATEGORY] in _ICTUS_CATEGORIES


def test_observation_category_is_never_replaced_by_a_domain_fact() -> None:
    # A domain/control fact that Ictus's execution taxonomy cannot represent
    # must never be smuggled in as the observation category.
    payload = _build(
        observation=_observation(category="VERIFICATION_FAILURE"),
        dependencies_satisfied=False,
        scope_constraints=("SCOPE_VIOLATION",),
    )

    facts = _facts(payload)
    assert facts[FACT_OBSERVATION_CATEGORY] == "VERIFICATION_FAILURE"
    assert facts[FACT_OBSERVATION_CATEGORY] != "UNKNOWN"


def test_observation_message_is_preserved_without_reclassification() -> None:
    payload = _build(observation=_observation(message="distinct failure detail"))

    assert _facts(payload)[FACT_OBSERVATION_MESSAGE] == "distinct failure detail"


def test_observation_message_is_omitted_when_absent() -> None:
    observation = parse_observation(
        {
            "schema_version": 1,
            "observation_id": "obs-0001",
            "execution_id": "exec-0001",
            "intent_id": "intent:proposal-0001",
            "category": "SUCCESS",
            "observed_at": "2026-10-01T00:00:00Z",
        }
    )

    payload = _build(observation=observation)

    assert FACT_OBSERVATION_MESSAGE not in _facts(payload)


def test_observation_evidence_is_preserved() -> None:
    observation = _observation(
        evidence=[
            {
                "kind": "test_report",
                "uri": "file:///artifacts/report.xml",
                "sha256": "a" * 64,
                "note": "verifier output",
            }
        ]
    )

    payload = _build(observation=observation)

    assert _facts(payload)["observation.evidence"] == [
        {
            "kind": "test_report",
            "uri": "file:///artifacts/report.xml",
            "sha256": "a" * 64,
            "note": "verifier output",
        }
    ]
    _assert_state_snapshot_v1(payload)


# -- the two distinct attempt counters -------------------------------------


def test_two_attempt_counters_are_represented_separately() -> None:
    payload = _build(
        attempt_history=AttemptHistory(
            dagster_micro_retry_attempt=1, semantic_attempts=4
        )
    )

    facts = _facts(payload)
    assert facts[FACT_ATTEMPT_NUMBER] == 1
    assert facts[FACT_RECOVERY_SEMANTIC_ATTEMPTS] == 4


def test_dagster_micro_retry_does_not_change_semantic_attempts() -> None:
    first = _build(attempt_history=AttemptHistory(dagster_micro_retry_attempt=0, semantic_attempts=2))
    second = _build(attempt_history=AttemptHistory(dagster_micro_retry_attempt=7, semantic_attempts=2))

    first_facts, second_facts = _facts(first), _facts(second)
    assert first_facts[FACT_ATTEMPT_NUMBER] == 0
    assert second_facts[FACT_ATTEMPT_NUMBER] == 7
    assert first_facts[FACT_RECOVERY_SEMANTIC_ATTEMPTS] == 2
    assert second_facts[FACT_RECOVERY_SEMANTIC_ATTEMPTS] == 2


def test_default_attempt_history_is_zero_and_zero() -> None:
    facts = _facts(_build())

    assert facts[FACT_ATTEMPT_NUMBER] == 0
    assert facts[FACT_RECOVERY_SEMANTIC_ATTEMPTS] == 0


# -- purity: no WorkOrder / lifecycle mutation -----------------------------


def test_adapter_does_not_mutate_work_order_lifecycle() -> None:
    work_order = _active_work_order()
    before = (
        work_order.state,
        work_order.readiness,
        work_order.transitions,
        work_order.failure_observations,
    )

    build_state_snapshot(
        work_order=work_order,
        observation=_observation(),
        timestamp=_TIMESTAMP,
        capability_id="demo.verify",
        attempt_history=AttemptHistory(semantic_attempts=5),
        backend_facts=BackendFacts(
            descriptors=(BackendDescriptorFact(BackendId("backend-a")),),
        ),
        constraints=("approval_required",),
    )

    assert (
        work_order.state,
        work_order.readiness,
        work_order.transitions,
        work_order.failure_observations,
    ) == before
    assert work_order.state is WorkOrderState.ACTIVE


def test_adapter_repeated_calls_do_not_accumulate_state_on_the_work_order() -> None:
    work_order = _active_work_order()
    transitions_before = work_order.transitions

    build_state_snapshot(work_order=work_order, observation=_observation(), timestamp=_TIMESTAMP)
    build_state_snapshot(work_order=work_order, observation=_observation(), timestamp=_TIMESTAMP)

    assert work_order.transitions == transitions_before
    assert work_order.failure_observations == ()
    assert work_order.state is WorkOrderState.ACTIVE


def _walk(value: Any):
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk(item)
    else:
        yield value


def test_snapshot_contains_no_work_order_or_persistence_object() -> None:
    # Persistence/domain entities must never leak into the snapshot; only plain
    # JSON values may appear.
    payload = _build(
        capability_id="demo.verify",
        backend_facts=BackendFacts(
            descriptors=(BackendDescriptorFact(BackendId("backend-a")),),
        ),
    )

    for value in _walk(payload):
        assert not isinstance(value, WorkOrder)
        assert value is None or isinstance(value, (str, int, float, bool, dict, list))


# -- purity: no recovery-decision logic ------------------------------------


@pytest.mark.parametrize(
    "forbidden",
    [
        "RecoveryDecision",
        "decide",
        "to_decision",
        "apply_decision",
        "recovery_decision",
        "select_backend",
        "rank_backends",
        "observation_from_failure",
    ],
)
def test_adapter_has_no_decision_surface(forbidden: str) -> None:
    assert not hasattr(state_snapshot, forbidden)


# -- fail-closed input validation ------------------------------------------


def test_non_work_order_input_is_rejected() -> None:
    with pytest.raises(StateSnapshotContractError):
        build_state_snapshot(
            work_order="WO-1",  # type: ignore[arg-type]
            observation=_observation(),
            timestamp=_TIMESTAMP,
        )


def test_non_tactus_observation_input_is_rejected() -> None:
    with pytest.raises(StateSnapshotContractError):
        build_state_snapshot(
            work_order=_active_work_order(),
            observation={"category": "WORKER_TIMEOUT"},  # type: ignore[arg-type]
            timestamp=_TIMESTAMP,
        )


@pytest.mark.parametrize("value", [-1, 1.5, True, "1"])
def test_invalid_attempt_counter_is_rejected(value: object) -> None:
    with pytest.raises(StateSnapshotContractError):
        AttemptHistory(dagster_micro_retry_attempt=value)  # type: ignore[arg-type]


def test_empty_capability_id_is_rejected() -> None:
    with pytest.raises(StateSnapshotContractError):
        _build(capability_id="   ")


def test_empty_scope_constraint_is_rejected() -> None:
    with pytest.raises(StateSnapshotContractError):
        _build(scope_constraints=("",))
