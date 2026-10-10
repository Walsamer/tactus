"""Cross-repository pinned profile fixtures.

The fixtures under ``data/`` are the *shared* artifacts: they are produced from
the deterministic Tactus adapter with an explicit clock and ids. The
cross-repository conformance proof that executes the **actual** pinned Ictus
boundary (and exposes the former ``retry.attempt``/``retry.budget`` mismatch)
lives in :mod:`test_real_ictus_boundary`; no Python policy mirror substitutes
for it here.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tactus.integrations.ictus import (
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