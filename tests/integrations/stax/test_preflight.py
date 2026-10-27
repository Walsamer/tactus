"""Deterministic preflight tests using a fake Stax executable and runner.

These tests never invoke a real Stax installation, Git, the network or a
canonical checkout. They exercise the injected command seam instead.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path

import pytest

from tactus.integrations.stax import (
    PINNED_STAX,
    CommandResult,
    StaxCommandExecutionError,
    StaxExecutableDigestMismatchError,
    StaxExecutableNotAbsoluteError,
    StaxExecutableNotFoundError,
    StaxExecutableNotExecutableError,
    StaxExecutableSymlinkError,
    StaxMalformedDoctorOutputError,
    StaxMalformedVersionError,
    StaxPreflight,
    StaxRepositoryNotInitializedError,
    StaxRepositoryNotFoundError,
    StaxUnsupportedPlatformError,
    StaxVersionMismatchError,
    parse_stax_version,
)

SUPPORTED = PINNED_STAX.supported_platform
VERSION_OUTPUT = f"stax {PINNED_STAX.version}\n"
INITIALIZED_OUTPUT = (
    "stax doctor\n\n"
    "\u2713 Repo initialized\n"
    "\u2713 Trunk: main\n"
    "\u2717 Remote missing: No git remote 'origin' found.\n"
)


class FakeRunner:
    """Deterministic command runner keyed by arguments after the executable."""

    def __init__(self, responses: dict[tuple[str, ...], CommandResult]) -> None:
        self.responses = responses
        self.calls: list[tuple[tuple[str, ...], Path | None]] = []

    def __call__(
        self, argv: tuple[str, ...], *, cwd: Path | None = None
    ) -> CommandResult:
        self.calls.append((tuple(argv), cwd))
        key = tuple(argv[1:])
        if key not in self.responses:
            raise AssertionError(f"unexpected fake Stax command: {key!r}")
        return self.responses[key]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def executable(tmp_path: Path) -> Path:
    path = tmp_path / "stax"
    path.write_bytes(b"deterministic fake stax executable\n")
    path.chmod(0o755)
    return path


def _test_pin(executable: Path, **overrides: str):
    values = {"executable_sha256": _digest(executable), **overrides}
    return replace(PINNED_STAX, **values)


def _success_runner() -> FakeRunner:
    return FakeRunner(
        {
            ("--version",): CommandResult(0, VERSION_OUTPUT),
            ("doctor",): CommandResult(0, INITIALIZED_OUTPUT),
        }
    )


def test_successful_preflight_returns_verified_facts(
    executable: Path, tmp_path: Path
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    runner = _success_runner()
    preflight = StaxPreflight(
        _test_pin(executable), runner=runner, platform=SUPPORTED
    )

    result = preflight.check(executable, repository=repository)

    assert result.pin is preflight.pin
    assert result.executable == os.fspath(executable)
    assert result.version == "0.113.1"
    assert result.executable_sha256 == _digest(executable)
    assert result.platform == SUPPORTED
    assert result.repository == os.fspath(repository.resolve())
    assert result.repository_initialized is True

    # Only the two reviewed, read-only commands are invoked, and the doctor
    # command runs in the requested repository without a shell.
    assert [argv for argv, _ in runner.calls] == [
        (os.fspath(executable), "--version"),
        (os.fspath(executable), "doctor"),
    ]
    assert runner.calls[0][1] is None
    assert runner.calls[1][1] == repository.resolve()
    invoked = {argv[1:] for argv, _ in runner.calls}
    assert invoked == {("--version",), ("doctor",)}


def test_preflight_without_repository_does_not_run_doctor(
    executable: Path,
) -> None:
    runner = _success_runner()
    result = StaxPreflight(
        _test_pin(executable), runner=runner, platform=SUPPORTED
    ).preflight(executable)

    assert result.repository is None
    assert result.repository_initialized is None
    assert [argv[1:] for argv, _ in runner.calls] == [("--version",)]


def test_absent_executable_fails_closed(tmp_path: Path) -> None:
    runner = _success_runner()
    preflight = StaxPreflight(PINNED_STAX, runner=runner, platform=SUPPORTED)

    with pytest.raises(StaxExecutableNotFoundError):
        preflight.check(tmp_path / "missing-stax")

    assert runner.calls == []


def test_relative_executable_is_rejected() -> None:
    runner = _success_runner()
    preflight = StaxPreflight(PINNED_STAX, runner=runner, platform=SUPPORTED)

    with pytest.raises(StaxExecutableNotAbsoluteError):
        preflight.check("bin/stax")

    assert runner.calls == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX execute bits required")
def test_non_executable_file_is_rejected(executable: Path) -> None:
    executable.chmod(0o644)
    runner = _success_runner()
    preflight = StaxPreflight(
        _test_pin(executable), runner=runner, platform=SUPPORTED
    )

    with pytest.raises(StaxExecutableNotExecutableError):
        preflight.check(executable)

    assert runner.calls == []


def test_symlink_drift_is_rejected(executable: Path, tmp_path: Path) -> None:
    link = tmp_path / "stax-link"
    link.symlink_to(executable)
    runner = _success_runner()
    preflight = StaxPreflight(
        _test_pin(executable), runner=runner, platform=SUPPORTED
    )

    with pytest.raises(StaxExecutableSymlinkError):
        preflight.check(link)

    assert runner.calls == []


def test_off_pin_digest_is_rejected_before_execution(
    executable: Path,
) -> None:
    runner = _success_runner()
    preflight = StaxPreflight(
        PINNED_STAX, runner=runner, platform=SUPPORTED
    )

    with pytest.raises(StaxExecutableDigestMismatchError) as raised:
        preflight.check(executable)

    assert raised.value.expected == PINNED_STAX.executable_sha256
    assert raised.value.actual == _digest(executable)
    assert runner.calls == []


def test_unsupported_platform_is_rejected_before_execution(
    executable: Path,
) -> None:
    runner = _success_runner()
    preflight = StaxPreflight(
        _test_pin(executable),
        runner=runner,
        platform="x86_64-apple-darwin",
    )

    with pytest.raises(StaxUnsupportedPlatformError):
        preflight.check(executable)

    assert runner.calls == []


def test_wrong_version_is_rejected(executable: Path) -> None:
    runner = FakeRunner(
        {("--version",): CommandResult(0, "stax 0.113.0\n")}
    )
    preflight = StaxPreflight(
        _test_pin(executable), runner=runner, platform=SUPPORTED
    )

    with pytest.raises(StaxVersionMismatchError) as raised:
        preflight.check(executable)

    assert raised.value.expected == "0.113.1"
    assert raised.value.actual == "0.113.0"


@pytest.mark.parametrize(
    "output",
    [
        "",
        "\n\n",
        "stax\n",
        "stax 0.113\n",
        "stax 0.113.1\nunexpected extra line\n",
        "error: could not determine version 0.113.1\n",
        "stax version 0.113.1\n",
    ],
)
def test_malformed_version_output_is_rejected(
    executable: Path, output: str
) -> None:
    runner = FakeRunner({("--version",): CommandResult(0, output)})
    preflight = StaxPreflight(
        _test_pin(executable), runner=runner, platform=SUPPORTED
    )

    with pytest.raises(StaxMalformedVersionError):
        preflight.check(executable)


def test_nonzero_version_command_is_a_typed_command_failure(
    executable: Path,
) -> None:
    runner = FakeRunner(
        {("--version",): CommandResult(2, "", "no version available\n")}
    )
    preflight = StaxPreflight(
        _test_pin(executable), runner=runner, platform=SUPPORTED
    )

    with pytest.raises(StaxCommandExecutionError):
        preflight.check(executable)


@pytest.mark.parametrize(
    "doctor_output",
    [
        "stax doctor\n\n\u2717 Repo not initialized (run `stax` once)\n",
        "stax doctor\n\n\u2717 Not in a Git repository. Run this command from "
        "inside a Git repository.\n",
    ],
)
def test_uninitialized_repository_is_rejected(
    executable: Path, tmp_path: Path, doctor_output: str
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    runner = FakeRunner(
        {
            ("--version",): CommandResult(0, VERSION_OUTPUT),
            ("doctor",): CommandResult(0, doctor_output),
        }
    )
    preflight = StaxPreflight(
        _test_pin(executable), runner=runner, platform=SUPPORTED
    )

    with pytest.raises(StaxRepositoryNotInitializedError):
        preflight.check(executable, repository=repository)


def test_nonzero_doctor_command_is_a_typed_command_failure(
    executable: Path, tmp_path: Path
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    runner = FakeRunner(
        {
            ("--version",): CommandResult(0, VERSION_OUTPUT),
            ("doctor",): CommandResult(1, "", "doctor crashed\n"),
        }
    )
    preflight = StaxPreflight(
        _test_pin(executable), runner=runner, platform=SUPPORTED
    )

    with pytest.raises(StaxCommandExecutionError):
        preflight.check(executable, repository=repository)


def test_unrecognized_doctor_output_fails_closed(
    executable: Path, tmp_path: Path
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    runner = FakeRunner(
        {
            ("--version",): CommandResult(0, VERSION_OUTPUT),
            ("doctor",): CommandResult(0, "stax doctor\nsomething unexpected\n"),
        }
    )
    preflight = StaxPreflight(
        _test_pin(executable), runner=runner, platform=SUPPORTED
    )

    with pytest.raises(StaxMalformedDoctorOutputError):
        preflight.check(executable, repository=repository)


def test_missing_repository_path_is_typed(
    executable: Path, tmp_path: Path
) -> None:
    runner = _success_runner()
    preflight = StaxPreflight(
        _test_pin(executable), runner=runner, platform=SUPPORTED
    )

    with pytest.raises(StaxRepositoryNotFoundError):
        preflight.check(executable, repository=tmp_path / "missing-repository")


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("stax 0.113.1\n", "0.113.1"),
        ("stax v0.113.1\n", "0.113.1"),
        ("stax-cli 0.113.1\n", "0.113.1"),
        ("0.113.1\n", "0.113.1"),
        ("\u001b[32mstax 0.113.1\u001b[0m\n", "0.113.1"),
    ],
)
def test_parse_stax_version_accepts_only_exact_forms(
    output: str, expected: str
) -> None:
    assert parse_stax_version(output) == expected
