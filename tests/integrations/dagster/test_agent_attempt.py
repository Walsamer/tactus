"""Agent-attempt tests: pin, supervision and patch evidence (Dagster-free).

Every test except the pin validation ones starts a real child process. The
runtime used is the deterministic **fake** coding agent, because a real provider
model and an OS execution-permission boundary remain open gates (Issue #22,
B2). Nothing here imports Dagster: the module under test is Dagster-free, so it
runs in Tactus's own test environment, while the join itself is proven by
``scripts/run-agent-slice.sh`` against the pinned Ictus bridge.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from tactus.runtime import WorkspaceScopeError

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "integrations" / "dagster" / "agent_attempt.py"
SHIPPED_CONFIG = REPO_ROOT / "config" / "agent-runtime.json"
FAKE_AGENT = REPO_ROOT / "tests" / "integrations" / "agents" / "fake_workspace_agent.py"

ORIGINAL = 'def hello():\n    return "hello"\n'
UPDATED = 'def hello():\n    return "hello, world"\n    # fake agent edit\n'


def _load():
    spec = importlib.util.spec_from_file_location("tactus_agent_attempt", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


attempt = _load()


def _shipped() -> dict:
    return json.loads(SHIPPED_CONFIG.read_text(encoding="utf-8"))


def _config(*, mode: str = "edit", **overrides) -> dict:
    config = _shipped()
    config["argv"] = ["{executable}", mode, "{request}"]
    config.update(overrides)
    return config


def _write_config(tmp_path: Path, config: dict) -> Path:
    path = tmp_path / "agent-runtime.json"
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src" / "example.py").write_text(ORIGINAL, encoding="utf-8")
    return root


def _run(tmp_path: Path, *, mode: str = "edit", limits: dict | None = None, **overrides):
    config = _config(mode=mode, **overrides)
    if limits is not None:
        config["limits"] = {**_shipped()["limits"], **limits}
    config_path = _write_config(tmp_path, config)
    return attempt.run_agent_attempt(
        intent_id="intent:agent-slice",
        capability="software.implement",
        attempt=1,
        workspace_root=_workspace(tmp_path),
        repo_root=REPO_ROOT,
        config_path=config_path,
        state_dir=tmp_path / "state",
    )


# -- the pin ---------------------------------------------------------------


def test_shipped_config_pins_the_repository_agent_exactly():
    """The committed pin must describe the committed runtime (drift is a failure)."""

    pin = attempt.load_agent_runtime_pin(SHIPPED_CONFIG, repo_root=REPO_ROOT)

    assert pin.runtime_id == "fake-workspace-agent"
    assert pin.runtime_kind == "fake-canary"
    assert pin.executable == FAKE_AGENT.resolve()
    assert pin.executable_sha256 == hashlib.sha256(FAKE_AGENT.read_bytes()).hexdigest()
    assert pin.allowed_scope == ("src",)
    assert pin.expected_version_token == "fake-workspace-agent 1.0.0"
    assert pin.argv[0] == str(FAKE_AGENT.resolve())
    assert attempt.REQUEST_PLACEHOLDER in pin.argv
    assert pin.limits.timeout_seconds > 0 and pin.limits.max_changed_paths >= 1
    # The pin is data for a descriptor the supervisor can execute unchanged.
    descriptor = pin.descriptor()
    assert descriptor.executable == str(FAKE_AGENT.resolve())
    assert descriptor.command("/tmp/request.json")[-1] == "/tmp/request.json"


def test_pin_rejects_a_runtime_whose_digest_changed(tmp_path):
    config_path = _write_config(tmp_path, _config(executable_sha256="0" * 64))

    with pytest.raises(attempt.AgentRuntimePinError, match="unpinned runtime"):
        attempt.load_agent_runtime_pin(config_path, repo_root=REPO_ROOT)


def test_pin_rejects_a_missing_or_unusable_executable(tmp_path):
    missing = _write_config(tmp_path, _config(executable="tests/does-not-exist.py"))
    with pytest.raises(attempt.AgentRuntimePinError, match="not found"):
        attempt.load_agent_runtime_pin(missing, repo_root=REPO_ROOT)


def test_pin_rejects_unknown_keys_missing_keys_and_bad_argv(tmp_path):
    unknown = _config()
    unknown["provider"] = "silent-fallback"
    with pytest.raises(attempt.AgentRuntimePinError, match="unknown keys"):
        attempt.load_agent_runtime_pin(_write_config(tmp_path, unknown), repo_root=REPO_ROOT)

    incomplete = _config()
    incomplete.pop("allowed_scope")
    with pytest.raises(attempt.AgentRuntimePinError, match="missing required keys"):
        attempt.load_agent_runtime_pin(
            _write_config(tmp_path, incomplete), repo_root=REPO_ROOT
        )

    no_request = _config()
    no_request["argv"] = ["{executable}", "edit"]
    with pytest.raises(attempt.AgentRuntimePinError, match="request"):
        attempt.load_agent_runtime_pin(
            _write_config(tmp_path, no_request), repo_root=REPO_ROOT
        )

    wrong_schema = _config(schema_version=2)
    with pytest.raises(attempt.AgentRuntimePinError, match="schema_version"):
        attempt.load_agent_runtime_pin(
            _write_config(tmp_path, wrong_schema), repo_root=REPO_ROOT
        )


def test_unpinned_runtime_is_refused_before_anything_is_touched(tmp_path):
    workspace = _workspace(tmp_path)
    config_path = _write_config(tmp_path, _config(executable_sha256="f" * 64))

    with pytest.raises(attempt.AgentRuntimePinError):
        attempt.run_agent_attempt(
            intent_id="intent:agent-slice",
            capability="software.implement",
            attempt=1,
            workspace_root=workspace,
            repo_root=REPO_ROOT,
            config_path=config_path,
            state_dir=tmp_path / "state",
        )

    assert not (tmp_path / "state" / "attempts").exists()
    assert (workspace / "src" / "example.py").read_text(encoding="utf-8") == ORIGINAL


# -- one supervised attempt ------------------------------------------------


def test_attempt_runs_the_pinned_agent_and_reports_patch_evidence(tmp_path):
    summary = _run(tmp_path)

    assert summary["status"] == "success"
    assert summary["attempt_id"] == "intent:agent-slice.attempt-1"
    assert summary["capability"] == "software.implement"
    assert summary["contract_version"] == "1.1.0"
    assert summary["runtime"]["runtime_id"] == "fake-workspace-agent"
    assert summary["runtime"]["runtime_kind"] == "fake-canary"
    assert (
        summary["runtime"]["observed_executable_sha256"]
        == summary["runtime"]["pinned_executable_sha256"]
    )
    assert summary["runtime"]["reported_version"] == "fake-workspace-agent 1.0.0"
    assert summary["artifacts"]["changed_paths"] == ["src/example.py"]
    assert summary["publication_authority"] is False
    assert summary["isolation"].startswith("none")
    # Scope detection is limited to what the runtime declares and the patch
    # headers; without a workspace diff provider the account must say so.
    assert summary["write_scope"]["verified"] is False
    assert "no workspace diff provider" in summary["write_scope"]["reason"]
    assert summary["termination"]["confirmed"] is True
    assert summary["writer_lock"]["released"] is True

    patch_path = Path(summary["artifacts"]["patch_path"])
    raw = patch_path.read_bytes()
    assert summary["artifacts"]["patch_bytes"] == len(raw)
    assert summary["artifacts"]["patch_sha256"] == hashlib.sha256(raw).hexdigest()
    assert (tmp_path / "workspace" / "src" / "example.py").read_text() == UPDATED

    # The account is durable next to the supervisor's own artifacts.
    summary_path = Path(summary["process"]["summary_path"])
    assert summary_path == Path(summary["process"]["attempt_dir"]) / attempt.ATTEMPT_SUMMARY_FILE
    on_disk = json.loads(summary_path.read_text())
    assert on_disk["artifacts"]["patch_sha256"] == summary["artifacts"]["patch_sha256"]


def test_a_run_config_naming_another_capability_is_refused(tmp_path):
    """The capability is a route constant; an incoming label can only match it."""

    assert attempt.assert_route_capability("software.implement", "software.implement") == (
        "software.implement"
    )
    for label in ("software.promote", "demo.verify", "", None):
        with pytest.raises(attempt.AgentRouteMismatch, match="implements"):
            attempt.assert_route_capability(label, "software.implement")
    assert issubclass(attempt.AgentRouteMismatch, attempt.AgentAttemptError)


def test_attempt_refuses_a_workspace_inside_the_repository(tmp_path):
    """The agent's workspace may not be the checkout its own code lives in."""

    config_path = _write_config(tmp_path, _config())
    with pytest.raises(WorkspaceScopeError, match="forbidden root"):
        attempt.run_agent_attempt(
            intent_id="intent:agent-slice",
            capability="software.implement",
            attempt=1,
            workspace_root=REPO_ROOT / "src",
            repo_root=REPO_ROOT,
            config_path=config_path,
            state_dir=tmp_path / "state",
        )
    assert not (tmp_path / "state" / "attempts").exists()


