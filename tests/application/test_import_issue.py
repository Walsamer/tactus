"""Application-level import and recheck tests (issue #21).

Exercise the end-to-end successor WorkSource path: mocked GitHub input, the
authorization gate, durable provenance mapping, idempotent import, distinct
identities for edited intent and typed claim/dispatch holds.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from tactus import cli
from tactus.application import apply_source_holds, import_issue, recheck_source
from tactus.domain import (
    IllegalTransitionError,
    ImmutableSourceConflictError,
    MissingReadyLabelError,
    OpenStatus,
    SourceHoldReason,
)
from tactus.integrations.github import MockGitHubIssueSource
from tactus.persistence import SqliteWorkSource

NOW = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
REPOSITORY = "walsamer/tactus"


def payload(
    *,
    body: str = "## Objective\n\nImmutable provenance.\n",
    labels: tuple[str, ...] = ("area:control-plane", "fleet:ready"),
    state: str = "open",
    updated_at: str = "2026-10-10T14:35:38Z",
) -> dict:
    return {
        "repository": {
            "id": 1404239931,
            "owner": "walsamer",
            "name": "tactus",
            "url": "https://github.com/walsamer/tactus",
        },
        "issue": {
            "number": 21,
            "url": "https://github.com/walsamer/tactus/issues/21",
            "title": "Ingest a checked GitHub Issue revision into a Tactus WorkOrder",
            "body": body,
            "state": state,
            "updated_at": updated_at,
            "labels": list(labels),
            "dependencies": ["https://github.com/walsamer/tactus/issues/3"],
            "author": "walsamer",
            "milestone": None,
        },
    }


def source(payloads: dict) -> MockGitHubIssueSource:
    return MockGitHubIssueSource(payloads, clock=lambda: NOW)


def default_source() -> MockGitHubIssueSource:
    return source({(REPOSITORY, 21): payload()})


# -- import ---------------------------------------------------------------


def test_import_creates_open_work_order_with_immutable_provenance(tmp_path) -> None:
    adapter = SqliteWorkSource(tmp_path / "work.sqlite")
    result = import_issue(default_source(), adapter, REPOSITORY, 21, imported_at=NOW)
    adapter.close()

    assert result.created is True
    assert result.work_order.state.value == "OPEN"
    assert result.work_order.readiness is OpenStatus.UNKNOWN
    provenance = result.provenance
    assert provenance.import_key.value.startswith("walsamer/tactus#21@")
    assert provenance.revision.body == payload()["issue"]["body"]
    assert provenance.revision.body_sha256
    assert provenance.revision.specification_digest
    assert provenance.imported_at == NOW
    assert provenance.parent_work_order_id is None


def test_reimport_same_revision_is_idempotent(tmp_path) -> None:
    adapter = SqliteWorkSource(tmp_path / "work.sqlite")
    first = import_issue(default_source(), adapter, REPOSITORY, 21, imported_at=NOW)
    second = import_issue(default_source(), adapter, REPOSITORY, 21, imported_at=NOW)
    adapter.close()

    assert first.created is True
    assert second.created is False
    assert first.work_order_id == second.work_order_id
    assert first.provenance.import_key == second.provenance.import_key


def test_edited_source_yields_distinct_work_order_and_preserves_original(tmp_path) -> None:
    adapter = SqliteWorkSource(tmp_path / "work.sqlite")
    original = import_issue(default_source(), adapter, REPOSITORY, 21, imported_at=NOW)
    edited_source = source({(REPOSITORY, 21): payload(body="## edited\n")})
    edited = import_issue(edited_source, adapter, REPOSITORY, 21, imported_at=NOW)

    assert edited.created is True
    assert edited.work_order_id != original.work_order_id
    # The original WorkOrder keeps its original revision: no silent rewrite.
    preserved = adapter.provenance_for(original.work_order_id)
    assert preserved.import_key == original.provenance.import_key
    assert preserved.revision.body != "## edited\n"
    adapter.close()


def test_explicit_conflicting_work_order_id_is_rejected(tmp_path) -> None:
    adapter = SqliteWorkSource(tmp_path / "work.sqlite")
    import_issue(default_source(), adapter, REPOSITORY, 21, work_order_id="WO-1", imported_at=NOW)
    edited_source = source({(REPOSITORY, 21): payload(body="## edited\n")})
    with pytest.raises(ImmutableSourceConflictError):
        import_issue(edited_source, adapter, REPOSITORY, 21, work_order_id="WO-1", imported_at=NOW)
    adapter.close()


def test_import_rejects_unready_issue(tmp_path) -> None:
    adapter = SqliteWorkSource(tmp_path / "work.sqlite")
    unready = source({(REPOSITORY, 21): payload(labels=("area:control-plane",))})
    with pytest.raises(MissingReadyLabelError):
        import_issue(unready, adapter, REPOSITORY, 21, imported_at=NOW)
    adapter.close()


# -- claim / dispatch recheck --------------------------------------------


def test_recheck_after_source_edit_returns_source_changed_hold(tmp_path) -> None:
    adapter = SqliteWorkSource(tmp_path / "work.sqlite")
    result = import_issue(default_source(), adapter, REPOSITORY, 21, imported_at=NOW)
    edited_source = source({(REPOSITORY, 21): payload(body="## edited\n")})

    holds = recheck_source(edited_source, adapter, result.work_order_id, at=NOW)
    adapter.close()

    assert [hold.reason for hold in holds] == [SourceHoldReason.SOURCE_CHANGED]


def test_recheck_unavailable_source_returns_typed_hold(tmp_path) -> None:
    adapter = SqliteWorkSource(tmp_path / "work.sqlite")
    result = import_issue(default_source(), adapter, REPOSITORY, 21, imported_at=NOW)
    unavailable = MockGitHubIssueSource({}, clock=lambda: NOW)

    holds = recheck_source(unavailable, adapter, result.work_order_id, at=NOW)
    adapter.close()

    assert [hold.reason for hold in holds] == [SourceHoldReason.SOURCE_UNAVAILABLE]


def test_recheck_holds_block_claim(tmp_path) -> None:
    adapter = SqliteWorkSource(tmp_path / "work.sqlite")
    result = import_issue(default_source(), adapter, REPOSITORY, 21, imported_at=NOW)
    work_order = result.work_order
    work_order.set_readiness(OpenStatus.READY)

    edited_source = source({(REPOSITORY, 21): payload(body="## edited\n")})
    holds = recheck_source(edited_source, adapter, result.work_order_id, at=NOW)
    apply_source_holds(work_order, holds)
    adapter.close()

    assert work_order.readiness is OpenStatus.BLOCKED
    with pytest.raises(IllegalTransitionError):
        work_order.claim(reason="dispatch")


# -- explicit local command ----------------------------------------------


def test_cli_import_issue_writes_database(tmp_path, capsys) -> None:
    from pathlib import Path

    fixture = Path(__file__).parents[1] / "integrations" / "github" / "data" / "issue_21.json"
    database = tmp_path / "cli.sqlite"

    exit_code = cli.main(
        [
            "import-issue",
            "--source",
            str(fixture),
            "--database",
            str(database),
            "--repository",
            REPOSITORY,
            "--issue",
            "21",
            "--check",
        ]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["created"] is True
    assert summary["work_order_id"].startswith("walsamer-tactus-issue-21-")
    assert summary["holds"] == []

    # A second identical run is idempotent.
    assert (
        cli.main(
            [
                "import-issue",
                "--source",
                str(fixture),
                "--database",
                str(database),
                "--repository",
                REPOSITORY,
                "--issue",
                "21",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["created"] is False
