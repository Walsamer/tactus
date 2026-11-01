"""The operator canary command: refusals, staging verification, real denials.

These tests never make a model call. They exercise the parts of
``scripts/run_agent_canary.py`` that must hold *before* a real run can happen —
the approvals, the credential preflight, the staging digest, the fixture
repository and the denial matrix — with a throwaway pinned runtime and
``--dry-run``.

The staging, fixture and denial-matrix tests run through ``sandbox-exec``, so
they are marked ``seatbelt`` and run only where that boundary exists (on Linux
they skip with a reason; on macOS a missing launcher fails them). The refusal and
digest tests need no launcher to execute and always run: their pin names a
stand-in binary pinned by its real digest.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tactus.runtime.containment import digest_tree

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts" / "run_agent_canary.py"
SANDBOX = shutil.which("sandbox-exec")

#: A stand-in for the pinned runtime: version, model list, and (never reached in
#: these tests) a successful JSONL turn. ``/bin/sh`` keeps it readable inside the
#: containment profile without an interpreter on the sandbox ``PATH``.
FAKE_RUNTIME = """\
#!/bin/sh
case "$1" in
  --version) echo "fake-canary 1.0.0"; exit 0 ;;
  debug) echo '{"models":[{"slug":"fake-canary-model"},{"slug":"other-model"}]}'; exit 0 ;;