def test_attempt_crash_reports_failure_and_releases_the_writer_lock(tmp_path):
    summary = _run(tmp_path, mode="crash")

    assert summary["status"] == "failed"
    assert summary["process"]["exit_code"] == 3
    assert summary["process"]["timed_out"] is False
    assert summary["termination"]["confirmed"] is True
    assert summary["writer_lock"]["released"] is True
    assert summary["artifacts"]["changed_paths"] == []
    # A failed attempt still leaves its evidence behind.
    assert Path(summary["process"]["attempt_dir"]).is_dir()


def test_attempt_timeout_terminates_the_process_group(tmp_path):
    started = time.monotonic()
    summary = _run(
        tmp_path,
        mode="timeout",
        limits={"timeout_seconds": 0.6, "termination_grace_seconds": 0.5},
    )
    elapsed = time.monotonic() - started

    assert summary["status"] == "timeout"
    assert summary["process"]["timed_out"] is True
    assert summary["termination"]["confirmed"] is True
    assert summary["termination"]["alive_after"] is False
    assert summary["writer_lock"]["released"] is True
    # The group is demonstrably gone, not merely reported as terminated.
    pid = summary["process"]["pgid"]
    assert pid is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)
    assert elapsed < 15.0


def test_attempt_rejects_a_changed_path_outside_the_workspace_scope(tmp_path):
    summary = _run(tmp_path, mode="bad-scope")

    assert summary["status"] == "invalid"
    # The supervisor detects the escaped path and the undeclared patch header after
    # the fact (this is scope detection, not OS isolation).
    joined = " ".join(summary["errors"])
    assert "escapes the workspace" in joined
    assert "not declared" in joined
    assert summary["writer_lock"]["released"] is True


