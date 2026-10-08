"""Versioned Tactus -> Ictus ``StateSnapshot`` profile tests.

Contract tests validate the emitted payload against the shape of the
authoritative Ictus ``contracts/state-snapshot.schema.json`` (Ictus commit
``833175d``) without vendoring that file: the ``jsonschema`` package is not a
Tactus dependency, so the schema constraints that matter at this edge are
encoded in :func:`_assert_state_snapshot_v1`.

Profile tests cover the two phases (INITIAL without a fabricated observation,
RECOVERY with a correlated, subject/intent/attempt-bound observation), the
canonical semantic count/limit, the diagnostic step-retry index, raw backend
and authorization facts, deterministic profile/digest, and fail-closed
validation. Purity tests assert the adapter has no side effects on the Work
Order lifecycle and contains no recovery-decision logic.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

import tactus.integrations.ictus.state_snapshot as state_snapshot
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
    WorkOrderState,
)
from tactus.integrations.ictus import (
    FACT_AUTHORIZATION_GRANTS,
    FACT_BACKEND_DESCRIPTORS,
    FACT_BACKEND_PREVIOUS_BACKEND,
    FACT_BACKEND_QUOTA,
    FACT_BACKEND_STATUS,
    FACT_DOMAIN_DEPENDENCIES_SATISFIED,
    FACT_DOMAIN_SCOPE_CONSTRAINTS,
    FACT_EXECUTION_STEP_RETRY_INDEX,
    FACT_OBSERVATION_CATEGORY,
    FACT_OBSERVATION_EVIDENCE,
    FACT_OBSERVATION_MESSAGE,
    FACT_PROFILE_VERSION,
    FACT_RECOVERY_ATTEMPT_ID,
    FACT_RECOVERY_INTENT_ID,
    FACT_RECOVERY_MAX_SEMANTIC_ATTEMPTS,
    FACT_RECOVERY_SEMANTIC_ATTEMPTS,
    FACT_SNAPSHOT_DIGEST,
    FACT_SNAPSHOT_PHASE,
    FACT_SOURCE_REVISION,
    FACT_WORK_ORDER_ID,
    FACT_WORK_ORDER_REVISION,
    FACT_WORK_ORDER_STATE,
    IctusObservationCategory,
    MalformedSnapshotError,
    SemanticBudget,
    SnapshotPhase,
    StateSnapshotContractError,
    StepRetryDiagnostic,
    TactusObservation,
    UnsupportedSnapshotProfileError,
    UnsupportedSnapshotVersionError,
    AuthorizationGrant,
    AttemptHistory,
    BackendFacts,
    build_state_snapshot,
    compute_snapshot_digest,
    parse_observation,
    semantic_budget_from_legacy,
    validate_snapshot_profile,
)

_TIMESTAMP = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
_EXPIRY = datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc)

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

    Mirrors ``contracts/state-snapshot.schema.json`` at Ictus commit ``833175d``.
    """

    assert isinstance(payload, dict)
    assert set(payload) == _SCHEMA_TOP_LEVEL_KEYS

    assert payload["schema_version"] == 1
    assert isinstance(payload["schema_version"], int)
    assert not isinstance(payload["schema_version"], bool)

    assert isinstance(payload["snapshot_id"], str) and payload["snapshot_id"]
    assert isinstance(payload["domain"], str) and payload["domain"]
    assert isinstance(payload["timestamp"], str) and payload["timestamp"]
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
        "intent_id": "intent-0001",
        "category": category,
        "message": "worker exceeded its timeout class",
        "observed_at": "2026-10-01T00:00:00Z",
        "retryable": True,
    }
    raw.update(overrides)
    return parse_observation(raw)


def _active_work_order(work_order_id: str = "WO-1") -> WorkOrder:
    work_order = WorkOrder.create(work_order_id)
    work_order.admit(reason="admitted", at=_TIMESTAMP)
    work_order.set_readiness(OpenStatus.READY)
    work_order.claim(reason="claimed", at=_TIMESTAMP)
    return work_order


def _attempt(*, work_order_id: str = "WO-1", intent_id: str = "intent-0001",
             dagster_run_id: str = "exec-0001", attempt_id: str = "attempt-0001") -> ExecutionAttempt:
    return ExecutionAttempt(
        attempt_id=ExecutionAttemptId(attempt_id),
        work_order_id=WorkOrder.create(work_order_id).id,
        intent_id=ExecutionIntentId(intent_id),
        dagster_run_id=DagsterRunId(dagster_run_id),
        accepted_at=_TIMESTAMP,
    )


