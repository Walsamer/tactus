"""Deterministic source-control preparation tests on disposable Git repos.

Every test creates its own temporary repository and worktree root. Nothing here
pushes, contacts a remote, reads a credential or touches a canonical checkout.
The prepared change is staged only in the disposable test repository; each test
asserts that the ``main`` ref is byte-for-byte unchanged.
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path

import pytest

from tactus.integrations.stax import (
    CHANGE_ID_PREFIX,
    PROTECTED_BRANCHES,
    REVIEW_BRANCH_PREFIX,
    ChangeIdentityConflictError,
    ChangePreparationRequest,
    DirtyWorkspaceError,
    ImplementationOutcome,
    PatchApplyError,
    PatchDigestMismatchError,
    PublicationEligibility,
    ReviewBranchConflictError,
    ReviewBranchError,
    RevisionBindingError,
    SourceRevisionMismatchError,
    StaxSourceControlPort,
    review_branch_for,
)
from tactus.integrations.stax.preflight import CommandResult
from tactus.integrations.stax.workspace import WorkspaceRequest

REVIEW_BRANCH = "agent/tactus/TCT-WO-023B1-branch-preparation"
CHANGE_ID = "TCT-WO-023B1-branch-preparation"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )


def _git_out(cwd: Path, *args: str) -> str:
    return _git(cwd, *args).stdout.strip()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_patch(
    repository: Path, base_revision: str, files: dict[str, str]
) -> bytes:
    """Create a real unified diff for *files* on top of *base_revision*."""

    work = Path(tempfile.mkdtemp(dir=repository.parent, prefix="patch-build-"))
    try:
        _git(repository, "worktree", "add", "--detach", str(work), base_revision)
        for name, content in files.items():
            target = work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            _git(work, "add", "--", name)
        return _git(work, "diff", "--cached", "--binary", base_revision).stdout.encode()
    finally:
        _git(repository, "worktree", "remove", "--force", str(work))
        _git(repository, "worktree", "prune")


class RecordingRunner:
    """Run real local Git while recording every argv for boundary assertions."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv, *, cwd: Path | None = None) -> CommandResult:
        argv = tuple(argv)
        self.calls.append(argv)
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        return CommandResult(
            completed.returncode, completed.stdout, completed.stderr
        )

    def commands(self) -> list[str]:
        return [" ".join(call) for call in self.calls]


class RejectApplyRunner(RecordingRunner):
    """Recording runner that models an atomic patch rejection."""

    def __call__(self, argv, *, cwd: Path | None = None) -> CommandResult:
        argv = tuple(argv)
        if len(argv) > 1 and argv[1] == "apply":
            self.calls.append(argv)
            return CommandResult(1, "", "error: patch does not apply\n")
        return super().__call__(argv, cwd=cwd)


class FakeLease:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.closed = False

    def close(self) -> None:
        self.closed = True