def test_attempt_summary_is_json_safe_and_carries_no_publication_authority(tmp_path):
    summary = _run(tmp_path)

    encoded = json.dumps(summary, sort_keys=True)
    assert "publication_authority" in encoded
    assert summary["publication_authority"] is False
    assert "PUBLICATION_TOKEN" not in encoded
    assert "GITHUB_TOKEN" not in encoded
    assert Path(summary["process"]["attempt_dir"]).name.startswith("intent-agent-slice")


# -- workspace scope is data, not a guess ----------------------------------


def test_each_attempt_gets_its_own_evidence_directory_and_the_pin_scope(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "docs").mkdir()
    config = _config(allowed_scope=["src"])
    config_path = _write_config(tmp_path, config)

    digests = set()
    for _ in range(2):
        summary = attempt.run_agent_attempt(
            intent_id="intent:scope",
            capability="software.implement",
            attempt=1,
            workspace_root=workspace,
            repo_root=REPO_ROOT,
            config_path=config_path,
            state_dir=tmp_path / "state",
        )
        assert summary["workspace"]["allowed_scope"] == ["src"]
        digests.add(summary["process"]["attempt_dir"])
    # Distinct attempts never share an evidence directory.
    assert len(digests) == 2


def test_fixture_agent_is_executable_and_self_reporting():
    """The pin is only honest if the pinned file can actually be executed."""

    assert os.access(FAKE_AGENT, os.X_OK)
    assert shutil.which("python3") is not None
    assert FAKE_AGENT.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3")


# -- phase 2: containment, artifacts, credentials in the operator pin --------


def _sandbox_launcher():
    found = shutil.which("sandbox-exec")
    if found is None:
        pytest.skip("seatbelt launcher is not available on this host")
    return attempt.Launcher(path=Path(found), argv_prefix=(found, "-f", "{profile}"))


def _containment_block(tmp_path: Path) -> dict:
    launcher = _sandbox_launcher()
    denied = tmp_path / "home"
    denied.mkdir(exist_ok=True)
    return {
        "launcher": {"path": str(launcher.path), "argv_prefix": list(launcher.argv_prefix)},
        "denied_roots": [{"path": str(denied), "reason": "operator home"}],
        "egress": {"mode": "deny"},
        "allowances": [],
    }


