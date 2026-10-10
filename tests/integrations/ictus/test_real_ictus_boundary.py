"""Real pinned-Ictus boundary conformance for the Tactus snapshot fixtures.

These tests execute the **actual** pinned Ictus binary (``ictus decide``) over
the deterministic Tactus initial/recovery fixtures. They do not evaluate a
Python mirror of Ictus policy: the decision in every assertion below comes from
the compiled Ictus ``RuleDecisionProvider``/``DefaultPolicyEvaluator`` at the
reviewed revision.

When the Ictus checkout is missing or not at the pinned revision the tests
**fail** (never skip), because a silent skip would hide the cross-repository
drift this conformance exists to catch. The harness builds into a disposable
target directory and never mutates either canonical checkout.

Original issue-7 criteria proven here:

* initial snapshots require no fabricated observation;
* canonical semantic count/limit at zero, one-remaining and exhausted;
* step-retry remains a diagnostic that cannot substitute for the semantic count;
* the shared fixtures run through the real Ictus boundary and expose the former
  ``retry.attempt``/``retry.budget`` mismatch.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tactus.integrations.ictus import (
    FACT_EXECUTION_STEP_RETRY_INDEX,
    FACT_OBSERVATION_CATEGORY,
    FACT_OBSERVATION_EXECUTION_ID,
    FACT_OBSERVATION_INTENT_ID,
    FACT_RECOVERY_ATTEMPT_ID,
    FACT_RECOVERY_INTENT_ID,
    FACT_RECOVERY_MAX_SEMANTIC_ATTEMPTS,
    FACT_RECOVERY_SEMANTIC_ATTEMPTS,
    FACT_SNAPSHOT_DIGEST,
    FACT_SNAPSHOT_PHASE,
    FACT_WORK_ORDER_ID,
    SemanticBudget,
    SnapshotPhase,
    StateSnapshotContractError,
    StepRetryDiagnostic,
    build_state_snapshot,
    compute_snapshot_digest,
    to_ictus_legacy_boundary_payload,
    validate_snapshot_profile,
)
from tactus.integrations.ictus.facts import (
    LEGACY_FACT_RETRY_ATTEMPT,
    LEGACY_FACT_RETRY_BUDGET,
)

import profile_fixtures as pf

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HARNESS_PATH = _REPO_ROOT / "integrations" / "ictus" / "conformance.py"
_SPEC = importlib.util.spec_from_file_location("ictus_conformance", _HARNESS_PATH)
assert _SPEC is not None and _SPEC.loader is not None
conformance = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = conformance
_SPEC.loader.exec_module(conformance)


# -- helpers ---------------------------------------------------------------


def _facts(payload: dict[str, Any]) -> dict[str, Any]:
    return {fact["key"]: fact["value"] for fact in payload["facts"]}


def _with_budget(payload: dict[str, Any], attempts: int, limit: int) -> dict[str, Any]:
    """Return a profile-valid copy with the canonical budget replaced."""

    facts = [dict(fact) for fact in payload["facts"]]
    for fact in facts:
        if fact["key"] == FACT_RECOVERY_SEMANTIC_ATTEMPTS:
            fact["value"] = attempts
        elif fact["key"] == FACT_RECOVERY_MAX_SEMANTIC_ATTEMPTS:
            fact["value"] = limit
    body = dict(payload)
    body["facts"] = [fact for fact in facts if fact["key"] != FACT_SNAPSHOT_DIGEST]
    digest = compute_snapshot_digest(body)
    for fact in facts:
        if fact["key"] == FACT_SNAPSHOT_DIGEST:
            fact["value"] = digest
    updated = dict(payload)
    updated["facts"] = facts
    return updated


def _git_status(root: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


# -- fixtures --------------------------------------------------------------


@pytest.fixture(scope="session")
def ictus_boundary(tmp_path_factory: pytest.TempPathFactory) -> conformance.IctusBoundary:
    """Resolve, pin-verify and build the real Ictus boundary (session-scoped)."""

    reusable = os.environ.get(conformance.ICTUS_TARGET_ENV)
    target = Path(reusable) if reusable else tmp_path_factory.mktemp("ictus-build")
    return conformance.load_boundary(target_dir=target)


# -- pin / availability ----------------------------------------------------


def test_reviewed_ictus_revision_is_pinned(ictus_boundary: conformance.IctusBoundary) -> None:
    assert ictus_boundary.commit == conformance.PINNED_ICTUS_COMMIT
    assert conformance.PINNED_ICTUS_COMMIT == "e823000c9255b959c2c9516ed4ee85eb13150df2"
    assert ictus_boundary.binary.is_file()


def test_missing_ictus_checkout_fails_visibly(tmp_path: Path) -> None:
    not_a_checkout = tmp_path / "not-ictus"
    not_a_checkout.mkdir()

    with pytest.raises(conformance.IctusBoundaryUnavailable):
        conformance.resolve_ictus_root(not_a_checkout)
    with pytest.raises(conformance.IctusBoundaryUnavailable):
        conformance.verify_pinned_ictus(not_a_checkout)


def test_off_pin_ictus_checkout_fails_visibly(tmp_path: Path) -> None:
    repo = tmp_path / "ictus"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "README.md").write_text("off-pin\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env={**env})
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "off-pin"],
        check=True,
        env=env,
    )

    with pytest.raises(conformance.IctusPinMismatch):
        conformance.verify_pinned_ictus(repo)


# -- initial fixture -------------------------------------------------------


def test_initial_fixture_has_no_fabricated_failure(ictus_boundary: conformance.IctusBoundary) -> None:
    payload = pf.build_initial_snapshot()
    validate_snapshot_profile(payload)
    assert _facts(payload)[FACT_SNAPSHOT_PHASE] == "INITIAL"
    assert FACT_OBSERVATION_CATEGORY not in _facts(payload)

    outcome = ictus_boundary.decide(to_ictus_legacy_boundary_payload(payload))

    # The real boundary accepted and decided on the observation-free context.
    assert outcome.returncode in {0, 3}
    assert outcome.proposal_decision
    assert outcome.subject_id == payload["subject"]["id"]
    assert outcome.subject_type == payload["subject"]["type"]


def test_initial_phase_rejects_a_fabricated_observation() -> None:
    with pytest.raises(StateSnapshotContractError):
        build_state_snapshot(
            work_order=pf.active_work_order(),
            phase=SnapshotPhase.INITIAL,
            observation=pf.sample_observation(),
            semantic_budget=SemanticBudget(semantic_attempts=0, max_semantic_attempts=3),
        )


@pytest.mark.parametrize(
    ("name", "decision"),
    [
        ("initial_snapshot.v2.json", "ESCALATE"),
        ("recovery_snapshot.v2.json", "REEXECUTE"),
    ],
)
def test_shared_golden_fixtures_run_through_real_boundary(
    ictus_boundary: conformance.IctusBoundary, name: str, decision: str
) -> None:
    """The checked-in shared fixtures are decided by the real binary."""

    payload = json.loads((pf.DATA_DIR / name).read_text(encoding="utf-8"))
    validate_snapshot_profile(payload)

    outcome = ictus_boundary.decide(to_ictus_legacy_boundary_payload(payload))

    assert outcome.proposal_decision == decision


# -- recovery fixture and binding -----------------------------------------


def test_recovery_fixture_binds_subject_intent_and_attempt(
    ictus_boundary: conformance.IctusBoundary,
) -> None:
    payload = pf.build_recovery_snapshot()
    validate_snapshot_profile(payload)
    facts = _facts(payload)

    assert payload["subject"] == {"type": "work_order", "id": "WO-1"}
    assert facts[FACT_WORK_ORDER_ID] == payload["subject"]["id"]
    assert facts[FACT_RECOVERY_INTENT_ID] == "intent-0001"
    assert facts[FACT_RECOVERY_INTENT_ID] == facts[FACT_OBSERVATION_INTENT_ID]
    assert facts[FACT_RECOVERY_ATTEMPT_ID] == "attempt-0001"
    assert facts[FACT_OBSERVATION_EXECUTION_ID] == "exec-0001"

    outcome = ictus_boundary.decide(to_ictus_legacy_boundary_payload(payload))

    assert outcome.proposal_decision == "REEXECUTE"
    assert outcome.policy_decision_kind == "ALLOW"
    assert outcome.executable
    assert outcome.subject_id == "WO-1"
    assert "(1/3)" in outcome.reason


@pytest.mark.parametrize(
    "attempt_kwargs",
    [
        {"intent_id": "intent-9999"},
        {"work_order_id": "WO-other"},
        {"dagster_run_id": "exec-other"},
    ],
)
def test_recovery_binding_mismatch_is_denied(attempt_kwargs: dict[str, str]) -> None:
    with pytest.raises(StateSnapshotContractError):
        build_state_snapshot(
            work_order=pf.active_work_order(),
            phase=SnapshotPhase.RECOVERY,
            observation=pf.sample_observation(),
            attempt=pf.sample_attempt(**attempt_kwargs),
            semantic_budget=SemanticBudget(semantic_attempts=1, max_semantic_attempts=3),
            step_retry=StepRetryDiagnostic(index=2),
        )


# -- canonical semantic count/limit through the real boundary --------------


@pytest.mark.parametrize(
    ("attempts", "limit", "expected"),
    [
        (0, 3, "REEXECUTE"),  # zero attempts recorded
        (2, 3, "REEXECUTE"),  # exactly one remaining
        (3, 3, "ESCALATE"),   # exhausted
    ],
)
def test_canonical_budget_matrix_through_real_boundary(
    ictus_boundary: conformance.IctusBoundary,
    attempts: int,
    limit: int,
    expected: str,
) -> None:
    payload = _with_budget(pf.build_recovery_snapshot(), attempts, limit)
    validate_snapshot_profile(payload)

    outcome = ictus_boundary.decide(to_ictus_legacy_boundary_payload(payload))

    assert outcome.proposal_decision == expected
    assert outcome.policy_decision_kind == "ALLOW"
    assert f"({attempts}/{limit})" in outcome.reason


@pytest.mark.parametrize("step_retry_index", [0, 5])
@pytest.mark.parametrize(
    ("attempts", "limit", "expected"),
    [(0, 3, "REEXECUTE"), (3, 3, "ESCALATE")],
)
def test_step_retry_is_diagnostic_only(
    ictus_boundary: conformance.IctusBoundary,
    attempts: int,
    limit: int,
    expected: str,
    step_retry_index: int,
) -> None:
    payload = _with_budget(pf.build_recovery_snapshot(), attempts, limit)
    for fact in payload["facts"]:
        if fact["key"] == FACT_EXECUTION_STEP_RETRY_INDEX:
            fact["value"] = step_retry_index

    outcome = ictus_boundary.decide(to_ictus_legacy_boundary_payload(payload))

    # The execution-owned step-retry index never changes the semantic verdict.
    assert outcome.proposal_decision == expected


# -- fail-closed projection ------------------------------------------------


@pytest.mark.parametrize("bad_value", [True, False, -1, "2", 1.5, None])
@pytest.mark.parametrize(
    "key",
    [FACT_RECOVERY_SEMANTIC_ATTEMPTS, FACT_RECOVERY_MAX_SEMANTIC_ATTEMPTS],
)
def test_projection_rejects_missing_bool_negative_values(
    key: str, bad_value: object
) -> None:
    payload = pf.build_recovery_snapshot()
    payload["facts"] = [
        {"key": fact["key"], "value": bad_value} if fact["key"] == key else fact
        for fact in payload["facts"]
    ]

    with pytest.raises(StateSnapshotContractError):
        to_ictus_legacy_boundary_payload(payload)


def test_projection_rejects_missing_canonical_budget() -> None:
    payload = pf.build_recovery_snapshot()
    payload["facts"] = [
        fact
        for fact in payload["facts"]
        if fact["key"] != FACT_RECOVERY_MAX_SEMANTIC_ATTEMPTS
    ]

    with pytest.raises(StateSnapshotContractError):
        to_ictus_legacy_boundary_payload(payload)


def test_projection_rejects_inconsistent_budget() -> None:
    payload = _with_budget(pf.build_recovery_snapshot(), 4, 3)

    with pytest.raises(StateSnapshotContractError):
        to_ictus_legacy_boundary_payload(payload)


def test_projection_rejects_an_existing_legacy_fact() -> None:
    payload = pf.build_recovery_snapshot()
    payload["facts"] = [
        *payload["facts"],
        {"key": LEGACY_FACT_RETRY_ATTEMPT, "value": 1},
    ]

    with pytest.raises(StateSnapshotContractError):
        to_ictus_legacy_boundary_payload(payload)


def test_projection_is_additive_and_preserves_canonical_body() -> None:
    payload = pf.build_recovery_snapshot()
    projected = to_ictus_legacy_boundary_payload(payload)
    projected_facts = _facts(projected)

    assert projected_facts[LEGACY_FACT_RETRY_ATTEMPT] == 1
    assert projected_facts[LEGACY_FACT_RETRY_BUDGET] == 3
    assert _facts(payload) == {
        key: value for key, value in projected_facts.items()
        if key not in {LEGACY_FACT_RETRY_ATTEMPT, LEGACY_FACT_RETRY_BUDGET}
    }
    assert projected_facts[FACT_SNAPSHOT_DIGEST] == _facts(payload)[FACT_SNAPSHOT_DIGEST]


# -- the former retry.attempt/retry.budget mismatch, through real Ictus ----


def test_former_missing_budget_mismatch_is_exposed_by_real_boundary(
    ictus_boundary: conformance.IctusBoundary,
) -> None:
    """Canonical-only facts leave Ictus defaulting retry.* to 0/0."""

    payload = _with_budget(pf.build_recovery_snapshot(), 1, 3)

    # Real boundary reading only the canonical facts: legacy retry.* default 0/0.
    unprojected = ictus_boundary.decide(payload)
    assert unprojected.proposal_decision == "ESCALATE"
    assert "(0/0)" in unprojected.reason

    # The explicit compatibility projection repairs it against the same binary.
    projected = ictus_boundary.decide(to_ictus_legacy_boundary_payload(payload))
    assert projected.proposal_decision == "REEXECUTE"
    assert "(1/3)" in projected.reason


def test_former_step_retry_substitution_mismatch_is_exposed_by_real_boundary(
    ictus_boundary: conformance.IctusBoundary,
) -> None:
    """Substituting the step-retry index for the semantic count wrongly permits."""

    exhausted = _with_budget(pf.build_recovery_snapshot(), 3, 3)
    step_retry_index = _facts(exhausted)[FACT_EXECUTION_STEP_RETRY_INDEX]
    assert step_retry_index == 2

    # Legacy substitution: retry.attempt <- step-retry index (2) with budget 3.
    substituted = dict(exhausted)
    substituted["facts"] = [
        *exhausted["facts"],
        {"key": LEGACY_FACT_RETRY_ATTEMPT, "value": step_retry_index},
        {"key": LEGACY_FACT_RETRY_BUDGET, "value": 3},
    ]
    wrong = ictus_boundary.decide(substituted)
    assert wrong.proposal_decision == "REEXECUTE"

    # The canonical projection keeps the step retry diagnostic: exhausted stays
    # exhausted.
    right = ictus_boundary.decide(to_ictus_legacy_boundary_payload(exhausted))
    assert right.proposal_decision == "ESCALATE"
    assert "(3/3)" in right.reason


# -- disposable state ------------------------------------------------------


def test_conformance_does_not_dirty_the_ictus_checkout(
    ictus_boundary: conformance.IctusBoundary,
) -> None:
    ictus_boundary.decide(
        to_ictus_legacy_boundary_payload(pf.build_recovery_snapshot())
    )
    assert _git_status(ictus_boundary.root) == ""


def test_fixture_payloads_are_deterministic() -> None:
    assert json.dumps(pf.build_initial_snapshot(), sort_keys=True) == json.dumps(
        pf.build_initial_snapshot(), sort_keys=True
    )
    assert json.dumps(pf.build_recovery_snapshot(), sort_keys=True) == json.dumps(
        pf.build_recovery_snapshot(), sort_keys=True
    )