def _backend_facts() -> BackendFacts:
    return BackendFacts(
        descriptors=(
            BackendDescriptor(
                backend_id=BackendId("backend-a"),
                capabilities=frozenset({"demo.verify"}),
                provider="provider-x",
                model="model-x",
                agent_runtime="local-subprocess",
            ),
        ),
        statuses=(
            BackendStatusObservation(
                backend=BackendId("backend-a"),
                status=BackendHealth.AVAILABLE,
                observed_at=_TIMESTAMP,
                expires_at=_EXPIRY,
            ),
        ),
        quotas=(
            ProviderCapacityObservation(
                backend=BackendId("backend-a"),
                observed_at=_TIMESTAMP,
                limit=10,
                unit="runs",
            ),
        ),
        previous_backend="backend-a",
    )


def _build_initial(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "work_order": _active_work_order(),
        "phase": SnapshotPhase.INITIAL,
        "snapshot_id": "snap-initial-0001",
        "timestamp": _TIMESTAMP,
        "semantic_budget": SemanticBudget(semantic_attempts=0, max_semantic_attempts=3),
    }
    kwargs.update(overrides)
    return build_state_snapshot(**kwargs)


def _build_recovery(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "work_order": _active_work_order(),
        "phase": SnapshotPhase.RECOVERY,
        "observation": _observation(),
        "attempt": _attempt(),
        "snapshot_id": "snap-recovery-0001",
        "timestamp": _TIMESTAMP,
        "semantic_budget": SemanticBudget(semantic_attempts=1, max_semantic_attempts=3),
    }
    kwargs.update(overrides)
    return build_state_snapshot(**kwargs)


# -- contract: valid StateSnapshot v1 --------------------------------------


def test_minimal_initial_snapshot_is_valid_ictus_state_snapshot_v1() -> None:
    payload = _build_initial()

    _assert_state_snapshot_v1(payload)
    assert payload["schema_version"] == 1
    assert payload["subject"] == {"type": "work_order", "id": "WO-1"}
    assert payload["domain"] == "software"
    assert payload["timestamp"] == "2026-10-01T00:00:00Z"
    assert payload["capabilities"] == []
    assert payload["constraints"] == []


def test_full_snapshot_is_valid_ictus_state_snapshot_v1() -> None:
    payload = _build_initial(
        capability_id="demo.verify",
        backend_facts=_backend_facts(),
        authorization_grants=(
            AuthorizationGrant(
                grant_id="grant-0001",
                scope="WO-1",
                actor="operator-1",
                issued_at=_TIMESTAMP,
                expires_at=_EXPIRY,
                source_revision="src-rev-1",
                evidence=("sig:abc",),
            ),
        ),
        dependencies_satisfied=False,
        scope_constraints=("src/**", "tests/**"),
        constraints=("approval_required",),
        source_revision="src-rev-1",
        work_order_revision=2,
    )

    _assert_state_snapshot_v1(payload)
    assert payload["capabilities"] == ["demo.verify"]
    assert payload["constraints"] == ["approval_required"]

    facts = _facts(payload)
    assert facts[FACT_DOMAIN_DEPENDENCIES_SATISFIED] is False
    assert facts[FACT_DOMAIN_SCOPE_CONSTRAINTS] == ["src/**", "tests/**"]
    assert facts[FACT_SOURCE_REVISION] == "src-rev-1"
    assert facts[FACT_WORK_ORDER_REVISION] == 2


def test_snapshot_is_json_serializable() -> None:
    payload = _build_initial(backend_facts=_backend_facts())

    assert json.loads(json.dumps(payload)) == payload


def test_snapshot_id_defaults_to_a_fresh_non_empty_id() -> None:
    first = build_state_snapshot(
        work_order=_active_work_order(),
        timestamp=_TIMESTAMP,
        semantic_budget=SemanticBudget(0, 1),
    )
    second = build_state_snapshot(
        work_order=_active_work_order(),
        timestamp=_TIMESTAMP,
        semantic_budget=SemanticBudget(0, 1),
    )

    _assert_state_snapshot_v1(first)
    _assert_state_snapshot_v1(second)
    assert first["snapshot_id"] and second["snapshot_id"]
    assert first["snapshot_id"] != second["snapshot_id"]