def test_the_pin_can_declare_containment_and_the_account_records_it(tmp_path):
    config_path = _write_config(
        tmp_path, _config(containment=_containment_block(tmp_path))
    )
    summary = attempt.run_agent_attempt(
        intent_id="intent:contained",
        capability="software.implement",
        attempt=1,
        workspace_root=_workspace(tmp_path),
        repo_root=REPO_ROOT,
        config_path=config_path,
        state_dir=tmp_path / "state",
    )

    assert summary["status"] == "success"
    assert summary["isolation"].startswith("enforced")
    containment = summary["containment"]
    assert containment["enabled"] is True
    assert containment["self_test"]["ok"] is True
    assert containment["policy"]["egress"]["mode"] == "deny"


def test_a_pin_without_containment_says_so_in_the_account(tmp_path):
    summary = _run(tmp_path)
    assert summary["isolation"].startswith("none")
    assert summary["containment"].get("enabled") is False


def test_the_pin_can_pin_a_tree_artifact(tmp_path):
    tree = tmp_path / "runtime-tree"
    (tree / "bin").mkdir(parents=True)
    agent = tree / "bin" / "agent"
    agent.write_text("#!/bin/sh\necho tree-agent 1.0.0\n", encoding="utf-8")
    agent.chmod(0o755)
    digest, count = attempt.digest_tree(tree)
    config_path = _write_config(
        tmp_path,
        _config(
            executable=str(agent),
            executable_sha256=hashlib.sha256(agent.read_bytes()).hexdigest(),
            argv=[str(agent), "run", "{request}"],
            preflight_argv=[str(agent), "--version"],
            expected_version_token="tree-agent 1.0.0",
            artifacts=[
                {
                    "path": str(tree),
                    "sha256": digest,
                    "kind": "tree",
                    "file_count": count,
                    "entrypoint": "bin/agent",
                }
            ],
        ),
    )
    pin = attempt.load_agent_runtime_pin(config_path, repo_root=REPO_ROOT)
    assert pin.artifacts[0].kind == "tree"
    assert pin.artifacts[0].file_count == count
    assert pin.descriptor().artifacts[0].sha256 == digest


def test_a_named_credential_missing_from_the_environment_fails_at_preflight(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("TACTUS_TEST_MODEL_KEY", raising=False)
    config_path = _write_config(
        tmp_path, _config(env_allowlist=["TACTUS_TEST_MODEL_KEY"])
    )
    with pytest.raises(attempt.AgentRuntimePinError, match="TACTUS_TEST_MODEL_KEY"):
        attempt.run_agent_attempt(
            intent_id="intent:key",
            capability="software.implement",
            attempt=1,
            workspace_root=_workspace(tmp_path),
            repo_root=REPO_ROOT,
            config_path=config_path,
            state_dir=tmp_path / "state",
        )
    # Fail-closed and clean: no attempt directory was created.
    assert not (tmp_path / "state" / "attempts").exists()


def test_a_named_credential_reaches_the_child_by_name_only(tmp_path, monkeypatch):
    monkeypatch.setenv("TACTUS_TEST_MODEL_KEY", "sk-from-environment")
    config_path = _write_config(
        tmp_path, _config(env_allowlist=["TACTUS_TEST_MODEL_KEY"])
    )
    summary = attempt.run_agent_attempt(
        intent_id="intent:key-ok",
        capability="software.implement",
        attempt=1,
        workspace_root=_workspace(tmp_path),
        repo_root=REPO_ROOT,
        config_path=config_path,
        state_dir=tmp_path / "state",
    )
    assert summary["status"] == "success"
    assert "sk-from-environment" not in json.dumps(summary)


def test_an_unknown_containment_key_is_refused(tmp_path):
    block = _containment_block(tmp_path)
    block["surprise"] = True
    config_path = _write_config(tmp_path, _config(containment=block))
    with pytest.raises(attempt.AgentRuntimePinError, match="unknown keys"):
        attempt.load_agent_runtime_pin(config_path, repo_root=REPO_ROOT)


def test_the_task_is_recorded_as_a_digest_in_the_account(tmp_path):
    task = "make the greeting explicit"
    config_path = _write_config(tmp_path, _config())
    summary = attempt.run_agent_attempt(
        intent_id="intent:task",
        capability="software.implement",
        attempt=1,
        workspace_root=_workspace(tmp_path),
        task=task,
        repo_root=REPO_ROOT,
        config_path=config_path,
        state_dir=tmp_path / "state",
    )
    assert summary["task_sha256"] == hashlib.sha256(task.encode()).hexdigest()
    assert task not in json.dumps({k: v for k, v in summary.items() if k != "request"})
