"""The seam backend's own contract: one supervised attempt, reported as evidence.

These tests run the real supervisor against the repository's deterministic fake
agent, so they prove behaviour rather than shape. Conformance with the *Ictus*
contract lives next door in
``tests/integrations/ictus/test_runtime_seam_conformance.py`` and is asserted
against the pinned artifact.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_PATH = REPO_ROOT / "integrations" / "dagster" / "agent_backend.py"
SHIPPED_PIN = REPO_ROOT / "config" / "agent-runtime.json"
WORKSPACE_ENV = "TACTUS_AGENT_WORKSPACE_ROOT"
STATE_ENV = "TACTUS_AGENT_STATE_DIR"
ORIGINAL = 'def hello():\n    return "hello"\n'


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("tactus_agent_backend", BACKEND_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


backend_module = _load()


class _WorkspaceRef:
    def __init__(self, descriptor_path: str) -> None:
        self.descriptor_path = descriptor_path


class _Request:
    """The fields of Ictus's ``AttemptRequest`` this backend actually reads."""

    def __init__(self, *, descriptor_path: str, write_scope=("src",), wall=120.0, steps=200):
        self.capability = "software.implement"
        self.write_scope = tuple(write_scope)
        self.wall_clock_seconds = wall
        self.step_budget = steps
        self.workspace = _WorkspaceRef(descriptor_path)


