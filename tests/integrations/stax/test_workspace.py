"""Deterministic tests for the pinned-Stax workspace lifecycle.

Nothing here runs a real Stax binary, Git, the network or a canonical checkout.
The fake runner models the documented ``stax worktree`` commands and records
every invocation so tests can assert that no publication or forced cleanup is
attempted.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from tactus.integrations.stax import (
    PINNED_STAX,
    WORKSPACE_NAME_PREFIX,
    CommandResult,
    StaxExecutableDigestMismatchError,
    StaxWorkspacePort,
    WorkspaceIdentity,
    WorkspaceIdentityConflictError,
    WorkspaceInUseError,
    WorkspacePreflightError,
    WorkspaceRequest,
    WorkspaceRequestError,
    WorkspaceSourceDriftError,
    WorkspaceStateError,
    WorkspaceCreateError,
    WorkspaceDirtyError,
    WorkspaceOwnershipError,
    WorkspaceBoundaryError,
    WorkspaceContainmentError,
    WorkspaceNotFoundError,
    sha256_file,
)

SUPPORTED = PINNED_STAX.supported_platform
BASE_A = "a" * 40
BASE_B = "b" * 40
OTHER = "c" * 40
VERSION_OUTPUT = f"stax {PINNED_STAX.version}\n"
INITIALIZED_OUTPUT = (
    "stax doctor\n\n"
    "\u2713 Repo initialized\n"
    "\u2713 Trunk: main\n"
)


class FakeStax:
    """Stateful fake for the exact Stax commands the port may invoke."""

    def __init__(
        self,
        executable: Path,
        workspace_root: Path,
        revisions: dict[Path, str],
    ) -> None:
        self.executable = executable
        self.workspace_root = workspace_root
        self.revisions = revisions
        self.worktrees: dict[str, dict[str, object]] = {}
        self.calls: list[tuple[tuple[str, ...], Path | None]] = []
        self.create_error: CommandResult | None = None
        self.remove_error: CommandResult | None = None
        self.path_override: dict[str, Path] = {}

    def add_worktree(
        self,
        name: str,
        *,
        path: Path | None = None,
        dirty: bool = False,
        branch: str | None = None,
        create_directory: bool = True,
    ) -> Path:
        worktree_path = path or (self.workspace_root / name)
        if create_directory:
            worktree_path.mkdir(parents=True, exist_ok=True)
        self.worktrees[name] = {
            "name": name,
            "branch": branch if branch is not None else name,
            "path": str(worktree_path),
            "dirty": dirty,
        }
        return worktree_path

    def mark_dirty(self, name: str) -> None:
        self.worktrees[name]["dirty"] = True

    def mutate(self, argv: tuple[str, ...]) -> bool:
        return (
            len(argv) >= 2
            and argv[0] == "worktree"
            and argv[1] in {"create", "remove"}
        )

    def __call__(
        self, argv: tuple[str, ...], *, cwd: Path | None = None
    ) -> CommandResult:
        argv = tuple(argv)
        self.calls.append((argv, cwd))
        args = argv[1:]

        if args == ("--version",):
            return CommandResult(0, VERSION_OUTPUT)
        if args == ("doctor",):
            return CommandResult(0, INITIALIZED_OUTPUT)
        if args == ("worktree", "list", "--json"):
            payload = []
            for record in self.worktrees.values():
                payload.append(
                    {
                        "name": record["name"],
                        "branch": record["branch"],
                        "path": record["path"],
                        "dirty": record["dirty"],
                        "status": ["dirty"] if record["dirty"] else ["clean"],
                    }
                )
            return CommandResult(
                0, json.dumps(payload, indent=2, sort_keys=True)
            )
        if args[:2] == ("worktree", "path"):
            name = args[2]
            record = self.worktrees.get(name)
            if record is None:
                return CommandResult(1, "", f"error: no worktree {name}\n")
            path = self.path_override.get(name, Path(str(record["path"])))
            return CommandResult(0, f"{path}\n")
        if args[:2] == ("worktree", "create"):
            if self.create_error is not None:
                return self.create_error
            name = args[2]
            try:
                base = args[args.index("--from") + 1]
            except (ValueError, IndexError):
                return CommandResult(1, "", "error: missing --from\n")
            path = self.add_worktree(name)
            self.revisions[path.resolve()] = base
            return CommandResult(0, f"Created worktree {name} at {path}\n")
        if args[:2] == ("worktree", "remove"):
            if any(arg in {"-f", "--force"} for arg in args):
                return CommandResult(1, "", "error: forced removal is not allowed\n")
            name = args[2]
            record = self.worktrees.get(name)
            if record is None:
                return CommandResult(1, "", f"error: no worktree {name}\n")
            if record["dirty"]:
                return CommandResult(
                    1, "", "error: worktree has uncommitted changes\n"
                )
            if self.remove_error is not None:
                return self.remove_error
            path = Path(str(record["path"]))
            if path.exists():
                shutil.rmtree(path)
            del self.worktrees[name]
            return CommandResult(0, f"Removed worktree {name}\n")
        raise AssertionError(f"unexpected fake Stax command: {args!r}")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    path = tmp_path / "repository"
    path.mkdir()
    return path


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    path = tmp_path / "worktrees"
    path.mkdir()
    return path


@pytest.fixture
def executable(tmp_path: Path) -> Path:
    path = tmp_path / "stax"
    path.write_bytes(b"deterministic fake stax executable\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def revisions(repository: Path) -> dict[Path, str]:
    return {repository.resolve(): BASE_A}


@pytest.fixture
def fake(
    executable: Path,
    workspace_root: Path,
    revisions: dict[Path, str],
) -> FakeStax:
    return FakeStax(executable, workspace_root, revisions)


@pytest.fixture
def port(
    executable: Path,
    workspace_root: Path,
    fake: FakeStax,
    revisions: dict[Path, str],
) -> StaxWorkspacePort:
    pin = replace(
        PINNED_STAX, executable_sha256=sha256_file(executable)
    )
    return StaxWorkspacePort(
        executable,
        workspace_root=workspace_root,
        runner=fake,
        pin=pin,
        platform=SUPPORTED,
        revision_probe=lambda path: revisions[path.resolve()],
    )


def request(repository: Path, *, attempt_id: str = "attempt-1", base: str = BASE_A) -> WorkspaceRequest:
    return WorkspaceRequest(
        attempt_id=attempt_id, base_revision=base, repository=repository
    )


def evidence_files(workspace_root: Path) -> list[Path]:
    directory = workspace_root / ".tactus" / "refusals"
    if not directory.exists():
        return []
    return sorted(directory.glob("*.json"))


def manifest_files(workspace_root: Path) -> list[Path]:
    directory = workspace_root / ".tactus" / "workspaces"
    if not directory.exists():
        return []
    return sorted(directory.glob("*.json"))


def worktree_commands(fake: FakeStax) -> list[tuple[str, ...]]:
    return [argv[2:] for argv, _ in fake.calls if argv[1:2] == ("worktree",)]


# -- identity ---------------------------------------------------------------


def test_identity_is_stable_and_binds_every_request_fact(
    repository: Path, workspace_root: Path, fake: FakeStax
) -> None:
    first = request(repository)
    second = request(repository)
    assert first.workspace_name == second.workspace_name
    assert first.workspace_name.startswith(WORKSPACE_NAME_PREFIX)
    assert first.branch == first.workspace_name

    path = fake.add_worktree(first.workspace_name)
    identity = WorkspaceIdentity(
        attempt_id=first.attempt_id,
        base_revision=first.base_revision,
        branch=first.branch,
        path=path,
        repository=repository,
    )
    other_revision = WorkspaceIdentity(
        attempt_id=first.attempt_id,
        base_revision=BASE_B,
        branch=first.branch,
        path=path,
        repository=repository,
    )
    other_path = WorkspaceIdentity(
        attempt_id=first.attempt_id,
        base_revision=first.base_revision,
        branch=first.branch,
        path=workspace_root / "different",
        repository=repository,
    )

    assert identity.fingerprint == identity.fingerprint
    assert identity.fingerprint != other_revision.fingerprint
    assert identity.fingerprint != other_path.fingerprint
    assert identity.as_dict()["attempt_id"] == "attempt-1"
    assert identity.as_dict()["base_revision"] == BASE_A
    assert identity.as_dict()["branch"] == first.branch
    assert identity.as_dict()["path"] == str(path.resolve())


def test_request_rejects_floating_revisions_and_relative_repositories(
    repository: Path,
) -> None:
    with pytest.raises(WorkspaceRequestError):
        request(repository, base="main")
    with pytest.raises(WorkspaceRequestError):
        request(repository, base=BASE_A.upper())
    with pytest.raises(WorkspaceRequestError):
        WorkspaceRequest(
            attempt_id="attempt-1",
            base_revision=BASE_A,
            repository=Path("relative/repo"),
        )
    with pytest.raises(WorkspaceRequestError):
        WorkspaceRequest(
            attempt_id="",
            base_revision=BASE_A,
            repository=repository,
        )


# -- create/reuse -----------------------------------------------------------


def test_create_invokes_only_the_expected_stax_commands_and_records_identity(
    port: StaxWorkspacePort,
    repository: Path,
    workspace_root: Path,
    fake: FakeStax,
) -> None:
    req = request(repository)
    with port.create(req) as lease:
        assert lease.reused is False
        assert lease.identity.attempt_id == req.attempt_id
        assert lease.identity.base_revision == BASE_A
        assert lease.identity.branch == req.workspace_name
        assert lease.root == (workspace_root / req.workspace_name).resolve()
        assert lease.path.is_dir()

    commands = worktree_commands(fake)
    assert (
        "create",
        req.workspace_name,
        "--from",
        BASE_A,
        "--no-verify",
    ) in commands
    assert {command[0] for command in commands} <= {"list", "create", "path"}
    assert len(manifest_files(workspace_root)) == 1
    assert evidence_files(workspace_root) == []


def test_repeated_identical_request_reuses_the_same_workspace(
    port: StaxWorkspacePort,
    repository: Path,
    workspace_root: Path,
    fake: FakeStax,
) -> None:
    req = request(repository)
    first = port.create(req)
    fingerprint = first.identity.fingerprint
    first.close()

    second = port.create(req)
    try:
        assert second.reused is True
        assert second.identity.fingerprint == fingerprint
        assert second.path == first.path
    finally:
        second.close()

    creates = [
        command for command in worktree_commands(fake) if command[:1] == ("create",)
    ]
    assert len(creates) == 1
    assert len(manifest_files(workspace_root)) == 1
    assert evidence_files(workspace_root) == []


def test_changed_base_revision_fails_closed_without_mutation(
    port: StaxWorkspacePort,
    repository: Path,
    workspace_root: Path,
    fake: FakeStax,
) -> None:
    req = request(repository)
    lease = port.create(req)
    path = lease.path
    lease.close()

    with pytest.raises(WorkspaceIdentityConflictError) as raised:
        port.create(request(repository, base=BASE_B))

    assert raised.value.expected["base_revision"] == BASE_B
    assert raised.value.actual["base_revision"] == BASE_A
    assert path.is_dir()
    assert len(manifest_files(workspace_root)) == 1
    assert len(evidence_files(workspace_root)) == 1

    creates = [
        command for command in worktree_commands(fake) if command[:1] == ("create",)
    ]
    assert len(creates) == 1


def test_source_revision_drift_is_rejected_before_creation(
    port: StaxWorkspacePort,
    repository: Path,
    workspace_root: Path,
    fake: FakeStax,
    revisions: dict[Path, str],
) -> None:
    revisions[repository.resolve()] = OTHER

    with pytest.raises(WorkspaceSourceDriftError) as raised:
        port.create(request(repository))

    assert raised.value.expected == BASE_A
    assert raised.value.actual == OTHER
    assert not any(
        command[:1] == ("create",) for command in worktree_commands(fake)
    )
    assert manifest_files(workspace_root) == []
    assert len(evidence_files(workspace_root)) == 1


def test_worktree_revision_drift_on_reuse_fails_closed(
    port: StaxWorkspacePort,
    repository: Path,
    workspace_root: Path,
    fake: FakeStax,
    revisions: dict[Path, str],
) -> None:
    req = request(repository)
    lease = port.create(req)
    path = lease.path
    lease.close()
    revisions[path.resolve()] = OTHER

    with pytest.raises(WorkspaceSourceDriftError):
        port.create(req)

    assert path.is_dir()
    assert len(manifest_files(workspace_root)) == 1
    assert len(evidence_files(workspace_root)) == 1
    creates = [
        command for command in worktree_commands(fake) if command[:1] == ("create",)
    ]
    assert len(creates) == 1


def test_concurrent_attempt_cannot_share_the_workspace(
    port: StaxWorkspacePort,
    repository: Path,
    workspace_root: Path,
    fake: FakeStax,
) -> None:
    req = request(repository)
    first = port.create(req)
    try:
        with pytest.raises(WorkspaceInUseError) as raised:
            port.create(req)
        assert req.attempt_id in str(raised.value) or req.attempt_id in raised.value.owner
    finally:
        first.close()

    assert len(evidence_files(workspace_root)) == 1
    creates = [
        command for command in worktree_commands(fake) if command[:1] == ("create",)
    ]
    assert len(creates) == 1

    with port.create(req) as second:
        assert second.reused is True


def test_create_failure_is_typed_and_preserves_evidence(
    port: StaxWorkspacePort,
    repository: Path,
    workspace_root: Path,
    fake: FakeStax,
) -> None:
    fake.create_error = CommandResult(1, "", "fatal: create failed\n")

    with pytest.raises(WorkspaceCreateError) as raised:
        port.create(request(repository))

    assert isinstance(raised.value, WorkspaceCreateError)
    assert manifest_files(workspace_root) == []
    evidence = evidence_files(workspace_root)
    assert len(evidence) == 1
    payload = json.loads(evidence[0].read_text(encoding="utf-8"))
    assert payload["refusal"] == "WorkspaceCreateError"
    assert payload["command_result"]["returncode"] == 1


# -- path/list --------------------------------------------------------------


def test_path_and_list_are_read_only_and_require_exact_ownership(
    port: StaxWorkspacePort,
    repository: Path,
    workspace_root: Path,
    fake: FakeStax,
) -> None:
    req = request(repository)
    with port.create(req) as lease:
        created_path = lease.path
        fingerprint = lease.identity.fingerprint

    fake.add_worktree("unrelated-worktree")
    fake.calls.clear()

    records = port.list(repository)
    assert [record.name for record in records] == [req.workspace_name]
    assert records[0].identity is not None
    assert records[0].identity.fingerprint == fingerprint
    assert records[0].path == created_path
    assert port.path(req) == created_path

    assert not any(
        fake.mutate(argv[1:]) for argv, _ in fake.calls
    )
    assert {
        command[0]
        for command in worktree_commands(fake)
    } == {"list"}


def test_path_refuses_a_workspace_without_an_ownership_manifest(
    port: StaxWorkspacePort,
    repository: Path,
    workspace_root: Path,
    fake: FakeStax,
) -> None:
    req = request(repository)
    fake.add_worktree(req.workspace_name)

    with pytest.raises(WorkspaceNotFoundError):
        port.path(req)

    assert len(evidence_files(workspace_root)) == 1


# -- remove -----------------------------------------------------------------


def test_clean_remove_is_not_forced_and_clears_ownership(
    port: StaxWorkspacePort,
    repository: Path,
    workspace_root: Path,
    fake: FakeStax,
) -> None:
    req = request(repository)
    with port.create(req) as lease:
        path = lease.path
        fingerprint = lease.identity.fingerprint

    removal = port.remove(req)

    assert removal.identity.fingerprint == fingerprint
    assert removal.remaining_path_exists is False
    assert not path.exists()
    assert manifest_files(workspace_root) == []
    assert evidence_files(workspace_root) == []

    remove_command = worktree_commands(fake)[-1]
    assert remove_command == ("remove", req.workspace_name)
    for forbidden in ("-f", "--force", "--delete-branch"):
        assert forbidden not in remove_command


def test_dirty_workspace_is_preserved_with_evidence(
    port: StaxWorkspacePort,
    repository: Path,
    workspace_root: Path,
    fake: FakeStax,
) -> None:
    req = request(repository)
    with port.create(req) as lease:
        path = lease.path
    fake.mark_dirty(req.workspace_name)

    with pytest.raises(WorkspaceDirtyError):
        port.remove(req)

    assert path.is_dir()
    assert len(manifest_files(workspace_root)) == 1
    assert not any(
        command[:1] == ("remove",) for command in worktree_commands(fake)
    )
    evidence = evidence_files(workspace_root)
    assert len(evidence) == 1
    payload = json.loads(evidence[0].read_text(encoding="utf-8"))
    assert payload["refusal"] == "WorkspaceDirtyError"
    assert payload["workspace_preserved"] is True


def test_dirty_refusal_from_stax_itself_is_not_retried_with_force(
    port: StaxWorkspacePort,
    repository: Path,
    workspace_root: Path,
    fake: FakeStax,
) -> None:
    req = request(repository)
    with port.create(req) as lease:
        path = lease.path
    fake.remove_error = CommandResult(
        1, "", "error: worktree has uncommitted changes\n"
    )

    with pytest.raises(WorkspaceDirtyError):
        port.remove(req)

    assert path.is_dir()
    assert len(manifest_files(workspace_root)) == 1
    removes = [
        command for command in worktree_commands(fake) if command[:1] == ("remove",)
    ]
    assert len(removes) == 1
    for forbidden in ("-f", "--force", "--delete-branch"):
        assert forbidden not in removes[0]


def test_remove_refuses_an_unowned_worktree(
    port: StaxWorkspacePort,
    repository: Path,
    workspace_root: Path,
    fake: FakeStax,
) -> None:
    req = request(repository)
    path = fake.add_worktree(req.workspace_name)

    with pytest.raises(WorkspaceOwnershipError):
        port.remove(req)

    assert path.is_dir()
    assert not any(
        command[:1] == ("remove",) for command in worktree_commands(fake)
    )
    assert len(evidence_files(workspace_root)) == 1


# -- preflight/boundary -----------------------------------------------------


def test_off_pin_binary_is_rejected_before_any_worktree_command(
    executable: Path,
    workspace_root: Path,
    repository: Path,
    fake: FakeStax,
    revisions: dict[Path, str],
) -> None:
    unpinned_port = StaxWorkspacePort(
        executable,
        workspace_root=workspace_root,
        runner=fake,
        pin=PINNED_STAX,
        platform=SUPPORTED,
        revision_probe=lambda path: revisions[path.resolve()],
    )

    with pytest.raises(WorkspacePreflightError) as raised:
        unpinned_port.create(request(repository))

    assert isinstance(raised.value.cause, StaxExecutableDigestMismatchError)
    assert fake.calls == []
    assert manifest_files(workspace_root) == []
    assert len(evidence_files(workspace_root)) == 1


def test_workspace_path_escape_is_refused_and_preserved(
    port: StaxWorkspacePort,
    repository: Path,
    workspace_root: Path,
    fake: FakeStax,
    tmp_path: Path,
) -> None:
    req = request(repository)
    outside = tmp_path / "outside"
    outside.mkdir()
    fake.path_override[req.workspace_name] = outside

    with pytest.raises(WorkspaceContainmentError):
        port.create(req)

    assert outside.is_dir()
    assert manifest_files(workspace_root) == []
    assert len(evidence_files(workspace_root)) == 1


def test_workspace_root_inside_the_canonical_checkout_is_refused(
    executable: Path,
    repository: Path,
    fake: FakeStax,
    revisions: dict[Path, str],
) -> None:
    inside = repository / "worktrees"
    inside.mkdir()
    unsafe_port = StaxWorkspacePort(
        executable,
        workspace_root=inside,
        runner=fake,
        pin=replace(PINNED_STAX, executable_sha256=sha256_file(executable)),
        platform=SUPPORTED,
        revision_probe=lambda path: revisions[path.resolve()],
    )

    with pytest.raises(WorkspaceBoundaryError):
        unsafe_port.create(request(repository))

    assert fake.calls == []
    assert not (inside / ".tactus").exists()


def test_no_publication_or_forced_cleanup_commands_are_ever_invoked(
    port: StaxWorkspacePort,
    repository: Path,
    workspace_root: Path,
    fake: FakeStax,
) -> None:
    req = request(repository)
    with port.create(req):
        pass
    port.path(req)
    port.list(repository)
    port.remove(req)

    worktree_commands_seen = worktree_commands(fake)
    assert {command[0] for command in worktree_commands_seen} == {
        "create",
        "path",
        "list",
        "remove",
    }
    flat = {token for command in worktree_commands_seen for token in command}
    for forbidden in (
        "-f",
        "--force",
        "--delete-branch",
        "submit",
        "lane",
        "update",
        "init",
        "sync",
        "restack",
        "pr",
        "push",
        "merge",
        "cleanup",
        "prune",
    ):
        assert forbidden not in flat


def test_state_reports_a_manifest_that_disappeared(
    port: StaxWorkspacePort,
    repository: Path,
    workspace_root: Path,
    fake: FakeStax,
) -> None:
    req = request(repository)
    lease = port.create(req)
    path = lease.path
    lease.close()
    for manifest in manifest_files(workspace_root):
        manifest.unlink()

    with pytest.raises(WorkspaceOwnershipError):
        port.remove(req)

    assert path.is_dir()
    assert not any(
        command[:1] == ("remove",) for command in worktree_commands(fake)
    )


def test_distinct_attempts_never_share_a_workspace(
    port: StaxWorkspacePort,
    repository: Path,
) -> None:
    first = port.create(request(repository, attempt_id="attempt-a"))
    second = port.create(request(repository, attempt_id="attempt-b"))
    try:
        assert first.path != second.path
        assert first.branch != second.branch
    finally:
        second.close()
        first.close()


def test_two_port_instances_contend_for_the_same_lease(
    executable: Path,
    workspace_root: Path,
    repository: Path,
    fake: FakeStax,
    revisions: dict[Path, str],
) -> None:
    pin = replace(PINNED_STAX, executable_sha256=sha256_file(executable))
    first_port = StaxWorkspacePort(
        executable,
        workspace_root=workspace_root,
        runner=fake,
        pin=pin,
        platform=SUPPORTED,
        revision_probe=lambda path: revisions[path.resolve()],
    )
    second_port = StaxWorkspacePort(
        executable,
        workspace_root=workspace_root,
        runner=fake,
        pin=pin,
        platform=SUPPORTED,
        revision_probe=lambda path: revisions[path.resolve()],
    )

    lease = first_port.create(request(repository))
    try:
        with pytest.raises(WorkspaceInUseError):
            second_port.create(request(repository))
    finally:
        lease.close()


def test_create_refuses_to_adopt_an_unowned_worktree(
    port: StaxWorkspacePort,
    repository: Path,
    workspace_root: Path,
    fake: FakeStax,
) -> None:
    req = request(repository)
    path = fake.add_worktree(req.workspace_name)

    with pytest.raises(WorkspaceOwnershipError):
        port.create(req)

    assert path.is_dir()
    assert manifest_files(workspace_root) == []
    assert len(evidence_files(workspace_root)) == 1
    assert not any(
        command[:1] == ("create",) for command in worktree_commands(fake)
    )


def test_source_revision_drift_on_reuse_fails_closed(
    port: StaxWorkspacePort,
    repository: Path,
    workspace_root: Path,
    fake: FakeStax,
    revisions: dict[Path, str],
) -> None:
    req = request(repository)
    lease = port.create(req)
    path = lease.path
    lease.close()
    revisions[repository.resolve()] = OTHER

    with pytest.raises(WorkspaceSourceDriftError):
        port.create(req)

    assert path.is_dir()
    assert len(manifest_files(workspace_root)) == 1
    assert len(evidence_files(workspace_root)) == 1
    creates = [
        command for command in worktree_commands(fake) if command[:1] == ("create",)
    ]
    assert len(creates) == 1


def test_remove_refuses_source_drift_before_mutation(
    port: StaxWorkspacePort,
    repository: Path,
    workspace_root: Path,
    fake: FakeStax,
    revisions: dict[Path, str],
) -> None:
    req = request(repository)
    with port.create(req) as lease:
        path = lease.path
    revisions[repository.resolve()] = OTHER

    with pytest.raises(WorkspaceSourceDriftError):
        port.remove(req)

    assert path.is_dir()
    assert len(manifest_files(workspace_root)) == 1
    assert not any(
        command[:1] == ("remove",) for command in worktree_commands(fake)
    )
    assert len(evidence_files(workspace_root)) == 1
