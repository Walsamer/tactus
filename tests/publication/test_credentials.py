"""Publication token provider: narrow file, refused broad scopes, no leaks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tactus.domain import SourceRevision, WorkOrderId
from tactus.publication import (
    CredentialPreflightError,
    FileTokenCredentialProvider,
    PublicationCredential,
    PublicationGrant,
    PublicationRequest,
    TokenScopeFacts,
)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
WORK_ORDER = WorkOrderId("TCT-WO-023B2-live-publication")
REPOSITORY = "Walsamer/tactus"
BRANCH = "agent/tactus/TCT-WO-023B2-live-publication"
BASE = "a" * 40
TOKEN = "github_pat_11AAABBBCCCDDDEEE_verySecretValue"

FINE_GRAINED = TokenScopeFacts(permissions={"admin": False, "push": True, "pull": True})


def write_token(directory: Path, token: str = TOKEN) -> Path:
    path = directory / "publication-token"
    path.write_text(token, encoding="utf-8")
    return path


def make_provider(
    token_path: Path,
    *,
    repository_root: Path,
    facts: TokenScopeFacts = FINE_GRAINED,
    repository: str = REPOSITORY,
) -> FileTokenCredentialProvider:
    return FileTokenCredentialProvider(
        token_path,
        repository=repository,
        repository_root=repository_root,
        probe=lambda token, repo: facts,
    )


def make_request(**overrides: object) -> PublicationRequest:
    defaults: dict[str, object] = dict(
        intent_id="intent-credential-1",
        work_order_id=WORK_ORDER,
        repository=REPOSITORY,
        branch=BRANCH,
        base_revision=BASE,
        destination="main",
        scope=("src/publication",),
        source_revision=SourceRevision(BASE),
    )
    defaults.update(overrides)
    return PublicationRequest(**defaults)  # type: ignore[arg-type]


def make_grant() -> PublicationGrant:
    return PublicationGrant(
        grant_id="grant-publication-1",
        authority="operator",
        work_order_id=WORK_ORDER,
        repository=REPOSITORY,
        branch=BRANCH,
        base_revision=BASE,
        destination="main",
        scope=("src/publication",),
        source_revision=SourceRevision(BASE),
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(hours=1),
    )


def test_a_narrow_fine_grained_token_resolves_to_an_opaque_handle(tmp_path: Path) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    provider = make_provider(write_token(tmp_path), repository_root=repository_root)

    credential = provider.resolve(make_grant(), make_request(), at=NOW)

    assert isinstance(credential, PublicationCredential)
    assert TOKEN not in repr(credential)
    assert TOKEN not in str(credential)
    assert TOKEN not in repr(provider)


def test_a_classic_token_is_refused_because_it_is_repository_wide(tmp_path: Path) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    classic = TokenScopeFacts(
        permissions={"admin": False, "push": True}, oauth_scopes=("gist", "repo")
    )

    with pytest.raises(CredentialPreflightError) as error:
        make_provider(write_token(tmp_path), repository_root=repository_root, facts=classic)

    message = str(error.value)
    assert "classic" in message
    assert "repo" in message
    assert TOKEN not in message


def test_an_admin_token_is_refused(tmp_path: Path) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    admin = TokenScopeFacts(permissions={"admin": True, "push": True})

    with pytest.raises(CredentialPreflightError) as error:
        make_provider(write_token(tmp_path), repository_root=repository_root, facts=admin)

    assert "admin" in str(error.value)
    assert TOKEN not in str(error.value)


def test_a_read_only_token_is_refused(tmp_path: Path) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    read_only = TokenScopeFacts(permissions={"admin": False, "push": False, "pull": True})

    with pytest.raises(CredentialPreflightError):
        make_provider(
            write_token(tmp_path), repository_root=repository_root, facts=read_only
        )


def test_a_token_file_inside_the_repository_is_refused(tmp_path: Path) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    inside = write_token(repository_root)

    with pytest.raises(CredentialPreflightError) as error:
        make_provider(inside, repository_root=repository_root)

    assert "outside the repository" in str(error.value)


def test_a_relative_token_path_is_refused(tmp_path: Path) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()

    with pytest.raises(CredentialPreflightError) as error:
        make_provider(Path("publication-token"), repository_root=repository_root)

    assert "absolute" in str(error.value)


def test_an_empty_token_file_is_refused(tmp_path: Path) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()

    with pytest.raises(CredentialPreflightError):
        make_provider(write_token(tmp_path, "   \n"), repository_root=repository_root)


def test_a_probe_failure_is_reported_without_the_token(tmp_path: Path) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    path = write_token(tmp_path)

    def failing(token: str, repository: str) -> TokenScopeFacts:
        raise CredentialPreflightError("cannot verify")

    with pytest.raises(CredentialPreflightError):
        FileTokenCredentialProvider(
            path,
            repository=REPOSITORY,
            repository_root=repository_root,
            probe=failing,
        )


def test_the_provider_refuses_a_request_for_another_repository(tmp_path: Path) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    provider = make_provider(write_token(tmp_path), repository_root=repository_root)

    with pytest.raises(CredentialPreflightError):
        provider.resolve(
            make_grant(), make_request(repository="Other/repo"), at=NOW
        )