class DisposableWorkspaceProvider:
    """Create a real disposable ``git worktree`` per attempt (no Stax binary)."""

    def __init__(
        self,
        root: Path,
        *,
        dirty: bool = False,
        drift: bool = False,
    ) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.dirty = dirty
        self.drift = drift
        self.calls: list[WorkspaceRequest] = []
        self.leases: list[FakeLease] = []

    def create(self, request: WorkspaceRequest) -> FakeLease:
        self.calls.append(request)
        path = self.root / request.workspace_name
        if not path.exists():
            _git(
                request.repository,
                "worktree",
                "add",
                "--detach",
                str(path),
                request.base_revision,
            )
        if self.dirty:
            (path / "user-notes.txt").write_text("preserve me\n", encoding="utf-8")
        if self.drift:
            _git(
                path,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "--allow-empty",
                "-m",
                "unexpected workspace drift",
            )
        lease = FakeLease(path)
        self.leases.append(lease)
        return lease


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    path = tmp_path / "repository"
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Tactus Test")
    _git(path, "config", "user.email", "test@example.invalid")
    (path / "README.md").write_text("verified base\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "base")
    return path


@pytest.fixture
def base_revision(repository: Path) -> str:
    return _git_out(repository, "rev-parse", "HEAD^{commit}")


@pytest.fixture
def patch(repository: Path, base_revision: str) -> bytes:
    return _build_patch(
        repository,
        base_revision,
        {"src/change.txt": "verified implementation\n"},
    )


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    path = tmp_path / "state"
    path.mkdir()
    return path


@pytest.fixture
def worktree_root(tmp_path: Path) -> Path:
    path = tmp_path / "worktrees"
    path.mkdir()
    return path


def make_request(
    repository: Path,
    base_revision: str,
    patch: bytes,
    **overrides,
) -> ChangePreparationRequest:
    values = {
        "change_id": CHANGE_ID,
        "work_order_id": CHANGE_ID,
        "repository": repository,
        "source_revision": base_revision,
        "base_revision": base_revision,
        "patch": patch,
        "patch_digest": _sha256(patch),
        "review_branch": REVIEW_BRANCH,
        "commit_message": "Tactus: stage verified implementation",
    }
    values.update(overrides)
    return ChangePreparationRequest(**values)


def prepare(
    provider: DisposableWorkspaceProvider,
    state_root: Path,
    request: ChangePreparationRequest,
    *,
    runner: RecordingRunner | None = None,
) -> tuple[object, RecordingRunner]:
    runner = runner or RecordingRunner()
    port = StaxSourceControlPort(
        provider, state_root=state_root, runner=runner
    )
    return port.prepare(request), runner


def main_ref(repository: Path) -> str:
    return _git_out(repository, "rev-parse", "refs/heads/main")


def test_prepare_stages_verified_patch_without_publication(
    repository: Path,
    base_revision: str,
    patch: bytes,
    state_root: Path,
    worktree_root: Path,
) -> None:
    before_main = main_ref(repository)
    provider = DisposableWorkspaceProvider(worktree_root)
    request = make_request(repository, base_revision, patch)

    prepared, runner = prepare(provider, state_root, request)

    assert prepared.implementation.outcome is ImplementationOutcome.VERIFIED
    assert prepared.implementation_verified is True
    assert prepared.publication is PublicationEligibility.NOT_ELIGIBLE
    assert prepared.publication_eligible is False
    assert prepared.as_dict()["publication_eligible"] is False
    assert "separate authorized publication grant" in prepared.publication_reason
    assert prepared.source_revision == base_revision
    assert prepared.base_revision == base_revision
    assert prepared.patch_digest == request.patch_digest
    assert prepared.review_branch == REVIEW_BRANCH
    assert prepared.branch_revision != base_revision
    assert prepared.prepared_change_id.startswith(CHANGE_ID_PREFIX)
    assert prepared.workspace_present is True

    # The review branch is exactly the staged commit and is a single child of
    # the verified base; the disposable worktree contains the verified content.
    branch_ref = "refs/heads/" + REVIEW_BRANCH
    assert _git_out(repository, "rev-parse", branch_ref) == prepared.branch_revision
    assert (
        _git_out(repository, "rev-parse", f"{prepared.branch_revision}^")
        == base_revision
    )
    workspace = Path(prepared.workspace_path)
    assert (workspace / "src/change.txt").read_text(encoding="utf-8") == (
        "verified implementation\n"
    )

    # Canonical main and canonical checkout are untouched; no remote-facing
    # command was attempted.
    assert main_ref(repository) == before_main
    assert not (repository / "src/change.txt").exists()
    assert _git_out(repository, "status", "--porcelain=v1") == ""
    commands = runner.commands()
    assert commands
    for forbidden in ("push", "submit", "pull", "fetch", "remote", "pr "):
        assert not any(forbidden in command for command in commands), commands


def test_repeated_preparation_reuses_one_stable_identity(
    repository: Path,
    base_revision: str,
    patch: bytes,
    state_root: Path,
    worktree_root: Path,
) -> None:
    before_main = main_ref(repository)
    provider = DisposableWorkspaceProvider(worktree_root)
    request = make_request(repository, base_revision, patch)

    first, _ = prepare(provider, state_root, request)
    second, runner = prepare(provider, state_root, request)

    assert first.prepared_change_id == second.prepared_change_id
    assert first.review_branch == second.review_branch
    assert first.branch_revision == second.branch_revision
    assert second.implementation.outcome is ImplementationOutcome.REUSED
    assert second.implementation_verified is True
    assert second.publication_eligible is False
    assert len(provider.calls) == 1  # the second call reused persisted state
    assert main_ref(repository) == before_main

    # No additional commit was created in the disposable repository.
    assert _git_out(repository, "rev-list", "--count", "refs/heads/main") == "1"
    branch_commits = _git_out(
        repository, "rev-list", "--count", f"{second.review_branch}"
    )
    assert branch_commits == "2"  # verified base + the one staged change


def test_same_change_id_with_different_facts_is_rejected(
    repository: Path,
    base_revision: str,
    patch: bytes,
    state_root: Path,
    worktree_root: Path,
) -> None:
    before_main = main_ref(repository)
    provider = DisposableWorkspaceProvider(worktree_root)
    first, _ = prepare(
        provider, state_root, make_request(repository, base_revision, patch)
    )

    different_patch = _build_patch(
        repository,
        base_revision,
        {"src/change.txt": "different implementation\n"},
    )
    conflicting = make_request(repository, base_revision, different_patch)
    assert conflicting.change_id == first.change_id
    assert conflicting.prepared_change_id != first.prepared_change_id

    with pytest.raises(ChangeIdentityConflictError):
        prepare(provider, state_root, conflicting)

    assert main_ref(repository) == before_main
    assert (
        _git_out(repository, "rev-parse", f"refs/heads/{first.review_branch}")
        == first.branch_revision
    )
    assert len(provider.calls) == 1


def test_patch_digest_mismatch_fails_before_workspace_mutation(
    repository: Path,
    base_revision: str,
    patch: bytes,
    state_root: Path,
    worktree_root: Path,
) -> None:
    before_main = main_ref(repository)
    provider = DisposableWorkspaceProvider(worktree_root)
    tampered = make_request(
        repository,
        base_revision,
        patch,
        patch_digest="0" * 64,
    )

    with pytest.raises(PatchDigestMismatchError) as raised:
        prepare(provider, state_root, tampered)

    assert raised.value.expected == "0" * 64
    assert raised.value.actual == _sha256(patch)
    assert raised.value.evidence_path is not None
    assert raised.value.evidence_path.is_file()
    assert provider.calls == []
    assert main_ref(repository) == before_main


def test_source_and_base_revision_must_be_the_same_exact_commit(
    repository: Path,
    base_revision: str,
    patch: bytes,
) -> None:
    other = "a" * 40
    with pytest.raises(RevisionBindingError):
        make_request(
            repository,
            base_revision,
            patch,
            source_revision=other,
        )


def test_missing_base_commit_fails_before_workspace_mutation(
    repository: Path,
    patch: bytes,
    state_root: Path,
    worktree_root: Path,
) -> None:
    before_main = main_ref(repository)
    provider = DisposableWorkspaceProvider(worktree_root)
    request = make_request(repository, "b" * 40, patch)

    with pytest.raises(SourceRevisionMismatchError):
        prepare(provider, state_root, request)

    assert provider.calls == []
    assert main_ref(repository) == before_main


def test_dirty_workspace_is_preserved_and_never_reset(
    repository: Path,
    base_revision: str,
    patch: bytes,
    state_root: Path,
    worktree_root: Path,
) -> None:
    before_main = main_ref(repository)
    provider = DisposableWorkspaceProvider(worktree_root, dirty=True)
    request = make_request(repository, base_revision, patch)

    with pytest.raises(DirtyWorkspaceError) as raised:
        prepare(provider, state_root, request)

    workspace = provider.leases[0].path
    assert raised.value.path == workspace
    assert (workspace / "user-notes.txt").read_text(encoding="utf-8") == "preserve me\n"
    assert not (repository / "user-notes.txt").exists()
    assert raised.value.evidence_path is not None
    assert raised.value.evidence_path.is_file()
    # The review branch was never created and canonical main is unchanged.
    missing = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{REVIEW_BRANCH}"],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    assert missing.returncode != 0
    assert main_ref(repository) == before_main


def test_workspace_source_drift_fails_closed(
    repository: Path,
    base_revision: str,
    patch: bytes,
    state_root: Path,
    worktree_root: Path,
) -> None:
    before_main = main_ref(repository)
    provider = DisposableWorkspaceProvider(worktree_root, drift=True)
    request = make_request(repository, base_revision, patch)

    with pytest.raises(SourceRevisionMismatchError):
        prepare(provider, state_root, request)

    assert main_ref(repository) == before_main
    assert provider.leases[0].path.is_dir()


def test_patch_apply_failure_preserves_workspace_and_main(
    repository: Path,
    base_revision: str,
    patch: bytes,
    state_root: Path,
    worktree_root: Path,
) -> None:
    before_main = main_ref(repository)
    provider = DisposableWorkspaceProvider(worktree_root)
    request = make_request(repository, base_revision, patch)

    with pytest.raises(PatchApplyError):
        prepare(
            provider,
            state_root,
            request,
            runner=RejectApplyRunner(),
        )

    assert main_ref(repository) == before_main
    workspace = provider.leases[0].path
    assert _git_out(workspace, "status", "--porcelain=v1") == ""
    assert not (workspace / "src/change.txt").exists()


def test_existing_review_branch_is_a_conflict(
    repository: Path,
    base_revision: str,
    patch: bytes,
    state_root: Path,
    worktree_root: Path,
) -> None:
    _git(repository, "branch", REVIEW_BRANCH, base_revision)
    before_main = main_ref(repository)
    provider = DisposableWorkspaceProvider(worktree_root)

    with pytest.raises(ReviewBranchConflictError) as raised:
        prepare(provider, state_root, make_request(repository, base_revision, patch))

    assert raised.value.branch == REVIEW_BRANCH
    assert raised.value.actual == base_revision
    assert provider.calls == []
    assert main_ref(repository) == before_main


@pytest.mark.parametrize(
    "branch",
    ["main", "master", "feature/not-a-review-branch", "agent/tactus/"],
)
def test_review_branch_naming_fails_closed(
    repository: Path,
    base_revision: str,
    patch: bytes,
    branch: str,
) -> None:
    with pytest.raises(ReviewBranchError):
        make_request(
            repository,
            base_revision,
            patch,
            review_branch=branch,
        )


def test_review_branch_helper_is_stable_and_namespaced() -> None:
    first = review_branch_for("TCT-WO-023B1 Branch Preparation")
    second = review_branch_for("TCT-WO-023B1 Branch Preparation")
    assert first == second
    assert first.startswith(REVIEW_BRANCH_PREFIX)
    assert first not in PROTECTED_BRANCHES


def test_contract_exposes_no_publication_surface(
    state_root: Path, worktree_root: Path
) -> None:
    provider = DisposableWorkspaceProvider(worktree_root)
    port = StaxSourceControlPort(provider, state_root=state_root)
    assert port.contract.published is False
    assert port.contract.publication_authority is False
    assert not hasattr(port, "publish")
    assert port.contract.as_dict()["publication_authority"] is False


def test_manifest_is_content_addressed_and_has_no_remote_ref(
    repository: Path,
    base_revision: str,
    patch: bytes,
    state_root: Path,
    worktree_root: Path,
) -> None:
    provider = DisposableWorkspaceProvider(worktree_root)
    request = make_request(repository, base_revision, patch)
    first, _ = prepare(provider, state_root, request)

    manifests = list((state_root / ".tactus" / "changes").glob("*.json"))
    assert len(manifests) == 1
    payload = manifests[0].read_text(encoding="utf-8")
    assert first.prepared_change_id in payload
    assert first.branch_revision in payload
    assert "push" not in payload
    assert "credential" not in payload.lower()
