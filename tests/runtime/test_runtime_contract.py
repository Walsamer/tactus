"""Runtime preflight and workspace-scope contract tests (no agent execution)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from tactus.runtime import (
    REQUEST_PLACEHOLDER,
    RuntimeDescriptor,
    RuntimePreflightError,
    Workspace,
    WorkspaceScopeError,
    normalize_changed_path,
    patch_header_paths,
    preflight,
    validate_changed_paths,
)
from tactus.runtime.containment import Launcher

FAKE_AGENT = Path(__file__).with_name("fake_agent.py")


def descriptor(**overrides) -> RuntimeDescriptor:
    values = {
        "runtime_id": "fake-agent",
        "adapter": "process.exec@1",
        "executable": sys.executable,
        "argv": (sys.executable, str(FAKE_AGENT), "success", REQUEST_PLACEHOLDER),
        "preflight_argv": (sys.executable, str(FAKE_AGENT), "--version"),
        "expected_version_token": "fake-agent 1.0.0",
    }
    values.update(overrides)
    return RuntimeDescriptor(**values)


# -- descriptor --------------------------------------------------------------


def test_descriptor_materialises_the_request_placeholder():
    runtime = descriptor()
    assert runtime.command("/tmp/request.json")[-1] == "/tmp/request.json"
    assert REQUEST_PLACEHOLDER not in runtime.command("/tmp/request.json")


def test_preflight_records_the_pinned_runtime_provenance():
    provenance = preflight(descriptor())

    assert provenance.runtime_id == "fake-agent"
    assert provenance.adapter == "process.exec@1"
    assert "fake-agent 1.0.0" in provenance.reported_version
    assert len(provenance.executable_sha256) == 64


def test_preflight_fails_closed_on_version_mismatch():
    with pytest.raises(RuntimePreflightError, match="version mismatch"):
        preflight(descriptor(expected_version_token="fake-agent 9.9.9"))


def test_preflight_fails_closed_when_the_executable_is_missing(tmp_path):
    with pytest.raises(RuntimePreflightError, match="not found"):
        preflight(descriptor(executable=str(tmp_path / "no-such-runtime")))


def test_preflight_fails_closed_when_the_executable_is_not_executable(tmp_path):
    candidate = tmp_path / "runtime"
    candidate.write_text("#!/bin/sh\necho fake-agent 1.0.0\n")
    candidate.chmod(stat.S_IRUSR | stat.S_IWUSR)
    with pytest.raises(RuntimePreflightError, match="not executable"):
        preflight(descriptor(executable=str(candidate)))


@pytest.mark.parametrize(
    "overrides, message",
    (
        ({"runtime_id": ""}, "runtime_id"),
        ({"adapter": "  "}, "adapter"),
        ({"executable": "relative/runtime"}, "absolute path"),
        ({"argv": (sys.executable, "agent")}, "exactly one"),
        ({"argv": (sys.executable, "{request}", "{request}")}, "exactly one"),
        ({"preflight_argv": ()}, "preflight_argv"),
        ({"expected_version_token": ""}, "expected_version_token"),
    ),
)
def test_descriptor_rejects_incomplete_pins(overrides, message):
    with pytest.raises(ValueError, match=message):
        descriptor(**overrides)


# -- workspace scope ---------------------------------------------------------


def workspace(tmp_path: Path, *, scope=("src",), forbidden=()) -> Workspace:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True, exist_ok=True)
    return Workspace(root=root, allowed_scope=scope, forbidden_roots=forbidden)


def test_path_normalisation_rejects_escapes():
    assert normalize_changed_path("src/./a/../b.py") == "src/b.py"
    for bad in ("/etc/passwd", "../outside.py", "a/../../b.py", "", "  "):
        with pytest.raises(WorkspaceScopeError):
            normalize_changed_path(bad)


def test_scope_is_prefix_containment_not_string_prefix(tmp_path):
    space = workspace(tmp_path)
    assert space.covers("src/a.py")
    assert space.covers("src/deep/nested/a.py")
    assert space.covers("src")
    assert not space.covers("src2/a.py")
    assert not space.covers("docs/a.md")


def test_workspace_refuses_a_forbidden_root(tmp_path):
    with pytest.raises(WorkspaceScopeError, match="forbidden root"):
        workspace(tmp_path, forbidden=(tmp_path,))


def test_workspace_requires_an_absolute_existing_root(tmp_path):
    with pytest.raises(WorkspaceScopeError, match="absolute"):
        Workspace(root=Path("relative"), allowed_scope=("src",))
    with pytest.raises(WorkspaceScopeError, match="not a directory"):
        Workspace(root=tmp_path / "missing", allowed_scope=("src",))
    with pytest.raises(WorkspaceScopeError, match="non-empty allowed scope"):
        Workspace(root=tmp_path, allowed_scope=())


def test_changed_paths_must_be_declared_inside_the_scope(tmp_path):
    space = workspace(tmp_path)
    assert validate_changed_paths(
        ["src/a.py", "src/a.py", "src/b.py"], workspace=space, max_paths=5
    ) == ("src/a.py", "src/b.py")

    with pytest.raises(WorkspaceScopeError, match="outside the allowed scope"):
        validate_changed_paths(["docs/a.md"], workspace=space, max_paths=5)
    with pytest.raises(WorkspaceScopeError, match="at least|must report"):
        validate_changed_paths([], workspace=space, max_paths=5)
    with pytest.raises(WorkspaceScopeError, match="limit is 1"):
        validate_changed_paths(["src/a.py", "src/b.py"], workspace=space, max_paths=1)


def test_patch_headers_are_extracted_from_both_sides():
    patch = (
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-a\n+b\n"
        "--- /dev/null\n+++ b/src/new.py\n@@ -0,0 +1 @@\n+x\n"
    )
    assert patch_header_paths(patch) == ("src/a.py", "src/new.py")


def test_workspace_evidence_is_json_safe(tmp_path):
    space = workspace(tmp_path)
    assert set(space.as_dict()) == {"root", "allowed_scope", "forbidden_roots"}
    assert os.path.isabs(space.as_dict()["root"])


# -- artifact sets, launcher pinning, placeholders (vendor-free) -------------


def _tree(tmp_path: Path) -> Path:
    tree = tmp_path / "runtime"
    (tree / "bin").mkdir(parents=True)
    (tree / "bin" / "agent").write_text("#!/bin/sh\necho agent-tree 1.0.0\n", encoding="utf-8")
    (tree / "bin" / "agent").chmod(0o755)
    (tree / "lib").mkdir()
    (tree / "lib" / "data").write_text("bytes\n", encoding="utf-8")
    return tree


def test_file_artifact_pin_is_verified_and_recorded(tmp_path: Path):
    from tactus.runtime.containment import digest_tree  # noqa: F401  (kept for parity)

    import hashlib

    artifact_path = tmp_path / "agent"
    artifact_path.write_text("#!/bin/sh\necho agent 1.0.0\n", encoding="utf-8")
    artifact_path.chmod(0o755)
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    from tactus.runtime.descriptor import Artifact

    runtime = descriptor(
        executable=str(artifact_path),
        argv=(str(artifact_path), "run", REQUEST_PLACEHOLDER),
        preflight_argv=(str(artifact_path), "--version"),
        expected_version_token="agent 1.0.0",
        artifacts=(Artifact(path=str(artifact_path), sha256=digest),),
    )
    provenance = preflight(runtime)
    assert provenance.artifacts[0]["sha256"] == digest
    assert provenance.artifacts[0]["kind"] == "file"


def test_tree_artifact_pin_is_verified_with_its_manifest(tmp_path: Path):
    from tactus.runtime.containment import digest_tree
    from tactus.runtime.descriptor import Artifact, TREE_DIGEST_ORDER

    tree = _tree(tmp_path)
    digest, count = digest_tree(tree)
    entry = tree / "bin" / "agent"
    runtime = descriptor(
        executable=str(entry),
        argv=(str(entry), "run", REQUEST_PLACEHOLDER),
        preflight_argv=(str(entry), "--version"),
        expected_version_token="agent-tree 1.0.0",
        artifacts=(
            Artifact(
                path=str(tree),
                sha256=digest,
                kind="tree",
                digest_order=TREE_DIGEST_ORDER,
                file_count=count,
                entrypoint="bin/agent",
            ),
        ),
    )
    provenance = preflight(runtime)
    assert provenance.artifacts[0]["kind"] == "tree"
    assert provenance.artifacts[0]["file_count"] == count
    assert provenance.artifacts[0]["digest_order"] == TREE_DIGEST_ORDER


def test_tree_artifact_digest_drift_fails_closed(tmp_path: Path):
    from tactus.runtime.containment import digest_tree
    from tactus.runtime.descriptor import Artifact

    tree = _tree(tmp_path)
    digest, count = digest_tree(tree)
    (tree / "lib" / "data").write_text("changed\n", encoding="utf-8")
    entry = tree / "bin" / "agent"
    runtime = descriptor(
        executable=str(entry),
        argv=(str(entry), "run", REQUEST_PLACEHOLDER),
        preflight_argv=(str(entry), "--version"),
        expected_version_token="agent-tree 1.0.0",
        artifacts=(
            Artifact(
                path=str(tree),
                sha256=digest,
                kind="tree",
                file_count=count,
                entrypoint="bin/agent",
            ),
        ),
    )
    with pytest.raises(RuntimePreflightError, match="artifact digest mismatch"):
        preflight(runtime)


def test_tree_artifact_file_count_drift_fails_closed(tmp_path: Path):
    from tactus.runtime.containment import digest_tree
    from tactus.runtime.descriptor import Artifact

    tree = _tree(tmp_path)
    digest, count = digest_tree(tree)
    entry = tree / "bin" / "agent"
    runtime = descriptor(
        executable=str(entry),
        argv=(str(entry), "run", REQUEST_PLACEHOLDER),
        preflight_argv=(str(entry), "--version"),
        expected_version_token="agent-tree 1.0.0",
        artifacts=(
            Artifact(
                path=str(tree),
                sha256=digest,
                kind="tree",
                file_count=count + 1,
                entrypoint="bin/agent",
            ),
        ),
    )
    with pytest.raises(RuntimePreflightError, match="files; the pin records"):
        preflight(runtime)


def test_an_artifact_without_a_digest_is_refused(tmp_path: Path):
    from tactus.runtime.descriptor import Artifact

    tree = _tree(tmp_path)
    entry = tree / "bin" / "agent"
    runtime = descriptor(
        executable=str(entry),
        argv=(str(entry), "run", REQUEST_PLACEHOLDER),
        preflight_argv=(str(entry), "--version"),
        expected_version_token="agent-tree 1.0.0",
        artifacts=(Artifact(path=str(tree), sha256="", kind="tree", file_count=2),),
    )
    with pytest.raises(RuntimePreflightError, match="no pinned digest"):
        preflight(runtime)


def test_launcher_is_pinned_and_the_probe_is_deferred_to_the_self_test(
    pinned_launcher: Launcher,
):
    launcher = pinned_launcher
    runtime = descriptor(launcher=launcher, expected_version_token="fake-agent 1.0.0")
    runtime = RuntimeDescriptor(
        runtime_id=runtime.runtime_id,
        adapter=runtime.adapter,
        executable=runtime.executable,
        argv=runtime.argv,
        preflight_argv=runtime.preflight_argv,
        expected_version_token=runtime.expected_version_token,
        launcher=launcher,
    )
    provenance = preflight(runtime)
    assert provenance.probe_mode == "containment-self-test"
    assert provenance.reported_version == ""
    assert len(provenance.launcher_sha256) == 64


def test_launcher_digest_drift_fails_before_anything_runs(pinned_launcher: Launcher):
    from tactus.runtime.containment import ContainmentUnavailable

    launcher = pinned_launcher
    drifted = Launcher(
        path=launcher.path, argv_prefix=launcher.argv_prefix, sha256="0" * 64
    )
    runtime = RuntimeDescriptor(
        runtime_id="fake-agent",
        adapter="process.exec@1",
        executable=sys.executable,
        argv=(sys.executable, str(FAKE_AGENT), "success", REQUEST_PLACEHOLDER),
        preflight_argv=(sys.executable, str(FAKE_AGENT), "--version"),
        expected_version_token="fake-agent 1.0.0",
        launcher=drifted,
    )
    with pytest.raises(ContainmentUnavailable, match="digest mismatch"):
        preflight(runtime)


def test_launcher_command_prefixes_the_pinned_runtime(pinned_launcher: Launcher):
    launcher = pinned_launcher
    runtime = RuntimeDescriptor(
        runtime_id="fake-agent",
        adapter="process.exec@1",
        executable=sys.executable,
        argv=(sys.executable, str(FAKE_AGENT), "success", REQUEST_PLACEHOLDER),
        preflight_argv=(sys.executable, str(FAKE_AGENT), "--version"),
        expected_version_token="fake-agent 1.0.0",
        launcher=launcher,
    )
    command = runtime.command(
        "/tmp/request.json", profile_path="/tmp/profile.sb", workspace="/tmp/ws"
    )
    assert command[0] == str(launcher.path)
    assert command[1] == "-f"
    assert command[2] == "/tmp/profile.sb"
    assert command[3] == sys.executable
    assert command[-1] == "/tmp/request.json"


def test_a_profile_placeholder_requires_a_launcher():
    with pytest.raises(ValueError, match="requires a pinned launcher"):
        runtime = descriptor(argv=(sys.executable, "{profile}", REQUEST_PLACEHOLDER))


def test_placeholder_without_a_value_is_refused(pinned_launcher: Launcher):
    runtime = descriptor()
    launcher = pinned_launcher
    pinned = RuntimeDescriptor(
        runtime_id=runtime.runtime_id,
        adapter=runtime.adapter,
        executable=runtime.executable,
        argv=runtime.argv,
        preflight_argv=runtime.preflight_argv,
        expected_version_token=runtime.expected_version_token,
        launcher=launcher,
    )
    with pytest.raises(RuntimePreflightError, match="no value was supplied"):
        pinned.command("/tmp/request.json")


def test_prompt_placeholder_and_stdin_source():
    runtime = descriptor(
        argv=(sys.executable, str(FAKE_AGENT), "success", "{prompt}", REQUEST_PLACEHOLDER),
        stdin_from="prompt",
    )
    command = runtime.command("/tmp/request.json", prompt_path="/tmp/prompt.txt")
    assert "/tmp/prompt.txt" in command
    # A runtime that reads its task from stdin needs no placeholder in argv.
    assert descriptor(stdin_from="prompt").stdin_from == "prompt"
    with pytest.raises(ValueError, match="stdin_from"):
        descriptor(stdin_from="file")


def test_env_allowlist_declares_names_only():
    runtime = descriptor(env_allowlist=("OPENAI_API_KEY", "TACTUS_MODEL"))
    assert runtime.env_allowlist == ("OPENAI_API_KEY", "TACTUS_MODEL")
    with pytest.raises(ValueError, match="identifiers"):
        descriptor(env_allowlist=("OPENAI_API_KEY=sk-value",))
