"""Inbound Ictus ``ExecutionObservation`` v1 boundary tests.

These cover the inbound contract only: validation, fail-closed handling,
evidence/provenance preservation and purity. There are deliberately no
taxonomy-mapping, outbound-origination or wire round-trip tests: the boundary
does not perform those translations.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

import tactus.integrations.ictus as ictus_boundary
from tactus.domain import FailureObservation, OpenStatus, WorkOrder, WorkOrderState
from tactus.integrations.ictus import (
    EvidenceRef,
    IctusObservationCategory,
    MalformedObservationError,
    TactusObservation,
    UnknownObservationCategoryError,
    UnsupportedObservationVersionError,
    parse_observation,
)

_OBSERVED_AT = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


def payload(**overrides: object) -> dict[str, object]:
    """A valid Ictus ``ExecutionObservation`` v1 payload (from the v1 example)."""

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


# -- inbound normalization -------------------------------------------------


def test_minimal_payload_normalizes_with_optional_fields_absent() -> None:
    raw = {
        "schema_version": 1,
        "observation_id": "obs-0001",
        "execution_id": "exec-0001",
        "intent_id": "intent:proposal-0001",
        "category": "SUCCESS",
        "observed_at": "2026-10-01T00:00:00Z",
    }

    observation = parse_observation(raw)

    assert observation == TactusObservation(
        observation_id="obs-0001",
        execution_id="exec-0001",
        intent_id="intent:proposal-0001",
        category=IctusObservationCategory.SUCCESS,
        observed_at=_OBSERVED_AT,
        message=None,
        evidence=(),
        retryable=None,
        schema_version=1,
    )


def test_full_payload_normalizes_all_fields() -> None:
    observation = parse_observation(payload())

    assert observation.observation_id == "obs-0001"
    assert observation.execution_id == "exec-0001"
    assert observation.intent_id == "intent:proposal-0001"
    assert observation.category is IctusObservationCategory.WORKER_TIMEOUT
    assert observation.observed_at == _OBSERVED_AT
    assert observation.message == "worker exceeded its timeout class"
    assert observation.schema_version == 1


@pytest.mark.parametrize("category", [member.value for member in IctusObservationCategory])
def test_every_v1_category_is_accepted(category: str) -> None:
    observation = parse_observation(payload(category=category))

    assert observation.category is IctusObservationCategory(category)


@pytest.mark.parametrize("value,expected", [(True, True), (False, False)])
def test_retryable_boolean_is_preserved(value: bool, expected: bool) -> None:
    assert parse_observation(payload(retryable=value)).retryable is expected


# -- evidence / provenance preservation ------------------------------------


def test_evidence_is_preserved_with_full_provenance() -> None:
    observation = parse_observation(
        payload(
            evidence=[
                {
                    "kind": "test_report",
                    "uri": "file:///artifacts/report.xml",
                    "sha256": "a" * 64,
                    "note": "verifier output",
                }
            ]
        )
    )

    assert observation.evidence == (
        EvidenceRef(
            kind="test_report",
            uri="file:///artifacts/report.xml",
            sha256="a" * 64,
            note="verifier output",
        ),
    )


def test_evidence_optional_provenance_fields_default_to_none() -> None:
    observation = parse_observation(
        payload(evidence=[{"kind": "log", "uri": "log://run/1"}])
    )

    assert observation.evidence == (
        EvidenceRef(kind="log", uri="log://run/1", sha256=None, note=None),
    )


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


# -- fail-closed: version --------------------------------------------------


def test_unsupported_schema_version_is_rejected() -> None:
    with pytest.raises(UnsupportedObservationVersionError):
        parse_observation(payload(schema_version=2))


@pytest.mark.parametrize("version", [None, "1", 1.0, True, False])
def test_non_integer_or_missing_schema_version_is_malformed(version: object) -> None:
    raw = payload()
    raw["schema_version"] = version
    with pytest.raises(MalformedObservationError):
        parse_observation(raw)


def test_missing_schema_version_is_malformed() -> None:
    raw = payload()
    del raw["schema_version"]
    with pytest.raises(MalformedObservationError):
        parse_observation(raw)


# -- fail-closed: category -------------------------------------------------


def test_unknown_incoming_category_fails_closed_not_coerced_to_unknown() -> None:
    with pytest.raises(UnknownObservationCategoryError):
        parse_observation(payload(category="TELEPORT_FAILURE"))


@pytest.mark.parametrize("category", [None, 7, ["WORKER_TIMEOUT"]])
def test_non_string_category_is_malformed(category: object) -> None:
    with pytest.raises(MalformedObservationError):
        parse_observation(payload(category=category))


# -- fail-closed: required fields ------------------------------------------


@pytest.mark.parametrize("field_name", ["observation_id", "execution_id", "intent_id"])
def test_missing_required_id_is_malformed(field_name: str) -> None:
    raw = payload()
    del raw[field_name]
    with pytest.raises(MalformedObservationError):
        parse_observation(raw)


@pytest.mark.parametrize("field_name", ["observation_id", "execution_id", "intent_id"])
def test_empty_required_id_is_malformed(field_name: str) -> None:
    with pytest.raises(MalformedObservationError):
        parse_observation(payload(**{field_name: "   "}))


def test_missing_observed_at_is_malformed() -> None:
    raw = payload()
    del raw["observed_at"]
    with pytest.raises(MalformedObservationError):
        parse_observation(raw)


@pytest.mark.parametrize("observed_at", [None, "", "not-a-date", 123])
def test_invalid_observed_at_is_malformed(observed_at: object) -> None:
    with pytest.raises(MalformedObservationError):
        parse_observation(payload(observed_at=observed_at))


def test_non_mapping_payload_is_malformed() -> None:
    with pytest.raises(MalformedObservationError):
        parse_observation(["not", "a", "mapping"])  # type: ignore[arg-type]


# -- fail-closed: evidence shape -------------------------------------------


@pytest.mark.parametrize("evidence", ["not-an-array", {"kind": "log"}, None])
def test_malformed_evidence_container_is_rejected(evidence: object) -> None:
    with pytest.raises(MalformedObservationError):
        parse_observation(payload(evidence=evidence))


def test_evidence_entry_requires_kind_and_uri() -> None:
    with pytest.raises(MalformedObservationError):
        parse_observation(payload(evidence=[{"kind": "log"}]))
    with pytest.raises(MalformedObservationError):
        parse_observation(payload(evidence=[{"uri": "u://x"}]))


def test_evidence_entries_must_be_objects() -> None:
    with pytest.raises(MalformedObservationError):
        parse_observation(payload(evidence=["not-an-object"]))


# -- authoritative nullable-field handling --------------------------------
#
# Ictus ``observation.schema.json`` declares ``message`` (string), ``evidence``
# (array) and ``retryable`` (boolean) as optional but never nullable, and the
# evidence ``sha256``/``note`` fields likewise. Optional therefore means
# "omitted", not "explicit null". Tactus is a strict fail-closed validator and
# rejects a ``null`` the authoritative schema rejects.


@pytest.mark.parametrize("field_name", ["message", "evidence", "retryable"])
def test_explicit_null_optional_field_is_malformed(field_name: str) -> None:
    with pytest.raises(MalformedObservationError):
        parse_observation(payload(**{field_name: None}))


@pytest.mark.parametrize("field_name", ["sha256", "note"])
def test_explicit_null_evidence_field_is_malformed(field_name: str) -> None:
    evidence = [{"kind": "log", "uri": "u://x", field_name: None}]
    with pytest.raises(MalformedObservationError):
        parse_observation(payload(evidence=evidence))


@pytest.mark.parametrize(
    "field_name,value",
    [("message", 5), ("retryable", 1), ("retryable", "yes")],
)
def test_wrong_type_optional_field_is_malformed(field_name: str, value: object) -> None:
    with pytest.raises(MalformedObservationError):
        parse_observation(payload(**{field_name: value}))


def test_wrong_type_evidence_provenance_field_is_malformed() -> None:
    with pytest.raises(MalformedObservationError):
        parse_observation(payload(evidence=[{"kind": "log", "uri": "u", "sha256": 5}]))


# -- boundary shape: inbound only, no taxonomy / round-trip ----------------
#
# Acceptance criteria: no Tactus-wide failure taxonomy mapped to Ictus
# categories, and no Ictus -> Tactus -> Ictus observation round-trip. Guard the
# removal so the outbound translation cannot creep back in.


@pytest.mark.parametrize(
    "removed_name",
    [
        "TactusFailureCategory",
        "TACTUS_TO_ICTUS_CATEGORY",
        "ictus_category_for",
        "observation_from_failure",
        "to_ictus_payload",
    ],
)
def test_outbound_translation_surface_is_absent(removed_name: str) -> None:
    assert not hasattr(ictus_boundary, removed_name)


# -- purity / no lifecycle side effects ------------------------------------


def test_boundary_does_not_mutate_work_order_lifecycle_state() -> None:
    work_order = WorkOrder.create("WO-1")
    work_order.admit(reason="admitted")
    work_order.set_readiness(OpenStatus.READY)
    work_order.claim(reason="claimed")

    before = (
        work_order.state,
        work_order.readiness,
        work_order.transitions,
        work_order.failure_observations,
    )

    parse_observation(deepcopy(payload()))

    assert work_order.state is WorkOrderState.ACTIVE
    assert work_order.failure_observations == ()
    assert (
        work_order.state,
        work_order.readiness,
        work_order.transitions,
        work_order.failure_observations,
    ) == before


def test_normalized_fact_is_immutable() -> None:
    observation = parse_observation(payload())

    with pytest.raises((AttributeError, TypeError)):
        observation.category = IctusObservationCategory.UNKNOWN  # type: ignore[misc]


def test_domain_failure_observation_is_not_produced_by_the_boundary() -> None:
    # The boundary validates/normalizes its own fact; it deliberately does not
    # create or mutate the Tactus domain entity.
    domain_observation = FailureObservation(summary="boom", observed_at=_OBSERVED_AT)
    result = parse_observation(payload())

    assert isinstance(result, TactusObservation)
    assert not isinstance(result, FailureObservation)
    assert domain_observation.summary == "boom"
    assert domain_observation.observed_at == _OBSERVED_AT
