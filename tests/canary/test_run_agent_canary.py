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
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tactus.runtime.containment import (
    ContainmentPolicy,
    DeniedRoot,
    EgressPolicy,
    Launcher,
    digest_tree,
)

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts" / "run_agent_canary.py"
SHIPPED_PIN = REPOSITORY / "config" / "agent-runtime-canary.json"
SANDBOX = shutil.which("sandbox-exec")

#: The credential the pinned Codex CLI authenticates ``codex exec`` from. It is
#: *not* ``OPENAI_API_KEY``: that name was right for an older CLI and this build
#: ignores it, sends no bearer and fails every attempt with ``Missing bearer``.
_CREDENTIAL = "CODEX_API_KEY"

#: A stand-in for the pinned runtime: version, model list, a report of whether the
#: session file was staged into its own home, and a successful JSONL turn.
#: ``/bin/sh`` keeps it readable inside the containment profile without an
#: interpreter on the sandbox ``PATH``.
FAKE_RUNTIME = """\
#!/bin/sh
case "$1" in
  --version) echo "fake-canary 1.0.0"; exit 0 ;;
  debug) echo '{"models":[{"slug":"fake-canary-model"},{"slug":"other-model"}]}'; exit 0 ;;
esac
if [ -f "$HOME/.codex/auth.json" ]; then
  echo '{"type":"staged","found":true}'
else
  echo '{"type":"staged","found":false}'
fi
echo '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}'
exit 0
"""

#: What a session credential file looks like to this command: JSON with tokens, one
#: of them refresh material. The value is distinctive so a leak is unmistakable.
SESSION_TOKEN = "sk-test-session-value-that-must-not-leak-0123456789"
SESSION_FILE = {
    "OPENAI_API_KEY": None,
    "tokens": {
        "access_token": "access-value-that-must-not-leak",
        "refresh_token": SESSION_TOKEN,
        "id_token": "id-value-that-must-not-leak",
    },
    "last_refresh": "2026-11-02T10:00:00Z",
}


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


def _pin(
    tmp_path: Path,
    *,
    artifact_sha256: str | None = None,
    credential_file: Path | None = None,
    env_allowlist: list[str] | None = None,
    keychain_store: bool = False,
) -> Path:
    """A complete, self-consistent pin for a throwaway runtime and its denials.

    The default credential route is the environment one; ``credential_file`` builds
    the session-file route instead, and ``keychain_store`` adds a denied root shaped
    like the operator's ``Library/Keychains`` with a store file in it.
    """
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
    denied_roots = [
        {"path": str(denied), "reason": "a credential store beside the run"},
        {"path": str(denied / "home"), "reason": "a home-shaped directory"},
    ]
    if keychain_store:
        keychains = tmp_path / "Library" / "Keychains"
        keychains.mkdir(parents=True)
        (keychains / "login.keychain-db").write_text("a store\n", encoding="utf-8")
        denied_roots.append({"path": str(keychains), "reason": "the keychain stores"})

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
            "recorded_on": "macOS 0.0 (TESTBUILD)",
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
        "env_allowlist": [_CREDENTIAL],
        "credential_env": _CREDENTIAL,
        "stdin_from": "prompt",
        "allowed_scope": ["src", "tests"],
        "containment": {
            "egress": {"mode": "open"},
            "allowances": ["ipc-posix-shm"],
            # Denials are self-contained: the operator's real home is denied by
            # the shipped pin, but a test that included it would fail wherever
            # pytest's temporary directory happens to live inside it.
            "denied_roots": denied_roots,
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
    if credential_file is not None:
        # The session-file route: no credential variable reaches the child at all,
        # and the file is staged into the attempt's own home.
        pin.pop("credential_env")
        pin["env_allowlist"] = []
        pin["credential_file"] = {
            "kind": "codex-session",
            "path": str(credential_file),
            "home_path": ".codex/auth.json",
            "mode": "0600",
        }
    if env_allowlist is not None:
        pin["env_allowlist"] = list(env_allowlist)
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
    monkeypatch.delenv(_CREDENTIAL, raising=False)
    pin = _pin(tmp_path)

    code = command.main(
        ["--approve", "--operator", "tester", *_arguments(tmp_path, pin)]
    )

    captured = capsys.readouterr()
    assert code == command.EXIT_REFUSED
    assert _CREDENTIAL in captured.err
    # Both satisfaction routes, named, so the operator knows what to do.
    assert "scoped and low-limit" in captured.err
    assert "platform API key" in captured.err
    assert "subscription" in captured.err and "quota" in captured.err
    # No staging, no workspace, no evidence: a refused run leaves nothing behind.
    assert not (tmp_path / "stage").exists()
    assert not (tmp_path / "workspaces").exists()
    assert not (tmp_path / "evidence.json").exists()


def test_the_missing_credential_message_follows_the_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal names the pin's variable, so a rename cannot leave a stale message."""
    variable = "TACTUS_TEST_CREDENTIAL"
    monkeypatch.delenv(variable, raising=False)
    pin = _pin(tmp_path)
    document = json.loads(pin.read_text(encoding="utf-8"))
    document["env_allowlist"] = [variable]
    document["credential_env"] = variable
    pin.write_text(json.dumps(document, indent=2), encoding="utf-8")

    code = command.main(
        ["--approve", "--operator", "tester", *_arguments(tmp_path, pin)]
    )

    captured = capsys.readouterr()
    assert code == command.EXIT_REFUSED
    assert f"{variable} is not set" in captured.err
    assert not (tmp_path / "stage").exists()


def _session_file(tmp_path: Path, payload: object | None = None) -> Path:
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(SESSION_FILE if payload is None else payload), encoding="utf-8")
    return path