class _Control:
    """Ictus's ``ExecutionControl`` as this backend is allowed to use it.

    ``spawn`` raises: the backend must never route the child through the control,
    because the supervisor owns that process group and its termination account.
    """

    def __init__(self, *, cancelled: bool = False) -> None:
        self.cancelled = cancelled
        self.steps = 0
        self.spawn_calls = 0

    def step(self) -> int:
        self.steps += 1
        return self.steps

    def spawn(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - must not run
        self.spawn_calls += 1
        raise AssertionError("the backend must not spawn through the seam control")

    def finish(self) -> None:
        return None


def _shipped() -> dict:
    return json.loads(SHIPPED_PIN.read_text(encoding="utf-8"))


def _pin(tmp_path: Path) -> Path:
    path = tmp_path / "agent-runtime.json"
    path.write_text(json.dumps(_shipped()), encoding="utf-8")
    return path


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src" / "example.py").write_text(ORIGINAL, encoding="utf-8")
    return root


@pytest.fixture()
def prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A configured backend, a pinned workspace and the operator environment."""

    workspace = _workspace(tmp_path)
    monkeypatch.setenv(WORKSPACE_ENV, str(workspace))
    monkeypatch.setenv(STATE_ENV, str(tmp_path / "state"))
    return {
        "backend": backend_module.create_backend(),
        "pin": _pin(tmp_path),
        "request": _Request(descriptor_path=str(_pin(tmp_path))),
    }


def test_a_real_attempt_succeeds_without_the_control_spawning_anything(
    prepared: dict[str, Any]
) -> None:
    """The whole point of decision 7a, asserted rather than claimed.

    The supervisor owns the child's process group and confirms its termination.
    Routing that spawn through ``control`` would give one process group two
    supervisors, so the control's ``spawn`` is never called — and the attempt
    still runs.
    """

    control = _Control()
    evidence = prepared["backend"].execute(prepared["request"], control)
    assert control.spawn_calls == 0
    assert control.steps == 1, "one supervised process is one semantic step"
    assert evidence["outcome"] == "succeeded", evidence["tactus"]["errors"]
    assert evidence["failure_kind"] is None
    assert evidence["steps_used"] == 1
    assert evidence["changed_paths"], "the fake agent edits src/example.py"
    assert evidence["patch_digest"].startswith("sha256:")
    assert evidence["patch_bytes"] > 0
    assert evidence["termination"]["confirmed"] is True
    assert evidence["tactus"]["publication_authority"] is False


def test_the_verified_patch_is_still_on_disk_and_re_checked(
    prepared: dict[str, Any]
) -> None:
    """``verify`` is the runtime's own post-condition, not a restatement."""

    evidence = prepared["backend"].execute(prepared["request"], _Control())
    verdict = prepared["backend"].verify(evidence)
    assert verdict["verified"] is True, verdict["detail"]

    # The same evidence with a digest that no longer matches what is on disk.
    tampered = {**evidence, "patch_digest": f"sha256:{'0' * 64}"}
    verdict = prepared["backend"].verify(tampered)
    assert verdict["verified"] is False
    assert "does not match" in verdict["detail"]


def test_an_already_cancelled_attempt_starts_nothing(prepared: dict[str, Any]) -> None:
    """Cancellation reaches us before a child exists; nothing may be spawned."""

    control = _Control(cancelled=True)
    evidence = prepared["backend"].execute(prepared["request"], control)
    assert control.spawn_calls == 0
    assert control.steps == 0
    assert evidence["outcome"] == "cancelled"
    assert evidence["failure_kind"] == "CANCEL_REQUESTED"
    assert evidence["steps_used"] == 0
    assert evidence["changed_paths"] == []
    assert evidence["tactus"]["attempt_id"] is None, "no attempt directory was created"


def test_a_missing_workspace_root_is_a_typed_configuration_failure(
    prepared: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ictus needs a kind, not a traceback from deep inside a run."""

    monkeypatch.delenv(WORKSPACE_ENV, raising=False)
    control = _Control()
    evidence = prepared["backend"].execute(prepared["request"], control)
    assert evidence["outcome"] == "failed"
    assert evidence["failure_kind"] == "CONFIGURATION"
    assert control.spawn_calls == 0
    assert any(WORKSPACE_ENV in problem for problem in evidence["tactus"]["errors"])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("relative/workspace", "absolute path"),
        ("/nonexistent/workspace/root", "is not a directory"),
    ],
)
def test_a_set_but_unusable_workspace_root_is_configuration_not_a_backend_crash(
    prepared: dict[str, Any], monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    """An operator mistake must not surface as PROCESS_CRASH, which is what
    ``WorkspaceScopeError`` escaping ``execute`` would produce."""

    monkeypatch.setenv(WORKSPACE_ENV, value)
    control = _Control()
    evidence = prepared["backend"].execute(prepared["request"], control)
    assert evidence["failure_kind"] == "CONFIGURATION"
    assert control.spawn_calls == 0
    assert any(expected in problem for problem in evidence["tactus"]["errors"])


@pytest.mark.parametrize(
    ("write_scope", "expected"),
    [
        (("/etc",), "is absolute"),
        (("",), "names no path"),
    ],
)
def test_a_write_scope_this_backend_cannot_express_is_refused(
    prepared: dict[str, Any], write_scope: tuple[str, ...], expected: str
) -> None:
    """Ictus permits absolute scope entries; this backend has a workspace root.

    Left to the supervisision layer it would escape as a ``WorkspaceScopeError``
    and Ictus would record BACKEND_ERROR — an operator-shaped mistake reported as
    a process incident.
    """

    request = prepared["request"]
    request.write_scope = write_scope
    control = _Control()
    evidence = prepared["backend"].execute(request, control)
    assert evidence["failure_kind"] == "CONFIGURATION"
    assert control.spawn_calls == 0
    assert control.steps == 0
    assert any(expected in problem for problem in evidence["tactus"]["errors"])


def test_a_workspace_inside_the_repository_is_refused(
    prepared: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An allowed path in the checkout the code lives in is not a bounded workspace."""

    monkeypatch.setenv(WORKSPACE_ENV, str(REPO_ROOT / "src"))
    evidence = prepared["backend"].execute(prepared["request"], _Control())
    assert evidence["failure_kind"] == "CONFIGURATION"
    assert any("inside this repository" in p for p in evidence["tactus"]["errors"])


@pytest.mark.parametrize("capability", ["software.promote", "demo.verify"])
def test_a_capability_this_backend_cannot_honour_is_refused(
    prepared: dict[str, Any], capability: str
) -> None:
    """The seam names one backend for both software routes.

    The only backend Tactus has edits a workspace. Pointing promotion at it would
    run the editor, return ``succeeded`` and record a successful promotion with
    nothing published, so it has to refuse rather than reuse the label.
    """

    request = prepared["request"]
    request.capability = capability
    control = _Control()
    evidence = prepared["backend"].execute(request, control)
    assert evidence["outcome"] == "failed"
    assert evidence["failure_kind"] == "CONFIGURATION"
    assert control.spawn_calls == 0
    assert control.steps == 0


def test_a_rejected_pin_is_reported_as_a_failed_probe(prepared: dict[str, Any]) -> None:
    """``describe_workspace`` never raises: Ictus turns ``ok: false`` into a fact."""

    broken = Path(prepared["pin"]).parent / "broken.json"
    broken.write_text("{}", encoding="utf-8")
    report = prepared["backend"].describe_workspace(_WorkspaceRef(str(broken)))
    assert report["probe"]["ok"] is False
    assert report["probe"]["detail"]