esac
echo '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}'
exit 0
"""


def _load_command() -> ModuleType:
    """Import the command as a module so tests call it as the operator would."""
    name = "tactus_run_agent_canary"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules while creating slots.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


command = _load_command()


def _launcher(tmp_path: Path) -> Path:
    """The launcher this pin names: the host's seatbelt, or a pinned stand-in.

    Off macOS the launcher is only data here — the staging and refusal tests never
    execute it, and the tests that do carry ``seatbelt`` and never run there.
    """
    if SANDBOX is not None:
        return Path(SANDBOX)
    path = tmp_path / "sandbox-exec-stand-in"
    path.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    path.chmod(0o755)
    return path


def _pin(tmp_path: Path, *, artifact_sha256: str | None = None) -> Path:
    """A complete, self-consistent pin for a throwaway runtime and its denials."""
    runtime = tmp_path / "runtime"
    (runtime / "bin").mkdir(parents=True)
    executable = runtime / "bin" / "fake-canary"
    executable.write_text(FAKE_RUNTIME, encoding="utf-8")
    executable.chmod(0o755)
    (runtime / "notes.txt").write_text("staged runtime fixture\n", encoding="utf-8")
    tree_sha256, file_count = digest_tree(runtime)

    denied = tmp_path / "denied"
    denied.mkdir()
    (denied / "credentials.txt").write_text("must stay unreadable\n", encoding="utf-8")
    (denied / "home").mkdir()

    launcher = _launcher(tmp_path)
    pin = {
        "schema_version": 1,
        "runtime_id": "fake-canary-1.0.0",
        "runtime_kind": "model-provider-canary",
        "adapter": "process.exec@1",
        "purpose": "test pin: no model provider is involved",
        "artifacts": [
            {
                "path": str(runtime),
                "kind": "tree",
                "role": "runtime",
                "digest_order": "path-sorted",
                "file_count": file_count,
                "sha256": artifact_sha256 or tree_sha256,
                "entrypoint": "bin/fake-canary",
            }
        ],
        "launcher": {
            "path": str(launcher),
            "argv_prefix": [str(launcher), "-f", "{profile}"],
            "sha256": hashlib.sha256(launcher.read_bytes()).hexdigest()
            if launcher.is_file()
            else "",
        },
        "executable": str(executable),
        "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "argv": ["{executable}", "exec", "--json", "--model", "{model}", "{request}"],
        "preflight_argv": ["{executable}", "--version"],
        "expected_version_token": "fake-canary 1.0.0",
        "model": {
            "slug": "fake-canary-model",
            "allowed": ["fake-canary-model"],
            "probe_argv": ["{executable}", "debug", "models"],
            "probe_timeout_seconds": 30.0,
        },
        "env_allowlist": ["OPENAI_API_KEY"],
        "credential_env": "OPENAI_API_KEY",
        "stdin_from": "prompt",
        "allowed_scope": ["src", "tests"],
        "containment": {
            "egress": {"mode": "open"},
            "allowances": ["ipc-posix-shm"],
            # Denials are self-contained: the operator's real home is denied by
            # the shipped pin, but a test that included it would fail wherever
            # pytest's temporary directory happens to live inside it.
            "denied_roots": [
                {"path": str(denied), "reason": "a credential store beside the run"},
                {"path": str(denied / "home"), "reason": "a home-shaped directory"},
            ],
        },
        "limits": {
            "timeout_seconds": 120.0,
            "max_output_bytes": 65536,
            "max_patch_bytes": 200000,
            "max_result_bytes": 65536,
            "max_changed_paths": 20,
            "termination_grace_seconds": 5.0,
        },
    }
    path = tmp_path / "pin.json"
    path.write_text(json.dumps(pin, indent=2), encoding="utf-8")
    return path


def _arguments(tmp_path: Path, pin: Path, *extra: str) -> list[str]:
    return [
        "--pin",
        str(pin),
        "--runtime-root",
        str(tmp_path / "stage"),
        "--workspace-root",
        str(tmp_path / "workspaces"),
        "--evidence",
        str(tmp_path / "evidence.json"),
        *extra,
    ]


# -- approvals -------------------------------------------------------------


def test_refuses_without_approve_and_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = command.main(
        [
            "--operator",
            "tester",
            "--runtime-root",
            str(tmp_path / "stage"),
            "--workspace-root",
            str(tmp_path / "workspaces"),
            "--evidence",
            str(tmp_path / "evidence.json"),
        ]
    )
    captured = capsys.readouterr()
    assert code == command.EXIT_REFUSED
    assert "approved by a human" in captured.err
    assert "--approve" in captured.err
    assert list(tmp_path.iterdir()) == []


def test_refuses_without_operator(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = command.main(
        [
            "--approve",
            "--runtime-root",
            str(tmp_path / "stage"),
            "--workspace-root",
            str(tmp_path / "workspaces"),
        ]
    )
    captured = capsys.readouterr()
    assert code == command.EXIT_REFUSED
    assert "--operator" in captured.err
    assert list(tmp_path.iterdir()) == []


# -- the credential --------------------------------------------------------


def test_missing_key_is_refused_before_anything_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    pin = _pin(tmp_path)

    code = command.main(
        ["--approve", "--operator", "tester", *_arguments(tmp_path, pin)]
    )

    captured = capsys.readouterr()
    assert code == command.EXIT_REFUSED
    assert "OPENAI_API_KEY" in captured.err
    # Both satisfaction routes, named, so the operator knows what to do.
    assert "scoped and low-limit" in captured.err
    assert "platform API key" in captured.err
    assert "subscription" in captured.err and "quota" in captured.err
    # No staging, no workspace, no evidence: a refused run leaves nothing behind.
    assert not (tmp_path / "stage").exists()
    assert not (tmp_path / "workspaces").exists()
    assert not (tmp_path / "evidence.json").exists()


# -- staging ---------------------------------------------------------------


def test_staging_refuses_a_digest_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pin = _pin(tmp_path, artifact_sha256="0" * 64)

    code = command.main(
        ["--dry-run", "--approve", "--operator", "tester", *_arguments(tmp_path, pin)]
    )

    captured = capsys.readouterr()
    assert code == command.EXIT_FAILED
    assert "digest mismatch" in captured.err
    # The mismatch is caught after the copy and before any workspace exists.
    assert list((tmp_path / "stage").iterdir())
    assert not (tmp_path / "workspaces").exists()
    assert not (tmp_path / "evidence.json").exists()


# -- the fixture and the denial matrix -------------------------------------


@pytest.mark.seatbelt
def test_fixture_repository_initialises_and_the_denial_matrix_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    pin = _pin(tmp_path)
    evidence_path = tmp_path / "evidence.json"

    code = command.main(
        ["--dry-run", "--approve", "--operator", "tester", *_arguments(tmp_path, pin)]
    )

    assert code == 0
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

    # 1. The fixture is a real one-commit repository with a reachable remote.
    workspace = evidence["workspace"]
    repository = Path(workspace["root"])
    remote = Path(workspace["remote"])
    assert workspace["commits"] == 1
    assert set(workspace["fixture_files"]) == {
        "README.md",
        "src/calc.py",
        "tests/__init__.py",
        "tests/test_calc.py",
    }
    assert (repository / "src" / "calc.py").is_file()
    assert (remote / "HEAD").is_file()
    assert any("refs/heads/main" in ref for ref in workspace["remote_refs"])
    commits = subprocess.run(
        ["git", "-C", str(repository), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert commits.stdout.strip() == "1"

    # 2. The matrix ran through the *staged* launcher and the pinned runtime.
    matrix = evidence["containment"]["denial_matrix"]
    checks = {check["name"]: check for check in matrix["checks"]}
    assert matrix["ok"] is True
    assert checks["read_denied_root"]["ok"] is True
    # The probe target is the pin's own credential store, not a happy accident.
    assert str(tmp_path / "denied") in checks["read_denied_root"]["detail"]
    assert checks["write_denied_outside"]["ok"] is True
    assert checks["connect_open"]["ok"] is True
    assert checks["runtime_probe_through_launcher"]["ok"] is True
    assert checks["runtime_probe_through_launcher"]["detail"].strip() == "fake-canary 1.0.0"

    # 3. The run keeps its state to itself: the staged runtime, the workspace, the
    #    child's HOME and the evidence all live under this test's directory, the
    #    child gets an attempt-local HOME (never the operator's), and none of them
    #    sits inside a configured denied root.
    staged = Path(evidence["staging"]["executable"]).resolve()
    assert staged.is_relative_to(tmp_path / "stage")
    denied_roots = [Path(root["path"]) for root in evidence["pin"]["denied_roots"]]
    for run_path in (
        Path(evidence["workspace"]["root"]),
        Path(matrix["attempt_dir"]),
        Path(matrix["attempt_home"]),
        evidence_path,
    ):
        resolved = run_path.resolve()
        assert resolved.is_relative_to(tmp_path)
        for root in denied_roots:
            assert not resolved.is_relative_to(root.resolve()), (resolved, root)
    assert Path(matrix["attempt_home"]).is_relative_to(tmp_path)
    assert Path(matrix["attempt_home"]).resolve() != Path.home().resolve()
    assert denied_roots, "the pin must deny something for the matrix to be meaningful"


@pytest.mark.seatbelt
def test_dry_run_skips_the_model_call_and_records_the_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    pin = _pin(tmp_path)
    evidence_path = tmp_path / "evidence.json"

    code = command.main(
        [
            "--dry-run",
            "--approve",
            "--operator",
            "tester",
            *_arguments(tmp_path, pin),
        ]
    )

    assert code == 0
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["dry_run"] is True
    assert evidence["model_call"] == "skipped (dry run)"
    assert evidence["operator"] == "tester"
    assert evidence["approved"] is True
    assert evidence["verdict"]["ok"] is True
    assert evidence["exit_code"] == 0

    names = {check["name"] for check in evidence["containment"]["denial_matrix"]["checks"]}
    assert {
        "write_denied_outside",
        "read_denied_root",
        "connect_open",
        "runtime_probe_through_launcher",
    } <= names

    # A rehearsal: no attempt, no usage, no patch, and no credential used.
    assert evidence["attempt"] is None
    assert evidence["usage"] is None
    assert evidence["patch"] is None
    assert evidence["containment_checks"] is None
    assert evidence["credentials"] == {
        "variable": "OPENAI_API_KEY",
        "present": False,
        "used": False,
        "in_argv": False,
        "in_evidence": False,
    }
    assert evidence["pin"]["egress"]["mode"] == "open"
    assert evidence["staging"]["artifacts"][0]["sha256"] == evidence["pin"]["artifacts"][0]["sha256"]


def test_the_survivor_sweep_accepts_a_workspace_root(tmp_path):
    """Regression: the real-run path once passed the workspace object, not its path."""
    module = _load_command()
    root = tmp_path / "repo"
    attempt_dir = tmp_path / "state" / "attempts" / "a1"
    for directory in (root, attempt_dir):
        directory.mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir(exist_ok=True)

    result = module._survivor_sweep("marker-not-present", root, attempt_dir)

    assert isinstance(result, dict)
    assert result["verified"] is True
    assert result["clear"] is True
    assert result["checks"]["workspace_holders"] in (True, False)


@pytest.mark.macos_process_table
def test_the_survivor_sweep_finds_a_helper_holding_the_workspace(tmp_path):
    """A leftover helper whose environment is invisible is found as a holder."""
    import subprocess

    module = _load_command()
    root = tmp_path / "repo"
    attempt_dir = tmp_path / "state" / "attempts" / "a1"
    for directory in (root, attempt_dir):
        directory.mkdir(parents=True, exist_ok=True)
    helper = subprocess.Popen(["/bin/sh", "-c", "sleep 20"], cwd=str(root))
    try:
        result = module._survivor_sweep("marker-not-present", root, attempt_dir)
        assert result["verified"] is True
        assert result["clear"] is False
        assert any(item["matched_by"] == "workspace-holder" for item in result["processes"])
    finally:
        helper.kill()
        helper.wait(timeout=10)


@pytest.mark.seatbelt
def test_a_workspace_inside_a_denied_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boundary is the pin's own list, not a hard-coded $HOME."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    pin = _pin(tmp_path)
    inside_denied = tmp_path / "denied" / "workspace"
    code = command.main(
        [
            "--dry-run",
            "--approve",
            "--operator",
            "tester",
            *_arguments(tmp_path, pin),
            "--workspace-root",
            str(inside_denied),
        ]
    )
    assert code != 0


@pytest.mark.seatbelt
def test_a_staging_root_inside_a_denied_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    pin = _pin(tmp_path)
    inside_denied = tmp_path / "denied" / "runtime-root"
    code = command.main(
        [
            "--dry-run",
            "--approve",
            "--operator",
            "tester",
            *_arguments(tmp_path, pin),
            "--runtime-root",
            str(inside_denied),
        ]
    )
    assert code != 0
