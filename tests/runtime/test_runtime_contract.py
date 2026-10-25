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
