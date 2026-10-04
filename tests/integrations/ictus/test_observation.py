from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from tactus.domain import FailureObservation, OpenStatus, WorkOrder, WorkOrderState
from tactus.integrations.ictus import (
    EvidenceRef,
    IctusObservationCategory,
    MalformedObservationError,
    TactusFailureCategory,
    TactusObservation,
    UnknownObservationCategoryError,
    UnsupportedObservationVersionError,
    ictus_category_for,
    observation_from_failure,
    parse_observation,
    to_ictus_payload,
)

_OBSERVED_AT = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


def payload(**overrides: object) -> dict[str, object]:
    """A valid Ictus ExecutionObservation v1 payload (from the real v1 example)."""
    base: dict[str, object] = {
        "schema_version": 1,
        "observation_id": "obs-0001",
        "execution_id": "exec-0001",
        "intent_id": "intent:proposal-0001",
        "category": "WORKER_TIMEOUT",
        "message": "worker exceeded its timeout class",
        "evidence": [
            {"kind": "dagster_run", "uri": "dagster://runs/0001"},
        ],
        "observed_at": "2026-10-01T00:00:00Z",
        "retryable": True,
    }
    base.update(overrides)
    return base


# -- inbound category coverage --------------------------------------------


@pytest.mark.parametrize(
    "category",
    [
        "WORKER_TIMEOUT",
        "PROCESS_CRASH",
        "VERIFICATION_FAILURE",
        "INTEGRATION_CONFLICT",
        "RESOURCE_EXHAUSTED",
        "PROVIDER_UNAVAILABLE",
    ],
)
def test_known_ictus_category_is_normalized(category: str) -> None:
    observation = parse_observation(payload(category=category))

    assert observation.category is IctusObservationCategory(category)
    assert observation.schema_version == 1
    assert observation.observation_id == "obs-0001"
    assert observation.execution_id == "exec-0001"
    assert observation.intent_id == "intent:proposal-0001"
    assert observation.observed_at == _OBSERVED_AT


def test_success_category_is_valid() -> None:
    assert parse_observation(payload(category="SUCCESS")).category is (
        IctusObservationCategory.SUCCESS
    )


# -- conservative Tactus -> Ictus mapping ----------------------------------


def test_precise_tactus_classifications_map_to_ictus_equivalents() -> None:
    expected = {
        TactusFailureCategory.WORKER_TIMEOUT: IctusObservationCategory.WORKER_TIMEOUT,
        TactusFailureCategory.PROVIDER_UNAVAILABLE: (
            IctusObservationCategory.PROVIDER_UNAVAILABLE
        ),
        TactusFailureCategory.PROVIDER_QUOTA_EXHAUSTED: (
            IctusObservationCategory.RESOURCE_EXHAUSTED
        ),
        TactusFailureCategory.VERIFICATION_FAILURE: (
            IctusObservationCategory.VERIFICATION_FAILURE
        ),
        TactusFailureCategory.INTEGRATION_FAILURE: (
            IctusObservationCategory.INTEGRATION_CONFLICT
        ),
    }

    for failure, category in expected.items():
        assert ictus_category_for(failure) is category


@pytest.mark.parametrize(
    "failure",
    [
        TactusFailureCategory.TRANSIENT_RUNTIME_FAILURE,
        TactusFailureCategory.PROVIDER_API_TIMEOUT,
        TactusFailureCategory.EXECUTION_TIMEOUT,
        TactusFailureCategory.SCOPE_VIOLATION,
        TactusFailureCategory.DEPENDENCY_UNAVAILABLE,
        TactusFailureCategory.DEPENDENCY_RESOLVED,
        TactusFailureCategory.TASK_TOO_COMPLEX_SPLITTABLE,
        TactusFailureCategory.TASK_TOO_COMPLEX_NOT_SPLITTABLE,
        TactusFailureCategory.REQUEST_INVALID_OR_SUPERSEDED,
        TactusFailureCategory.UNKNOWN,
    ],
)
def test_conservative_classifications_map_to_unknown(
    failure: TactusFailureCategory,
) -> None:
    assert ictus_category_for(failure) is IctusObservationCategory.UNKNOWN


def test_every_tactus_failure_category_is_mapped() -> None:
    for failure in TactusFailureCategory:
        assert ictus_category_for(failure) in set(IctusObservationCategory)


# -- evidence / provenance preservation ------------------------------------


def test_evidence_is_preserved_and_round_trips() -> None:
    original = payload(
        evidence=[
            {
                "kind": "test_report",
                "uri": "file:///artifacts/report.xml",
                "sha256": "a" * 64,
                "note": "verifier output",
            }
        ]
    )

    observation = parse_observation(original)

    assert observation.evidence == (
        EvidenceRef(
            kind="test_report",
            uri="file:///artifacts/report.xml",
            sha256="a" * 64,
            note="verifier output",
        ),
    )

    serialized = to_ictus_payload(observation)
    assert serialized["evidence"] == original["evidence"]


def test_evidence_optional_sha256_and_note_are_omitted_when_absent() -> None:
    observation = parse_observation(
        payload(evidence=[{"kind": "log", "uri": "log://run/1"}])
    )

    assert observation.evidence == (
        EvidenceRef(kind="log", uri="log://run/1", sha256=None, note=None),
    )
    assert to_ictus_payload(observation)["evidence"] == [
        {"kind": "log", "uri": "log://run/1"}
    ]


