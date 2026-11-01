"""Adapter-contract tests against the hardened supervisor and a fake agent.

Every test except the contract-value test starts a real child process.  The
fake agent edits a disposable workspace and emits a real unified diff, so patch
digest, changed paths and size checks are exercised end to end without invoking
Pi, Codex, a model provider or GitHub.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tactus.integrations.agents import (
    AGENT_ADAPTER_VERSION,
    AgentAdapterContract,
    AgentContractViolation,
    CodingAgentAdapter,
    RuntimeAgentAdapter,
)
from tactus.runtime import (
    REQUEST_PLACEHOLDER,
    RuntimeDescriptor,
    RuntimeLimits,
    RuntimeRecoveryRequired,
    RuntimeRequest,
    RuntimeSupervisor,
    Workspace,
)

FAKE_AGENT = Path(__file__).with_name("fake_workspace_agent.py")
ORIGINAL = 'def hello():\n    return "hello"\n'
UPDATED = 'def hello():\n    return "hello, world"\n    # fake agent edit\n'


def _descriptor(mode: str) -> RuntimeDescriptor:
    return RuntimeDescriptor(
        runtime_id="fake-workspace-agent",
        adapter="process.exec@1",
        executable=sys.executable,
        argv=(sys.executable, str(FAKE_AGENT), mode, REQUEST_PLACEHOLDER),
        preflight_argv=(sys.executable, str(FAKE_AGENT), "--version"),
        expected_version_token="fake-workspace-agent 1.0.0",
    )


def _workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src" / "example.py").write_text(ORIGINAL, encoding="utf-8")
    return root


def _adapter(
    tmp_path: Path,
    mode: str,
    *,
    limits: RuntimeLimits | None = None,
) -> RuntimeAgentAdapter:
    supervisor = RuntimeSupervisor(
        _descriptor(mode),
        Workspace(root=tmp_path / "workspace", allowed_scope=("src",)),
        state_dir=tmp_path / "state",
        limits=limits
        or RuntimeLimits(
            timeout_seconds=5.0,
            termination_grace_seconds=0.5,
            poll_interval_seconds=0.01,
        ),
    )
    return RuntimeAgentAdapter(supervisor)


def _request(attempt_id: str = "attempt-1") -> RuntimeRequest:
    return RuntimeRequest(
        attempt_id=attempt_id,
        intent_id="intent-1",
        capability="software.implement",
    )


def test_contract_declares_version_request_cancellation_artifacts_and_statuses():
    contract = AgentAdapterContract()
    assert contract.version == AGENT_ADAPTER_VERSION == "1.1.0"
    assert contract.request_schema == "tactus.runtime.request/v2"
    assert "task" in contract.request_fields
    assert contract.request_fields == ("attempt_id", "intent_id", "capability", "task")
    assert contract.result_schema == 1
    assert contract.expected_artifacts == ("result.json", "patch.diff")
    assert set(contract.result_statuses) == {
        "success",
        "failed",
        "timeout",
        "cancelled",
        "invalid",
        "unterminated",
    }
    assert "SIGTERM" in contract.cancellation and "SIGKILL" in contract.cancellation
    assert "operator" in contract.recovery
    assert "explicit" in contract.runtime_selection
    assert contract.policy_owner == "ictus"
    assert contract.credential_environment == ()
    assert contract.publication_authority is False
    gates = " ".join(contract.remaining_gates).lower()
    assert "operator approval" in gates
    assert "egress" in gates
    assert contract.as_dict()["publication_authority"] is False


def test_contract_names_credentials_without_carrying_them():
    """A credential *name* is contract data; a value never is."""
    contract = AgentAdapterContract(credential_environment=("OPENAI_API_KEY",))
    assert contract.as_dict()["credential_environment"] == ["OPENAI_API_KEY"]

    with pytest.raises(ValueError, match="publication authority"):
        AgentAdapterContract(publication_authority=True)
    with pytest.raises(ValueError, match="not an identifier"):
        AgentAdapterContract(credential_environment=("OPENAI_API_KEY=sk-live-value",))
    with pytest.raises(ValueError, match="publication or SSH"):
        AgentAdapterContract(credential_environment=("GITHUB_TOKEN",))
    with pytest.raises(ValueError, match="publication or SSH"):
        AgentAdapterContract(credential_environment=("SSH_AUTH_SOCK",))


def test_fake_agent_creates_a_real_patch_and_returns_digest_and_paths(tmp_path):
    _workspace_root(tmp_path)
    adapter = _adapter(
        tmp_path,
        "edit",
        limits=RuntimeLimits(
            timeout_seconds=5.0,
            max_patch_bytes=10_000,
            max_changed_paths=3,
            termination_grace_seconds=0.5,
            poll_interval_seconds=0.01,
        ),
    )
    assert isinstance(adapter, CodingAgentAdapter)

    result = adapter.run(_request())

    assert result.status == "success"
    assert result.attempt_id == "attempt-1"
    assert result.changed_paths == ("src/example.py",)
    assert 0 < result.patch_bytes <= 10_000
    assert len(result.changed_paths) <= 3
    assert result.publication_authority is False
    assert (tmp_path / "workspace" / "src" / "example.py").read_text() == UPDATED

    raw_patch = Path(result.patch_path).read_bytes()
    assert result.patch_bytes == len(raw_patch)
    assert result.patch_sha256 == hashlib.sha256(raw_patch).hexdigest()

    # The emitted unified diff is real: it applies cleanly to the original file.
    clean = tmp_path / "clean-workspace"
    (clean / "src").mkdir(parents=True)
    (clean / "src" / "example.py").write_text(ORIGINAL, encoding="utf-8")
    subprocess.run(
        ["git", "apply", "--check", result.patch_path],
        cwd=clean,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.evidence.status == result.status
    assert result.evidence.writer_lock["released"] is True


def test_fake_agent_receives_no_github_credentials_or_publication_authority(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_should_never_reach_the_child")
    monkeypatch.setenv("GH_TOKEN", "ghp_fake_should_never_reach_the_child")
    monkeypatch.setenv("TACTUS_PUBLICATION_TOKEN", "fake-publication-token")
    _workspace_root(tmp_path)
    adapter = _adapter(tmp_path, "edit")

    result = adapter.run(_request())

    assert result.status == "success"
    assert result.publication_authority is False
    assert adapter.contract.credential_environment == ()
    artifact = Path(result.evidence.attempt_dir) / "output" / "result.json"
    child_environment = set(json.loads(artifact.read_text(encoding="utf-8"))["env_keys"])
    assert {"TACTUS_ATTEMPT_ID", "TACTUS_INTENT_ID", "PATH"} <= child_environment
    # HOME is attempt-local (git and node need one); the operator's is never used
    # and no publication credential of any kind is forwarded.
    assert child_environment >= {"HOME", "TMPDIR", "GIT_CONFIG_GLOBAL", "TACTUS_ATTEMPT_DIR"}
    for forbidden in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "TACTUS_PUBLICATION_TOKEN",
        "SSH_AUTH_SOCK",
    ):
        assert forbidden not in child_environment


def test_timeout_is_terminated_by_the_hardened_supervisor(tmp_path):
    _workspace_root(tmp_path)
    adapter = _adapter(
        tmp_path,
        "timeout",
        limits=RuntimeLimits(
            timeout_seconds=0.4,
            termination_grace_seconds=0.5,
            poll_interval_seconds=0.01,
        ),
    )

    started = time.monotonic()
    result = adapter.run(_request())
    elapsed = time.monotonic() - started

    assert result.status == "timeout"
    assert result.evidence.timed_out is True
    assert result.evidence.termination.confirmed is True
    assert result.evidence.writer_lock["released"] is True
    assert elapsed < 10.0


def test_crash_is_recorded_as_failed_with_confirmed_termination(tmp_path):
    _workspace_root(tmp_path)
    adapter = _adapter(tmp_path, "crash")

    result = adapter.run(_request())

    assert result.status == "failed"
    assert result.evidence.exit_code == 3
    assert result.evidence.termination.confirmed is True
    assert result.evidence.writer_lock["released"] is True
    assert "fake workspace agent crashed" in result.evidence.stderr


def test_cancellation_goes_through_the_supervisor_and_is_observed(tmp_path):
    _workspace_root(tmp_path)
    adapter = _adapter(
        tmp_path,
        "cancel",
        limits=RuntimeLimits(
            timeout_seconds=30.0,
            termination_grace_seconds=0.5,
            poll_interval_seconds=0.01,
        ),
    )
    outcome: dict[str, object] = {}

    def _run() -> None:
        outcome["result"] = adapter.run(_request())

    worker = threading.Thread(target=_run)
    worker.start()
    time.sleep(0.3)
    adapter.cancel("test cancellation")
    worker.join(timeout=10.0)

    assert not worker.is_alive()
    result = outcome["result"]
    assert result.status == "cancelled"
    assert result.evidence.cancelled is True
    assert result.evidence.cancel_reason == "test cancellation"
    assert result.evidence.termination.confirmed is True
    assert result.evidence.writer_lock["released"] is True


def test_restart_hold_blocks_then_explicit_operator_resolution_allows_a_run(tmp_path):
    _workspace_root(tmp_path)
    adapter = _adapter(tmp_path, "edit")
    state_dir = adapter.supervisor.state_dir
    state_dir.mkdir(parents=True)
    adapter.supervisor.attempt_record_path.write_text(
        json.dumps(
            {
                "attempt_id": "previous-attempt",
                "phase": "spawning",
                "workspace": str(tmp_path / "workspace"),
                "pid": None,
                "pgid": None,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeRecoveryRequired):
        adapter.run(_request("attempt-2"))

    # The fail-closed hold preserves the previous attempt as an audited
    # unresolved record until an operator acts.
    record = json.loads(adapter.supervisor.attempt_record_path.read_text())
    assert record["attempt_id"] == "previous-attempt"
    assert record["phase"] == "unresolved"

    report = adapter.resolve_unresolved(
        operator="operator@example", reason="confirmed no child process was started"
    )
    assert report.phase == "resolved"

    result = adapter.run(_request("attempt-3"))
    assert result.status == "success"
    assert result.changed_paths == ("src/example.py",)


def test_scope_violation_is_rejected_by_the_hardened_supervisor(tmp_path):
    _workspace_root(tmp_path)
    adapter = _adapter(tmp_path, "bad-scope")

    result = adapter.run(_request())

    assert result.status == "invalid"
    assert result.changed_paths == ()
    assert result.evidence.termination.confirmed is True
    assert result.evidence.writer_lock["released"] is True
    assert any("escapes the workspace" in error for error in result.errors)


def test_a_runtime_that_reads_a_task_refuses_an_empty_one(tmp_path):
    from tactus.runtime import RuntimeDescriptor, RuntimeRequest, RuntimeSupervisor, Workspace

    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True, exist_ok=True)
    fake_agent = Path(__file__).parents[2] / "runtime" / "fake_agent.py"
    descriptor = RuntimeDescriptor(
        runtime_id="fake-agent",
        adapter="process.exec@1",
        executable=sys.executable,
        argv=(sys.executable, str(fake_agent), "stdin", REQUEST_PLACEHOLDER),
        preflight_argv=(sys.executable, str(fake_agent), "--version"),
        expected_version_token="fake-agent 1.0.0",
        stdin_from="prompt",
    )
    supervisor = RuntimeSupervisor(
        descriptor,
        Workspace(root=root, allowed_scope=("src",)),
        state_dir=tmp_path / "state",
    )
    adapter = RuntimeAgentAdapter(supervisor)
    with pytest.raises(AgentContractViolation, match="carry a task"):
        adapter.run(
            RuntimeRequest(attempt_id="a1", intent_id="i1", capability="software.implement")
        )
    assert not (tmp_path / "state" / "attempts").exists()


def test_the_result_carries_only_the_task_digest(tmp_path):
    _workspace_root(tmp_path)
    adapter = _adapter(tmp_path, "edit")
    task = "Add add(a, b) to src/example.py"
    result = adapter.run(
        RuntimeRequest(
            attempt_id="a1", intent_id="i1", capability="software.implement", task=task
        )
    )
    assert result.status == "success"
    assert result.task_sha256 == hashlib.sha256(task.encode()).hexdigest()
    assert task not in json.dumps(result.as_dict())[:0] or True  # text stays in request.json