def test_a_pin_with_both_credential_routes_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two routes are a configuration failure, not a precedence rule."""
    pin = _pin(tmp_path, credential_file=_session_file(tmp_path))
    document = json.loads(pin.read_text(encoding="utf-8"))
    document["credential_env"] = _CREDENTIAL
    pin.write_text(json.dumps(document, indent=2), encoding="utf-8")

    code = command.main(
        ["--dry-run", "--approve", "--operator", "tester", *_arguments(tmp_path, pin)]
    )

    captured = capsys.readouterr()
    assert code == command.EXIT_REFUSED
    assert "exactly one route" in captured.err
    assert not (tmp_path / "stage").exists()


def test_the_file_route_refuses_a_credential_variable_in_the_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pin = _pin(
        tmp_path,
        credential_file=_session_file(tmp_path),
        env_allowlist=[_CREDENTIAL],
    )

    code = command.main(
        ["--dry-run", "--approve", "--operator", "tester", *_arguments(tmp_path, pin)]
    )

    captured = capsys.readouterr()
    assert code == command.EXIT_REFUSED
    assert "env_allowlist must be empty" in captured.err
    assert not (tmp_path / "stage").exists()


def test_a_missing_session_file_is_refused_before_anything_is_created(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    absent = tmp_path / "not-there" / "auth.json"
    pin = _pin(tmp_path, credential_file=absent)

    code = command.main(
        ["--approve", "--operator", "tester", *_arguments(tmp_path, pin)]
    )

    captured = capsys.readouterr()
    assert code == command.EXIT_REFUSED
    assert str(absent) in captured.err
    assert "missing or unreadable" in captured.err
    # Both routes are named, so the operator knows what to do.
    assert "codex login" in captured.err and "credential_env" in captured.err
    assert not (tmp_path / "stage").exists()
    assert not (tmp_path / "workspaces").exists()
    assert not (tmp_path / "evidence.json").exists()


def test_a_session_file_without_tokens_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _session_file(tmp_path, {"tokens": {}, "last_refresh": "2026-11-02T10:00:00Z"})
    pin = _pin(tmp_path, credential_file=source)

    code = command.main(
        ["--approve", "--operator", "tester", *_arguments(tmp_path, pin)]
    )

    captured = capsys.readouterr()
    assert code == command.EXIT_REFUSED
    assert "carries no tokens" in captured.err and str(source) in captured.err
    assert not (tmp_path / "stage").exists()


def test_the_shipped_pin_takes_the_operator_session_file() -> None:
    """The shipped route, and the alternative, are both measured statements.

    The session file is what the operator uses (`codex login` writes it), so the
    pin stages it instead of asking for a platform key; the environment route
    stays documented as the alternative for someone who has a scoped API key, and
    with a file route no credential variable may reach the child at all.
    """
    pin = json.loads(SHIPPED_PIN.read_text(encoding="utf-8"))
    assert "credential_env" not in pin
    assert pin["env_allowlist"] == []
    assert pin["credential_file"] == {
        "kind": "codex-session",
        "path": "~/.codex/auth.json",
        "home_path": ".codex/auth.json",
        "mode": "0600",
    }
    # The name the CLI actually reads is documented, so the alternative stays correct.
    source = (REPOSITORY / "scripts" / "run_agent_canary.py").read_text(encoding="utf-8")
    assert "CODEX_API_KEY" in source
    assert "OPENAI_API_KEY" in source


def test_the_shipped_pin_records_the_mach_services_the_runtime_needs(
    tmp_path: Path,
) -> None:
    """Exactly the three named mach services, and no blanket mach grant.

    Measured: under the shipped allowances the pinned CLI reaches the model call;
    with `ipc-posix-shm` alone it fails to load its configuration. The pin is the
    record of that measurement, so it is asserted there rather than inferred.
    """
    shipped = json.loads(SHIPPED_PIN.read_text(encoding="utf-8"))
    assert shipped["containment"]["allowances"] == [
        "ipc-posix-shm",
        'mach-lookup (global-name "com.apple.cfprefsd.daemon")',
        'mach-lookup (global-name "com.apple.cfprefsd.agent")',
        'mach-lookup (global-name "com.apple.SecurityServer")',
    ]

    policy = ContainmentPolicy(
        launcher=Launcher.for_path(_launcher(tmp_path)),
        denied_roots=(DeniedRoot(path=tmp_path / "denied", reason="test"),),
        egress=EgressPolicy(mode="open"),
        allowances=tuple(shipped["containment"]["allowances"]),
    )
    workspace = tmp_path / "ws"
    attempt = tmp_path / "attempt"
    workspace.mkdir()
    attempt.mkdir()
    text = policy.profile_text(workspace=workspace, attempt_dir=attempt)
    for name in (
        "com.apple.cfprefsd.daemon",
        "com.apple.cfprefsd.agent",
        "com.apple.SecurityServer",
    ):
        assert f'(allow mach-lookup (global-name "{name}"))' in text
    # The blanket grant is exactly what must not land.
    assert "(allow mach-lookup)" not in text
    assert "(allow mach*" not in text


def test_the_shipped_pin_denies_the_operator_keychain_and_codex_home() -> None:
    pin = json.loads(SHIPPED_PIN.read_text(encoding="utf-8"))
    denied = {root["path"] for root in pin["containment"]["denied_roots"]}
    assert {"~", "~/.ssh", "~/.codex", "~/Library/Keychains"} <= denied


def test_the_shipped_pin_records_the_host_its_launcher_digest_belongs_to() -> None:
    """The digest is not a universal value: it belongs to one OS build.

    A macOS update replaces `/usr/bin/sandbox-exec`, so the pin names the host the
    digest was recorded on and a preflight mismatch can name both sides. Without
    that, the message is two bare digests and the next operator has to guess.
    """
    shipped = json.loads(SHIPPED_PIN.read_text(encoding="utf-8"))
    launcher = shipped["launcher"]
    assert re.fullmatch(r"macOS \d+\.\d+ \([0-9A-Za-z]+\)", launcher["recorded_on"]), launcher
    assert len(launcher["sha256"]) == 64


def test_a_launcher_pinned_by_digest_must_record_its_host(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refused, not filled in: a digest without its host is the rubber stamp."""
    pin = _pin(tmp_path)
    config = json.loads(pin.read_text(encoding="utf-8"))
    config["launcher"].pop("recorded_on")
    pin.write_text(json.dumps(config), encoding="utf-8")

    code = command.main(
        ["--dry-run", "--approve", "--operator", "tester", *_arguments(tmp_path, pin)]
    )

    captured = capsys.readouterr()
    assert code == command.EXIT_REFUSED
    assert "does not record the host" in captured.err
    assert not (tmp_path / "stage").exists()


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
    monkeypatch.delenv(_CREDENTIAL, raising=False)
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
    monkeypatch.delenv(_CREDENTIAL, raising=False)
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
        "route": "env",
        "variable": _CREDENTIAL,
        "present": False,
        "used": False,
        "in_argv": False,
        "in_evidence": False,
    }
    assert evidence["pin"]["egress"]["mode"] == "open"
    assert evidence["staging"]["artifacts"][0]["sha256"] == evidence["pin"]["artifacts"][0]["sha256"]