def test_naive_timestamp_is_treated_as_utc() -> None:
    payload = _build_initial(timestamp=datetime(2026, 10, 1, 12, 30))

    _assert_state_snapshot_v1(payload)
    assert payload["timestamp"] == "2026-10-01T12:30:00Z"


def test_work_order_facts_are_present_and_reflect_the_entity() -> None:
    work_order = _active_work_order("WO-42")
    payload = _build_initial(work_order=work_order)

    facts = _facts(payload)
    assert facts[FACT_WORK_ORDER_ID] == "WO-42"
    assert facts[FACT_WORK_ORDER_STATE] == WorkOrderState.ACTIVE.value


def test_open_work_order_readiness_is_exposed_as_a_fact() -> None:
    work_order = WorkOrder.create("WO-1")
    work_order.admit(reason="admitted")
    work_order.set_readiness(OpenStatus.BLOCKED)

    payload = _build_initial(work_order=work_order)

    facts = _facts(payload)
    assert facts[FACT_WORK_ORDER_STATE] == WorkOrderState.OPEN.value
    assert facts["work_order.readiness"] == OpenStatus.BLOCKED.value


# -- profile version / phase ------------------------------------------------


def test_profile_version_and_phase_are_emitted() -> None:
    initial = _facts(_build_initial())
    recovery = _facts(_build_recovery())

    assert initial[FACT_PROFILE_VERSION] == 2
    assert initial[FACT_SNAPSHOT_PHASE] == SnapshotPhase.INITIAL.value
    assert recovery[FACT_PROFILE_VERSION] == 2
    assert recovery[FACT_SNAPSHOT_PHASE] == SnapshotPhase.RECOVERY.value


def test_phase_is_inferred_from_supplied_context() -> None:
    initial = build_state_snapshot(
        work_order=_active_work_order(),
        snapshot_id="snap-1",
        timestamp=_TIMESTAMP,
        semantic_budget=SemanticBudget(0, 1),
    )
    recovery = build_state_snapshot(
        work_order=_active_work_order(),
        observation=_observation(),
        attempt=_attempt(),
        snapshot_id="snap-2",
        timestamp=_TIMESTAMP,
        semantic_budget=SemanticBudget(1, 1),
    )

    assert _facts(initial)[FACT_SNAPSHOT_PHASE] == "INITIAL"
    assert _facts(recovery)[FACT_SNAPSHOT_PHASE] == "RECOVERY"


def test_unknown_phase_string_is_rejected() -> None:
    with pytest.raises(StateSnapshotContractError):
        _build_initial(phase="BOGUS")


# -- INITIAL requires no fabricated observation ----------------------------


def test_initial_snapshot_has_no_observation_facts() -> None:
    facts = _facts(_build_initial())

    assert FACT_OBSERVATION_CATEGORY not in facts
    assert FACT_OBSERVATION_EVIDENCE not in facts


def test_initial_snapshot_rejects_an_observation() -> None:
    with pytest.raises(StateSnapshotContractError):
        _build_initial(observation=_observation())


def test_initial_snapshot_rejects_an_attempt_correlation() -> None:
    with pytest.raises(StateSnapshotContractError):
        _build_initial(attempt=_attempt())


# -- RECOVERY correlation: reject wrong intent/attempt/subject -------------


def test_recovery_snapshot_requires_observation_and_attempt() -> None:
    with pytest.raises(StateSnapshotContractError):
        _build_recovery(observation=None)
    with pytest.raises(StateSnapshotContractError):
        _build_recovery(attempt=None)


def test_recovery_snapshot_rejects_wrong_intent() -> None:
    with pytest.raises(StateSnapshotContractError):
        _build_recovery(attempt=_attempt(intent_id="intent-OTHER"))


def test_recovery_snapshot_rejects_wrong_subject() -> None:
    with pytest.raises(StateSnapshotContractError):
        _build_recovery(attempt=_attempt(work_order_id="WO-OTHER"))


def test_recovery_snapshot_rejects_wrong_attempt_run() -> None:
    with pytest.raises(StateSnapshotContractError):
        _build_recovery(attempt=_attempt(dagster_run_id="exec-OTHER"))