def test_multiple_evidence_entries_preserve_order() -> None:
    observation = parse_observation(
        payload(
            evidence=[
                {"kind": "a", "uri": "u://a"},
                {"kind": "b", "uri": "u://b", "sha256": "b" * 64},
            ]
        )
    )

    assert [ref.kind for ref in observation.evidence] == ["a", "b"]


# -- retryable tri-state ---------------------------------------------------


@pytest.mark.parametrize(("value", "expected"), [(True, True), (False, False), (None, None)])
def test_retryable_tristate(value: bool | None, expected: bool | None) -> None:
    observation = parse_observation(payload(retryable=value))

    assert observation.retryable is expected
    if expected is None:
        assert "retryable" not in to_ictus_payload(observation)
    else:
        assert to_ictus_payload(observation)["retryable"] is expected


def test_missing_retryable_defaults_to_none() -> None:
    raw = payload()
    del raw["retryable"]

    assert parse_observation(raw).retryable is None


# -- fail-closed compatibility semantics -----------------------------------


def test_unsupported_schema_version_is_rejected() -> None:
    with pytest.raises(UnsupportedObservationVersionError):
        parse_observation(payload(schema_version=2))


def test_non_integer_schema_version_is_malformed() -> None:
    with pytest.raises(MalformedObservationError):
        parse_observation(payload(schema_version="1"))
    with pytest.raises(MalformedObservationError):
        parse_observation(payload(schema_version=True))


def test_unknown_incoming_category_is_rejected_not_coerced_to_unknown() -> None:
    with pytest.raises(UnknownObservationCategoryError):
        parse_observation(payload(category="TELEPORT_FAILURE"))


def test_missing_required_field_is_rejected() -> None:
    raw = payload()
    del raw["observation_id"]
    with pytest.raises(MalformedObservationError):
        parse_observation(raw)


def test_empty_required_field_is_rejected() -> None:
    with pytest.raises(MalformedObservationError):
        parse_observation(payload(intent_id="   "))


def test_invalid_observed_at_is_rejected() -> None:
    with pytest.raises(MalformedObservationError):
        parse_observation(payload(observed_at="not-a-date"))


def test_malformed_evidence_is_rejected() -> None:
    with pytest.raises(MalformedObservationError):
        parse_observation(payload(evidence="not-an-array"))
    with pytest.raises(MalformedObservationError):
        parse_observation(payload(evidence=[{"kind": "log"}]))
    with pytest.raises(MalformedObservationError):
        parse_observation(payload(evidence=[{"kind": "log", "uri": "u", "sha256": 5}]))


def test_payload_must_be_a_mapping() -> None:
    with pytest.raises(MalformedObservationError):
        parse_observation(["not", "a", "mapping"])  # type: ignore[arg-type]


# -- outbound origination --------------------------------------------------


def test_originate_from_tactus_failure_maps_category() -> None:
    observation = observation_from_failure(
        observation_id="obs-1",
        execution_id="exec-1",
        intent_id="intent-1",
        failure=TactusFailureCategory.VERIFICATION_FAILURE,
        observed_at=_OBSERVED_AT,
        message="verification failed",
        evidence=[EvidenceRef(kind="report", uri="file:///r.json")],
        retryable=False,
    )

    serialized = to_ictus_payload(observation)
    assert serialized["category"] == "VERIFICATION_FAILURE"
    assert serialized["schema_version"] == 1
    assert serialized["retryable"] is False
    assert serialized["observed_at"] == _OBSERVED_AT.isoformat()


def test_normalized_observation_round_trips_through_the_wire() -> None:
    original = payload()
    observation = parse_observation(original)

    assert parse_observation(to_ictus_payload(observation)) == observation


# -- purity / no lifecycle side effects ------------------------------------


def test_boundary_does_not_mutate_lifecycle_state() -> None:
    work_order = WorkOrder.create("WO-1")
    work_order.admit(reason="admitted")
    work_order.set_readiness(OpenStatus.READY)
    work_order.claim(reason="claimed")

    snapshot = (
        work_order.state,
        work_order.readiness,
        work_order.transitions,
        work_order.failure_observations,
    )

    raw = deepcopy(payload())
    observation = parse_observation(raw)
    to_ictus_payload(observation)
    observation_from_failure(
        observation_id="obs-2",
        execution_id="exec-2",
        intent_id="intent-2",
        failure=TactusFailureCategory.PROVIDER_UNAVAILABLE,
        observed_at=_OBSERVED_AT,
    )

    assert work_order.state is WorkOrderState.ACTIVE
    assert work_order.failure_observations == ()
    assert (
        work_order.state,
        work_order.readiness,
        work_order.transitions,
        work_order.failure_observations,
    ) == snapshot


def test_normalized_fact_is_immutable() -> None:
    observation = parse_observation(payload())

    with pytest.raises((AttributeError, TypeError)):
        observation.category = IctusObservationCategory.UNKNOWN  # type: ignore[misc]


def test_domain_failure_observation_is_untouched_by_the_boundary() -> None:
    # The boundary deliberately does not produce or mutate the domain entity;
    # this guards against accidental coupling.
    domain_observation = FailureObservation(summary="boom", observed_at=_OBSERVED_AT)
    parse_observation(payload())

    assert domain_observation.summary == "boom"
    assert domain_observation.observed_at == _OBSERVED_AT