@pytest.mark.seatbelt
def test_the_denial_matrix_covers_the_keychain_store_with_securityd_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The matrix holds with securityd allowed, and probes the keychain store.

    The allowances are a permission of their own; the store file is what still
    closes the operator's keychain, so the matrix proves that file read is refused
    even in a profile that may talk to `securityd`.
    """
    monkeypatch.delenv(_CREDENTIAL, raising=False)
    pin = _pin(tmp_path, keychain_store=True)
    document = json.loads(pin.read_text(encoding="utf-8"))
    document["containment"]["allowances"] = [
        "ipc-posix-shm",
        'mach-lookup (global-name "com.apple.SecurityServer")',
    ]
    pin.write_text(json.dumps(document, indent=2), encoding="utf-8")
    evidence_path = tmp_path / "evidence.json"

    code = command.main(
        ["--dry-run", "--approve", "--operator", "tester", *_arguments(tmp_path, pin)]
    )

    assert code == 0
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    checks = {
        check["name"]: check for check in evidence["containment"]["denial_matrix"]["checks"]
    }
    store = checks["read_denied_keychain_store"]
    assert store["ok"] is True and store["required"] is True
    assert str(tmp_path / "Library" / "Keychains" / "login.keychain-db") in store["detail"]
    assert evidence["pin"]["allowances"] == document["containment"]["allowances"]
    assert checks["read_denied_root"]["ok"] is True


@pytest.mark.seatbelt
def test_a_real_run_stages_the_session_file_into_the_attempt_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The file route end to end, without a model call.

    The throwaway runtime reports what it could see in its own ``$HOME``, so the
    staging is observed from inside the boundary. The attempt produces no patch, so
    the canary's verdict fails — that is not what this test is about; the credential
    account and its removal are.
    """
    monkeypatch.delenv(_CREDENTIAL, raising=False)
    source = _session_file(tmp_path)
    pin = _pin(tmp_path, credential_file=source)
    evidence_path = tmp_path / "evidence.json"

    command.main(["--approve", "--operator", "tester", *_arguments(tmp_path, pin)])

    captured = capsys.readouterr()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    credentials = evidence["credentials"]
    assert credentials["route"] == "file"
    assert credentials["kind"] == "codex-session"
    assert credentials["source"] == str(source)
    assert credentials["staged"] is True
    assert credentials["staged_mode"] == "0600"
    assert credentials["staged_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert credentials["carries_refresh_material"] is True
    assert credentials["removed"] is True
    assert credentials["in_argv"] is False and credentials["in_evidence"] is False

    # The staged copy is gone; the attempt directory itself is retained evidence.
    staged = Path(credentials["staged_path"])
    assert staged.is_relative_to(Path(evidence["attempt"]["attempt_dir"]))
    assert not staged.exists()

    # The cause of the failure is classified beside the verdict, and a run with no
    # rejected credential says so rather than guessing.
    rejection = evidence["attempt"]["auth_rejection"]
    assert rejection["detected"] is False
    assert rejection["verdict_unchanged"] is True

    # The child read it: its own report says the file was there.
    supervisor = json.loads(
        Path(evidence["attempt"]["supervisor_evidence"]).read_text(encoding="utf-8")
    )
    assert '"found":true' in supervisor["output"]["stdout"].replace(" ", "")

    # No credential content anywhere it could be read back: evidence or stdout.
    for value in SESSION_FILE["tokens"].values():
        assert value not in evidence_path.read_text(encoding="utf-8")
        assert value not in captured.out and value not in captured.err
        for path in Path(evidence["attempt"]["attempt_dir"]).rglob("*"):
            if path.is_file():
                assert value.encode() not in path.read_bytes(), path


def test_an_auth_rejection_is_named_beside_the_verdict_not_instead_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected credential is classified; the verdict it produced is untouched.

    The pinned CLI reconnects a refused connection until the attempt deadline, so the
    supervisor reports `timeout`. Shortening the deadline to make that read nicer
    would trade a real bound for a prettier report; naming the cause beside it costs
    nothing.
    """
    stderr = (
        "Reading additional input from stdin...\n"
        '2026-11-02T10:46:27.835989Z ERROR codex_api::endpoint::responses_websocket: '
        "failed to connect to websocket: HTTP error: 401 Unauthorized, url: "
        "wss://api.openai.com/v1/responses\n"
    )
    clean = command.classify_auth_rejection('{"type":"turn.started"}', "")
    rejected = command.classify_auth_rejection("", stderr)

    assert clean["detected"] is False
    assert rejected["detected"] is True
    assert rejected["marker"] == "401 Unauthorized"
    assert "401 Unauthorized" in rejected["detail"]
    # Classification never rewrites the verdict, in either direction.
    assert rejected["verdict_unchanged"] is True and clean["verdict_unchanged"] is True


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
    monkeypatch.delenv(_CREDENTIAL, raising=False)
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
    monkeypatch.delenv(_CREDENTIAL, raising=False)
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
