"""The ``tactus publish`` entry point: refusal paths and one wired publication.

These tests stay offline: the credential provider, the live transport and the
live port are replaced with the mocked equivalents, so what is proven here is the
CLI's composition and its operator-facing output — not a GitHub round trip.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tactus import cli
from tactus.integrations.github import FakeGitHubTransport
from tactus.publication import GitHubPublicationPort, PublicationCredential

NOW = datetime(2026, 11, 1, 15, 0, tzinfo=timezone.utc)

#: Grants are validated against the real clock when the CLI publishes, so the
#: test grant window is built from it rather than from a fixed past instant.
LIVE_NOW = datetime.now(timezone.utc)
WORK_ORDER = "TCT-WO-023B2-live-publication"
REPOSITORY = "Walsamer/tactus"
BRANCH = "agent/tactus/TCT-WO-023B2-live-publication"
BASE = "a" * 40
BRANCH_REVISION = "b" * 40
PATCH_DIGEST = hashlib.sha256(b"verified patch").hexdigest()
CHANGE_ID = "change-cli-1"


class StubProvider:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def resolve(self, grant, request, *, at) -> PublicationCredential:
        return PublicationCredential("stub-credential", provider_id="stub")


def write_manifest(state_root: Path) -> None:
    directory = state_root / ".tactus" / "changes"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{hashlib.sha256(CHANGE_ID.encode()).hexdigest()}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "change_id": CHANGE_ID,
                "prepared_change_id": "chg-" + hashlib.sha256(PATCH_DIGEST.encode()).hexdigest()[:32],
                "request_fingerprint": "f" * 64,
                "facts": {
                    "change_id": CHANGE_ID,
                    "work_order_id": WORK_ORDER,
                    "repository": "/tmp/tactus-test-repository",
                    "source_revision": BASE,
                    "base_revision": BASE,
                    "patch_digest": PATCH_DIGEST,
                    "patch_bytes": 14,
                    "review_branch": BRANCH,
                    "commit_message": "Tactus: verified change",
                    "attempt_id": "attempt-1",
                },
                "review_branch": BRANCH,
                "branch_revision": BRANCH_REVISION,
                "workspace_path": "/tmp/tactus-test-workspace",
                "patch_digest": PATCH_DIGEST,
                "patch_bytes": 14,
                "created_at": NOW.isoformat(),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def write_grant(database: Path) -> str:
    """Record one durable PUBLISH grant and return its recorded grant id."""

    from tactus.domain import SourceRevision, WorkOrderId, WorkOrderRevision
    from tactus.domain.human_intervention import (
        HumanAction,
        HumanInterventionRequest,
        HumanResolution,
        InterventionReason,
    )
    from tactus.persistence import SqliteHumanIntervention

    request = HumanInterventionRequest(
        intervention_id="intervention-cli-1",
        work_order_id=WorkOrderId(WORK_ORDER),
        reason=InterventionReason.APPROVAL_REQUIRED,
        summary="publish this verified change",
        permitted_actions=(HumanAction.PUBLISH,),
        scope=("src/publication",),
        source_revision=SourceRevision(BASE),
        work_order_revision=WorkOrderRevision(1),
        created_at=LIVE_NOW - timedelta(minutes=20),
    )
    resolution = HumanResolution(
        resolution_id="resolution-cli-1",
        intervention_id="intervention-cli-1",
        work_order_id=WorkOrderId(WORK_ORDER),
        actor="Walsamer",
        action=HumanAction.PUBLISH,
        resolved_at=LIVE_NOW - timedelta(minutes=10),
        scope=("src/publication",),
        source_revision=SourceRevision(BASE),
        work_order_revision=WorkOrderRevision(1),
        expires_at=LIVE_NOW + timedelta(hours=2),
    )
    store = SqliteHumanIntervention(str(database))
    try:
        store.record_request(request)
        _, grant = store.resolve(
            resolution,
            current_source_revision=SourceRevision(BASE),
            current_work_order_revision=WorkOrderRevision(1),
        )
        return grant.grant_id
    finally:
        store.close()


def base_args(tmp_path: Path, grant_id: str, **overrides: str) -> list[str]:
    state_root = tmp_path / "state"
    values = {
        "--grant": grant_id,
        "--work-order": WORK_ORDER,
        "--prepared-change": CHANGE_ID,
        "--database": str(tmp_path / "human.sqlite"),
        "--state-root": str(state_root),
        "--repository": REPOSITORY,
        "--repository-root": str(tmp_path),
        "--token-file": str(tmp_path / "token"),
        "--base": "main",
    }
    values.update(overrides)
    argv = ["publish"]
    for key, value in values.items():
        argv.extend([key, value])
    return argv


def test_publish_requires_its_operator_arguments(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli.main(["publish", "--grant", "g"])


def test_an_unknown_grant_is_refused(tmp_path: Path, capsys) -> None:
    write_manifest(tmp_path / "state")
    write_grant(tmp_path / "human.sqlite")

    code = cli.main(base_args(tmp_path, "grant-that-does-not-exist"))

    assert code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"] == "unknown_grant"
    assert payload["publication"] == "refused"


def test_an_unknown_prepared_change_is_refused(tmp_path: Path, capsys) -> None:
    grant_id = write_grant(tmp_path / "human.sqlite")

    code = cli.main(
        base_args(tmp_path, grant_id, **{"--prepared-change": "change-missing"})
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"] == "PreparedChangeStateError"


def test_a_wired_publication_prints_redacted_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    write_manifest(tmp_path / "state")
    grant_id = write_grant(tmp_path / "human.sqlite")

    transport = FakeGitHubTransport()
    transport.set_base_revision(REPOSITORY, "main", BASE)

    monkeypatch.setattr("tactus.publication.FileTokenCredentialProvider", StubProvider)
    monkeypatch.setattr("tactus.integrations.github.gh.GhGitHubTransport", StubProvider)

    def port_factory(_transport, provider, *, state_root, **_kwargs):
        return GitHubPublicationPort(transport, provider, state_root=state_root)

    monkeypatch.setattr("tactus.publication.GitHubLivePublicationPort", port_factory)

    code = cli.main(base_args(tmp_path, grant_id))

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["publication_status"] == "PUBLISHED"
    assert payload["redacted"] is True
    assert payload["grant_id"] == grant_id
    assert "credential" not in json.dumps(payload).lower()