def test_recovery_snapshot_rejects_success_observation() -> None:
    with pytest.raises(StateSnapshotContractError):
        _build_recovery(observation=_observation(category="SUCCESS"))


def test_recovery_snapshot_binds_subject_intent_and_attempt() -> None:
    facts = _facts(_build_recovery())

    assert facts[FACT_RECOVERY_ATTEMPT_ID] == "attempt-0001"
    assert facts[FACT_RECOVERY_INTENT_ID] == "intent-0001"
    assert facts[FACT_OBSERVATION_CATEGORY] == "WORKER_TIMEOUT"


# -- execution observation is included, never reclassified -----------------


@pytest.mark.parametrize("category", [member.value for member in IctusObservationCategory])
def test_observation_category_is_copied_verbatim(category: str) -> None:
    if category == "SUCCESS":
        # Success is applied before recovery, never fed through it.
        with pytest.raises(StateSnapshotContractError):
            _build_recovery(observation=_observation(category=category))
        return
    payload = _build_recovery(observation=_observation(category=category))

    facts = _facts(payload)
    assert facts[FACT_OBSERVATION_CATEGORY] == category
    assert facts[FACT_OBSERVATION_CATEGORY] in _ICTUS_CATEGORIES


def test_observation_category_is_never_replaced_by_a_domain_fact() -> None:
    payload = _build_recovery(
        observation=_observation(category="VERIFICATION_FAILURE"),
        dependencies_satisfied=False,
        scope_constraints=("SCOPE_VIOLATION",),
    )

    facts = _facts(payload)
    assert facts[FACT_OBSERVATION_CATEGORY] == "VERIFICATION_FAILURE"
    assert facts[FACT_OBSERVATION_CATEGORY] != "UNKNOWN"


def test_observation_message_is_preserved_and_optional() -> None:
    with_message = _facts(_build_recovery(observation=_observation(message="detail")))
    assert with_message[FACT_OBSERVATION_MESSAGE] == "detail"

    observation = parse_observation(
        {
            "schema_version": 1,
            "observation_id": "obs-0001",
            "execution_id": "exec-0001",
            "intent_id": "intent-0001",
            "category": "PROCESS_CRASH",
            "observed_at": "2026-10-01T00:00:00Z",
        }
    )
    assert FACT_OBSERVATION_MESSAGE not in _facts(_build_recovery(observation=observation))


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

    payload = _build_recovery(observation=observation)

    assert _facts(payload)[FACT_OBSERVATION_EVIDENCE] == [
        {
            "kind": "test_report",
            "uri": "file:///artifacts/report.xml",
            "sha256": "a" * 64,
            "note": "verifier output",
        }
    ]
    _assert_state_snapshot_v1(payload)


# -- canonical semantic count/limit vs diagnostic step retry ---------------


def test_canonical_semantic_count_and_limit_are_emitted() -> None:
    facts = _facts(_build_initial(semantic_budget=SemanticBudget(0, 2)))

    assert facts[FACT_RECOVERY_SEMANTIC_ATTEMPTS] == 0
    assert facts[FACT_RECOVERY_MAX_SEMANTIC_ATTEMPTS] == 2


def test_recovery_semantic_count_includes_the_failed_current_attempt() -> None:
    facts = _facts(_build_recovery(semantic_budget=SemanticBudget(2, 3)))

    assert facts[FACT_RECOVERY_SEMANTIC_ATTEMPTS] == 2
    assert facts[FACT_RECOVERY_MAX_SEMANTIC_ATTEMPTS] == 3


def test_step_retry_index_is_diagnostic_and_independent() -> None:
    first = _facts(_build_initial(step_retry=StepRetryDiagnostic(index=0)))
    second = _facts(_build_initial(step_retry=StepRetryDiagnostic(index=7)))

    assert first[FACT_EXECUTION_STEP_RETRY_INDEX] == 0
    assert second[FACT_EXECUTION_STEP_RETRY_INDEX] == 7
    # The semantic allowance is untouched by the execution-owned diagnostic.
    assert first[FACT_RECOVERY_SEMANTIC_ATTEMPTS] == 0
    assert second[FACT_RECOVERY_SEMANTIC_ATTEMPTS] == 0
    assert first[FACT_RECOVERY_MAX_SEMANTIC_ATTEMPTS] == 3
    assert second[FACT_RECOVERY_MAX_SEMANTIC_ATTEMPTS] == 3


