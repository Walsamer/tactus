"""Live GitHub transport: argv shape, protected refusals, no force, no token leak."""

from __future__ import annotations

import json

import pytest

from tactus.integrations.github.gh import (
    GhCommandResult,
    GhGitHubTransport,
)
from tactus.integrations.github.transport import (
    GitHubAcknowledgmentLostError,
    GitHubBaseMovedError,
    GitHubNonFastForwardPushError,
    GitHubPermissionDeniedError,
    GitHubProtectedDestinationError,
    GitHubTransportError,
)
from tactus.publication import PublicationCredential

REPOSITORY = "Walsamer/tactus"
BASE = "a" * 40
BRANCH = "agent/tactus/TCT-WO-023B2-live-publication"
REVISION = "b" * 40
TOKEN = "github_pat_TESTONLYdo_not_log"
MARKER = "pub-" + "c" * 32


class RecordingRunner:
    """Deterministic command stand-in: records argv/env, returns canned output.

    A response may be a single result or a sequence consumed one per match, so
    a test can model GitHub changing state between a failed mutation and the
    reconciliation read that follows it.
    """

    def __init__(self, responses: dict[str, object] | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []
        self._responses = responses or {}
        self._consumed: dict[str, int] = {}

    def __call__(self, argv, *, cwd=None, env=None):
        self.calls.append((list(argv), dict(env) if env is not None else None))
        joined = " ".join(argv[1:])
        for needle, response in self._responses.items():
            if needle not in joined:
                continue
            if isinstance(response, list):
                index = min(self._consumed.get(needle, 0), len(response) - 1)
                self._consumed[needle] = index + 1
                return response[index]
            return response
        raise AssertionError(f"unexpected command: {argv}")

    @property
    def pushed(self) -> list[list[str]]:
        return [
            argv
            for argv, _ in self.calls
            if argv[:1] == ["git"] and "push" in argv
        ]

    @property
    def push_env(self) -> dict[str, str] | None:
        for argv, env in self.calls:
            if argv[:1] == ["git"] and "push" in argv:
                return env
        return None


REMOTE = "git@github.com:Walsamer/tactus.git\n"


def ok(payload: object) -> GhCommandResult:
    return GhCommandResult(returncode=0, stdout=json.dumps(payload))


def ref(revision: str) -> GhCommandResult:
    return ok({"object": {"sha": revision}})


def make_transport(runner: RecordingRunner, **overrides: object) -> GhGitHubTransport:
    options: dict[str, object] = dict(
        source_path="/tmp",
        authorized_base_branch="main",
        authorized_base_revision=BASE,
        runner=runner,
    )
    options.update(overrides)
    transport = GhGitHubTransport(**options)  # type: ignore[arg-type]
    transport.bind_credential(PublicationCredential(TOKEN, provider_id="operator-token"))
    return transport


def test_the_contract_declares_a_live_networked_transport_needing_credentialed_reads() -> None:
    transport = make_transport(RecordingRunner())

    assert transport.contract.live
    assert transport.contract.network
    assert transport.contract.reads_require_credential
    assert transport.contract.transport == "gh"


def test_push_never_forces_and_keeps_the_token_out_of_argv() -> None:
    runner = RecordingRunner(
        {
            "remote get-url": GhCommandResult(0, stdout=REMOTE),
            f"{REVISION}:refs/heads/{BRANCH}": GhCommandResult(0),
            f"git/ref/heads/{BRANCH}": ref(REVISION),
        }
    )
    transport = make_transport(runner)

    assert transport.ensure_branch(REPOSITORY, BRANCH, REVISION) == REVISION

    push = runner.pushed[0]
    # `--force` is a flag someone eventually adds: the transport never has one,
    # and it resets any ambient credential helper before its own.
    assert "--force" not in push
    assert not any(argument.startswith("--force") for argument in push)
    assert push[:2] == ["git", "-c"]
    assert "credential.helper=" in push
    assert "--no-verify" in push
    assert f"{REVISION}:refs/heads/{BRANCH}" in push
    assert TOKEN not in " ".join(push)

    environment = runner.push_env
    assert environment is not None
    assert environment["GH_TOKEN"] == TOKEN


def test_a_checkout_whose_remote_is_another_repository_is_refused() -> None:
    runner = RecordingRunner(
        {
            "remote get-url": GhCommandResult(
                0, stdout="git@github.com:someone-else/tactus.git\n"
            ),
        }
    )
    transport = make_transport(runner)

    with pytest.raises(GitHubProtectedDestinationError) as error:
        transport.ensure_branch(REPOSITORY, BRANCH, REVISION)

    assert "not the authorized repository" in error.value.reason
    assert runner.pushed == []


def test_a_rejected_non_fast_forward_push_is_refused_never_forced() -> None:
    runner = RecordingRunner(
        {
            "remote get-url": GhCommandResult(0, stdout=REMOTE),
            f"{REVISION}:refs/heads/{BRANCH}": GhCommandResult(
                returncode=1,
                stderr=(
                    "To github.com:Walsamer/tactus.git\n"
                    f" ! [rejected]        {REVISION} -> {BRANCH} (non-fast-forward)\n"
                    "error: failed to push some refs to 'github.com:Walsamer/tactus.git'"
                ),
            ),
        }
    )
    transport = make_transport(runner)

    with pytest.raises(GitHubNonFastForwardPushError) as error:
        transport.ensure_branch(REPOSITORY, BRANCH, REVISION)

    assert error.value.branch == BRANCH
    assert error.value.expected == REVISION
    assert "never forced" in str(error.value)
    # One attempt, no retry, and never a forced second one.
    assert len(runner.pushed) == 1
    assert transport.operations == ()
    assert not any("push" in argv for argv in runner.pushed[1:])


def test_a_permission_failure_is_typed_not_retried() -> None:
    from tactus.integrations.github.transport import GitHubPermissionDeniedError

    runner = RecordingRunner(
        {
            "remote get-url": GhCommandResult(0, stdout=REMOTE),
            f"{REVISION}:refs/heads/{BRANCH}": GhCommandResult(
                returncode=1,
                stderr="remote: Permission to Walsamer/tactus.git denied to app.",
            ),
        }
    )
    transport = make_transport(runner)

    with pytest.raises(GitHubPermissionDeniedError):
        transport.ensure_branch(REPOSITORY, BRANCH, REVISION)
    assert len(runner.pushed) == 1


def test_a_ruleset_rejection_is_a_permission_story_not_a_stale_branch() -> None:
    """A protected-branch hook rejection also prints "failed to push some refs"."""

    from tactus.integrations.github.transport import GitHubBranchConflictError
    runner = RecordingRunner(
        {
            "remote get-url": GhCommandResult(0, stdout=REMOTE),
            f"{REVISION}:refs/heads/{BRANCH}": GhCommandResult(
                returncode=1,
                stderr=(
                    f"! [remote rejected] {REVISION} -> {BRANCH} "
                    "(protected branch hook declined)\n"
                    "error: failed to push some refs to 'github.com:Walsamer/tactus.git'"
                ),
            ),
        }
    )
    transport = make_transport(runner)

    with pytest.raises(GitHubPermissionDeniedError) as error:
        transport.ensure_branch(REPOSITORY, BRANCH, REVISION)

    assert not isinstance(error.value, GitHubBranchConflictError)


def test_publishing_onto_the_trunk_is_refused_before_any_command() -> None:
    for trunk in ("main", "master", "MAIN"):
        runner = RecordingRunner()
        transport = make_transport(runner)

        with pytest.raises(GitHubProtectedDestinationError) as error:
            transport.ensure_branch(REPOSITORY, trunk, REVISION)

        assert error.value.ref == trunk
        assert runner.calls == []


def test_publishing_onto_the_authorized_base_branch_is_refused() -> None:
    runner = RecordingRunner()
    transport = make_transport(
        runner, authorized_base_branch="develop", authorized_base_revision=BASE
    )

    with pytest.raises(GitHubProtectedDestinationError):
        transport.ensure_branch(REPOSITORY, "develop", REVISION)
    assert runner.calls == []


def test_a_pull_request_against_another_base_is_refused_before_any_command() -> None:
    runner = RecordingRunner()
    transport = make_transport(runner)

    with pytest.raises(GitHubProtectedDestinationError) as error:
        transport.create_pull_request(
            REPOSITORY,
            head_branch=BRANCH,
            base_branch="release/2.x",
            head_revision=REVISION,
            expected_base_revision=BASE,
            publication_id=MARKER,
            title="t",
            body="b",
        )

    assert error.value.ref == "release/2.x"
    assert runner.calls == []


def test_a_pull_request_for_another_base_revision_is_refused() -> None:
    runner = RecordingRunner()
    transport = make_transport(runner)

    with pytest.raises(GitHubProtectedDestinationError):
        transport.create_pull_request(
            REPOSITORY,
            head_branch=BRANCH,
            base_branch="main",
            head_revision=REVISION,
            expected_base_revision="d" * 40,
            publication_id=MARKER,
            title="t",
            body="b",
        )
    assert runner.calls == []


def test_a_pull_request_with_the_trunk_as_head_is_refused() -> None:
    runner = RecordingRunner()
    transport = make_transport(runner)

    with pytest.raises(GitHubProtectedDestinationError):
        transport.create_pull_request(
            REPOSITORY,
            head_branch="main",
            base_branch="main",
            head_revision=REVISION,
            expected_base_revision=BASE,
            publication_id=MARKER,
            title="t",
            body="b",
        )
    assert runner.calls == []


def test_a_moved_base_is_detected_before_creation() -> None:
    runner = RecordingRunner(
        {
            "pulls?state=all": ok([]),
            "git/ref/heads/main": ref("e" * 40),
        }
    )
    transport = make_transport(runner)

    with pytest.raises(GitHubBaseMovedError) as error:
        transport.create_pull_request(
            REPOSITORY,
            head_branch=BRANCH,
            base_branch="main",
            head_revision=REVISION,
            expected_base_revision=BASE,
            publication_id=MARKER,
            title="t",
            body="b",
        )

    assert error.value.actual == "e" * 40
    assert not any("--method" in argv for argv, _ in runner.calls)


def test_a_created_pull_request_carries_the_publication_marker() -> None:
    created = {
        "number": 7,
        "html_url": f"https://github.com/{REPOSITORY}/pull/7",
        "title": "t",
        "body": f"Tactus publication marker: {MARKER}\n",
        "head": {"ref": BRANCH, "sha": REVISION},
        "base": {"ref": "main", "sha": BASE},
    }
    runner = RecordingRunner(
        {
            "pulls?state=all": ok([]),
            "git/ref/heads/main": ref(BASE),
            "--method POST": ok(created),
        }
    )
    transport = make_transport(runner)

    pull_request = transport.create_pull_request(
        REPOSITORY,
        head_branch=BRANCH,
        base_branch="main",
        head_revision=REVISION,
        expected_base_revision=BASE,
        publication_id=MARKER,
        title="t",
        body=f"Tactus publication marker: {MARKER}",
    )

    assert pull_request.number == 7
    assert pull_request.publication_id == MARKER
    assert pull_request.head_revision == REVISION
    assert transport.operations[0].kind == "create_pull_request"
    assert transport.network_calls == len(runner.calls)


def test_a_lost_acknowledgment_is_reported_for_reconciliation() -> None:
    existing = {
        "number": 8,
        "html_url": f"https://github.com/{REPOSITORY}/pull/8",
        "title": "t",
        "body": f"Tactus publication marker: {MARKER}",
        "head": {"ref": BRANCH, "sha": REVISION},
        "base": {"ref": "main", "sha": BASE},
    }
    runner = RecordingRunner(
        {
            # No matching pull request before the create; GitHub holds it after
            # the create, whose acknowledgment is lost.
            "pulls?state=all": [ok([]), ok([existing])],
            "git/ref/heads/main": ref(BASE),
            "--method POST": GhCommandResult(returncode=1, stderr="EOF"),
        }
    )
    transport = make_transport(runner)

    with pytest.raises(GitHubAcknowledgmentLostError) as error:
        transport.create_pull_request(
            REPOSITORY,
            head_branch=BRANCH,
            base_branch="main",
            head_revision=REVISION,
            expected_base_revision=BASE,
            publication_id=MARKER,
            title="t",
            body="b",
        )

    assert error.value.pull_request is not None
    assert error.value.pull_request.number == 8


def test_a_missing_ref_reads_as_absent() -> None:
    runner = RecordingRunner(
        {"git/ref/heads/gone": GhCommandResult(1, stderr="gh: Not Found (HTTP 404)")}
    )
    transport = make_transport(runner)

    assert transport.branch_revision(REPOSITORY, "gone") is None


def test_an_unbound_credential_refuses_before_any_command() -> None:
    runner = RecordingRunner()
    transport = GhGitHubTransport(
        source_path="/tmp",
        authorized_base_branch="main",
        authorized_base_revision=BASE,
        runner=runner,
    )

    with pytest.raises(GitHubTransportError) as error:
        transport.ensure_branch(REPOSITORY, BRANCH, REVISION)

    assert "credential" in str(error.value)
    assert runner.calls == []


def test_a_relative_source_path_is_refused() -> None:
    with pytest.raises(GitHubTransportError):
        GhGitHubTransport(
            source_path="relative/path",
            authorized_base_branch="main",
            authorized_base_revision=BASE,
            runner=RecordingRunner(),
        )
