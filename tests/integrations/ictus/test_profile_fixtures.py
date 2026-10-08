"""Cross-repository pinned profile fixtures and boundary mismatch tests.

The fixtures under ``data/`` are the *shared* artifacts: they are produced from
the deterministic Tactus adapter with an explicit clock and ids, and an Ictus
boundary validates the same payloads at its schema/policy edge. Running them
through a boundary that mirrors the documented Ictus policy (``count < limit``)
also exposes the former ``retry.attempt``/``retry.budget`` mismatch: the legacy
defaults (missing -> ``0``) and the substitution of the execution-owned
step-retry index for the semantic count.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tactus.integrations.ictus import (
    FACT_RECOVERY_MAX_SEMANTIC_ATTEMPTS,
    FACT_RECOVERY_SEMANTIC_ATTEMPTS,
    FACT_SNAPSHOT_PHASE,
    StateSnapshotContractError,
    translate_legacy_budget_facts,
    validate_snapshot_profile,
)
from tactus.integrations.ictus.facts import (
    LEGACY_FACT_RETRY_ATTEMPT,
    LEGACY_FACT_RETRY_BUDGET,
)

import profile_fixtures as pf


def _load(name: str) -> dict[str, Any]:
    return json.loads((pf.DATA_DIR / name).read_text(encoding="utf-8"))


def _facts(payload: dict[str, Any]) -> dict[str, Any]:
    return {fact["key"]: fact["value"] for fact in payload["facts"]}


def _canonical_boundary_permits(payload: dict[str, Any]) -> bool:
    """Mirror the documented Ictus profile rule: permit while count < limit."""

    facts = _facts(payload)
    return facts[FACT_RECOVERY_SEMANTIC_ATTEMPTS] < facts[FACT_RECOVERY_MAX_SEMANTIC_ATTEMPTS]


def _legacy_boundary_permits(legacy_facts: dict[str, Any]) -> bool:
    """Mirror the superseded rule: read retry.attempt/retry.budget, default 0."""

    count, limit = translate_legacy_budget_facts(legacy_facts, default=0)
    return count < limit


# -- golden fixtures -------------------------------------------------------


def test_golden_initial_fixture_matches_the_adapter() -> None:
    fixture = _load("initial_snapshot.v2.json")

    assert fixture == pf.build_initial_snapshot()
    assert _facts(fixture)[FACT_SNAPSHOT_PHASE] == "INITIAL"


def test_golden_recovery_fixture_matches_the_adapter() -> None:
    fixture = _load("recovery_snapshot.v2.json")

    assert fixture == pf.build_recovery_snapshot()
    assert _facts(fixture)[FACT_SNAPSHOT_PHASE] == "RECOVERY"


def test_golden_fixtures_validate_at_the_shared_profile_boundary() -> None:
    validate_snapshot_profile(_load("initial_snapshot.v2.json"))
    validate_snapshot_profile(_load("recovery_snapshot.v2.json"))


def test_golden_fixtures_are_reproducible_and_digest_pinned() -> None:
    initial = _load("initial_snapshot.v2.json")
    recovery = _load("recovery_snapshot.v2.json")

    assert _facts(initial)["snapshot.digest"] == _facts(pf.build_initial_snapshot())["snapshot.digest"]
    assert _facts(recovery)["snapshot.digest"] == _facts(pf.build_recovery_snapshot())["snapshot.digest"]


# -- the former retry.attempt/retry.budget mismatch ------------------------


def test_shared_fixture_exposes_exhausted_budget_mismatch() -> None:
    """Canonical exhaustion must not be masked by the step-retry diagnostic."""

    # Recovery snapshot with the semantic allowance exhausted (3 of 3).
    exhausted = pf.build_recovery_snapshot()
    exhausted_facts = _facts(exhausted)
    exhausted_facts[FACT_RECOVERY_SEMANTIC_ATTEMPTS] = 3
    exhausted_facts[FACT_RECOVERY_MAX_SEMANTIC_ATTEMPTS] = 3

    canonical = dict(exhausted)
    canonical["facts"] = [
        {"key": key, "value": value} for key, value in exhausted_facts.items()
    ]

    # The legacy boundary reads the Dagster step-retry index (0 here) as if it
    # were the semantic attempt count against a budget of 3 -> wrongly permits.
    legacy_facts = {LEGACY_FACT_RETRY_ATTEMPT: 0, LEGACY_FACT_RETRY_BUDGET: 3}

    assert _canonical_boundary_permits(canonical) is False
    assert _legacy_boundary_permits(legacy_facts) is True


def test_shared_fixture_exposes_missing_budget_mismatch_for_initial() -> None:
    """Initial context must permit execution; legacy 0/0 defaults do not."""

    initial = pf.build_initial_snapshot()

    assert _canonical_boundary_permits(initial) is True
    # Missing legacy facts default to 0/0, which yields "no attempt permitted".
    assert _legacy_boundary_permits({}) is False


# -- explicit legacy translation semantics ---------------------------------


def test_translate_legacy_budget_facts_reads_explicit_values() -> None:
    assert translate_legacy_budget_facts(
        {LEGACY_FACT_RETRY_ATTEMPT: 2, LEGACY_FACT_RETRY_BUDGET: 5}
    ) == (2, 5)


def test_translate_legacy_budget_facts_defaults_missing_to_zero() -> None:
    assert translate_legacy_budget_facts({}) == (0, 0)


def test_translate_legacy_budget_facts_ignores_bool_and_non_int() -> None:
    assert translate_legacy_budget_facts(
        {LEGACY_FACT_RETRY_ATTEMPT: True, LEGACY_FACT_RETRY_BUDGET: "3"}
    ) == (0, 0)


def test_translate_legacy_budget_facts_honours_default() -> None:
    assert translate_legacy_budget_facts({}, default=1) == (1, 1)


def test_translate_legacy_budget_facts_rejects_non_mapping() -> None:
    with pytest.raises(StateSnapshotContractError):
        translate_legacy_budget_facts("nope")  # type: ignore[arg-type]