def test_legacy_attempt_number_fact_is_never_emitted() -> None:
    facts = _facts(_build_recovery())

    assert "attempt.number" not in facts
    assert state_snapshot.FACT_ATTEMPT_NUMBER == "attempt.number"


def test_semantic_budget_boundary_rule_matches_documentation() -> None:
    assert SemanticBudget(0, 1).permits_another_attempt is True
    assert SemanticBudget(1, 1).permits_another_attempt is False
    assert SemanticBudget(3, 2).permits_another_attempt is False


# -- raw backend facts, not a filtered candidate list ----------------------


def test_raw_backend_facts_are_exposed() -> None:
    facts = _facts(_build_initial(backend_facts=_backend_facts()))

    descriptors = facts[FACT_BACKEND_DESCRIPTORS]
    assert [item["backend_id"] for item in descriptors] == ["backend-a"]
    assert descriptors[0]["capabilities"] == ["demo.verify"]
    assert descriptors[0]["provider"] == "provider-x"
    assert descriptors[0]["model"] == "model-x"
    assert descriptors[0]["agent_runtime"] == "local-subprocess"

    statuses = facts[FACT_BACKEND_STATUS]
    assert statuses[0]["status"] == "AVAILABLE"
    assert statuses[0]["expires_at"] == "2026-10-02T00:00:00Z"

    quotas = facts[FACT_BACKEND_QUOTA]
    assert quotas[0]["limit"] == 10
    assert quotas[0]["unit"] == "runs"
    assert facts[FACT_BACKEND_PREVIOUS_BACKEND] == "backend-a"


def test_no_policy_filtered_candidate_list_is_emitted() -> None:
    facts = _facts(_build_initial(backend_facts=_backend_facts()))

    assert "backend.available_candidates" not in facts


def test_backend_facts_validation_is_fail_closed() -> None:
    with pytest.raises(StateSnapshotContractError):
        _build_initial(backend_facts="nope")  # type: ignore[arg-type]


# -- raw authorization evidence, not a policy verdict ----------------------


def test_authorization_grants_are_exposed_as_evidence() -> None:
    grant = AuthorizationGrant(
        grant_id="grant-0001",
        scope="WO-1",
        actor="operator-1",
        issued_at=_TIMESTAMP,
        expires_at=_EXPIRY,
        source_revision="src-rev-1",
        evidence=("sig:abc",),
    )

    facts = _facts(_build_initial(authorization_grants=(grant,)))

    entry = facts[FACT_AUTHORIZATION_GRANTS][0]
    assert entry["grant_id"] == "grant-0001"
    assert entry["actor"] == "operator-1"
    assert entry["scope"] == "WO-1"
    assert entry["evidence"] == ["sig:abc"]
    # No policy verdict is asserted anywhere in the snapshot.
    assert "decision" not in json.dumps(facts).lower()


# -- deterministic profile and digest --------------------------------------


def test_profile_and_digest_are_deterministic_for_fixed_clock_and_ids() -> None:
    first = _build_initial(backend_facts=_backend_facts())
    second = _build_initial(backend_facts=_backend_facts())

    assert first == second
    assert _facts(first)[FACT_SNAPSHOT_DIGEST] == _facts(second)[FACT_SNAPSHOT_DIGEST]
    validate_snapshot_profile(first)


def test_digest_changes_with_the_snapshot_body() -> None:
    first = _build_initial(snapshot_id="snap-a")
    second = _build_initial(snapshot_id="snap-b")

    assert _facts(first)[FACT_SNAPSHOT_DIGEST] != _facts(second)[FACT_SNAPSHOT_DIGEST]


def test_digest_is_computed_over_the_body_without_the_digest_fact() -> None:
    payload = _build_initial()
    digest = _facts(payload)[FACT_SNAPSHOT_DIGEST]

    body = dict(payload)
    body["facts"] = [
        fact for fact in payload["facts"] if fact["key"] != FACT_SNAPSHOT_DIGEST
    ]
    assert compute_snapshot_digest(body) == digest


# -- profile validation (consumer side) ------------------------------------


def test_validate_snapshot_profile_accepts_both_phases() -> None:
    validate_snapshot_profile(_build_initial())
    validate_snapshot_profile(_build_recovery())


def test_validate_snapshot_profile_rejects_unsupported_profile_version() -> None:
    payload = _build_initial()
    for fact in payload["facts"]:
        if fact["key"] == FACT_PROFILE_VERSION:
            fact["value"] = 99

    with pytest.raises(UnsupportedSnapshotProfileError):
        validate_snapshot_profile(payload)


def test_validate_snapshot_profile_rejects_unsupported_schema_version() -> None:
    payload = _build_initial()
    payload["schema_version"] = 2

    with pytest.raises(UnsupportedSnapshotVersionError):
        validate_snapshot_profile(payload)


def test_validate_snapshot_profile_rejects_tampered_digest() -> None:
    payload = _build_initial()
    for fact in payload["facts"]:
        if fact["key"] == FACT_SNAPSHOT_DIGEST:
            fact["value"] = "0" * 64

    with pytest.raises(MalformedSnapshotError):
        validate_snapshot_profile(payload)


def test_validate_snapshot_profile_rejects_missing_phase() -> None:
    payload = _build_initial()
    payload["facts"] = [
        fact for fact in payload["facts"] if fact["key"] != FACT_SNAPSHOT_PHASE
    ]

    with pytest.raises(MalformedSnapshotError):
        validate_snapshot_profile(payload)


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
        phase=SnapshotPhase.RECOVERY,
        observation=_observation(),
        attempt=_attempt(),
        snapshot_id="snap-1",
        timestamp=_TIMESTAMP,
        semantic_budget=SemanticBudget(1, 3),
        backend_facts=_backend_facts(),
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

    for _ in range(2):
        build_state_snapshot(
            work_order=work_order,
            timestamp=_TIMESTAMP,
            semantic_budget=SemanticBudget(0, 1),
        )

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
    payload = _build_initial(backend_facts=_backend_facts())

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
            timestamp=_TIMESTAMP,
            semantic_budget=SemanticBudget(0, 1),
        )


def test_missing_semantic_budget_is_rejected() -> None:
    with pytest.raises(StateSnapshotContractError):
        build_state_snapshot(
            work_order=_active_work_order(),
            snapshot_id="snap-1",
            timestamp=_TIMESTAMP,
        )


def test_wrong_semantic_budget_type_is_rejected() -> None:
    with pytest.raises(StateSnapshotContractError):
        _build_initial(semantic_budget=(0, 1))


@pytest.mark.parametrize("value", [-1, 1.5, True, "1", None])
def test_invalid_semantic_count_is_rejected(value: object) -> None:
    with pytest.raises(StateSnapshotContractError):
        SemanticBudget(semantic_attempts=value, max_semantic_attempts=1)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [-1, 1.5, True, "1", None])
def test_invalid_semantic_limit_is_rejected(value: object) -> None:
    with pytest.raises(StateSnapshotContractError):
        SemanticBudget(semantic_attempts=0, max_semantic_attempts=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [-1, 1.5, True, "1"])
def test_invalid_step_retry_is_rejected(value: object) -> None:
    with pytest.raises(StateSnapshotContractError):
        StepRetryDiagnostic(index=value)  # type: ignore[arg-type]
    with pytest.raises(StateSnapshotContractError):
        _build_initial(step_retry=value)


def test_empty_capability_id_is_rejected() -> None:
    with pytest.raises(StateSnapshotContractError):
        _build_initial(capability_id="   ")


def test_empty_scope_constraint_is_rejected() -> None:
    with pytest.raises(StateSnapshotContractError):
        _build_initial(scope_constraints=("",))


# -- explicit legacy translation (never guessed) ---------------------------


def test_legacy_attempt_history_is_translated_explicitly() -> None:
    history = AttemptHistory(dagster_micro_retry_attempt=4, semantic_attempts=2)

    budget = semantic_budget_from_legacy(history, max_semantic_attempts=5)

    assert budget.semantic_attempts == 2
    assert budget.max_semantic_attempts == 5
    # The Dagster step retry was never substituted for the semantic count.
    assert budget.semantic_attempts != history.dagster_micro_retry_attempt


@pytest.mark.parametrize("value", [-1, 1.5, True, "1"])
def test_invalid_legacy_attempt_history_is_rejected(value: object) -> None:
    with pytest.raises(StateSnapshotContractError):
        AttemptHistory(dagster_micro_retry_attempt=value)  # type: ignore[arg-type]
