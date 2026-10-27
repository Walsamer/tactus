"""Fail-closed, dependency-injected Stax CLI preflight.

This module is an anti-corruption boundary around one external tool. It does
not create workspaces, call a scheduler, touch a Fleet pool, fall back to Git or
repair/mutate a checkout. It only:

1. validates an absolute executable path and rejects symlinks,
2. verifies the exact supported platform,
3. verifies the executable SHA-256 *before* executing it,
4. invokes ``stax --version`` and requires the pinned version, and
5. optionally invokes read-only ``stax doctor`` in a repository to prove Stax
   has been initialized there.

Every failure is typed. Production callers use :data:`PINNED_STAX`; tests inject
a deterministic runner, platform and/or pin so no real Stax installation (and no
network) is required.
"""

from __future__ import annotations

import hashlib
import os
import platform as _platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from .pin import PINNED_STAX, StaxPin

__all__ = [
    "CommandResult",
    "CommandRunner",
    "StaxCommandExecutionError",
    "StaxExecutableDigestMismatchError",
    "StaxExecutableNotAbsoluteError",
    "StaxExecutableNotFoundError",
    "StaxExecutableNotExecutableError",
    "StaxExecutableReadError",
    "StaxExecutableSymlinkError",
    "StaxMalformedDoctorOutputError",
    "StaxMalformedVersionError",
    "StaxPlatformError",
    "StaxPreflight",
    "StaxPreflightError",
    "StaxPreflightResult",
    "StaxRepositoryNotInitializedError",
    "StaxRepositoryNotFoundError",
    "StaxUnsupportedPlatformError",
    "StaxVersionMismatchError",
    "SubprocessCommandRunner",
    "current_platform",
    "parse_stax_version",
    "sha256_file",
]


class StaxPreflightError(Exception):
    """Base class for all typed Stax preflight failures."""


class StaxExecutableNotAbsoluteError(StaxPreflightError):
    """The configured executable path was not absolute."""

    def __init__(self, executable: str) -> None:
        self.executable = executable
        super().__init__(
            f"Stax executable must be an absolute path, got {executable!r}"
        )


class StaxExecutableNotFoundError(StaxPreflightError):
    """No usable executable exists at the configured absolute path."""

    def __init__(self, executable: str) -> None:
        self.executable = executable
        super().__init__(f"Stax executable not found: {executable}")


class StaxExecutableNotExecutableError(StaxPreflightError):
    """The path exists but is not executable by this process."""

    def __init__(self, executable: str) -> None:
        self.executable = executable
        super().__init__(f"Stax executable is not executable: {executable}")


class StaxExecutableSymlinkError(StaxPreflightError):
    """The configured executable is a symlink (symlink drift is rejected)."""

    def __init__(self, executable: str, target: str | None = None) -> None:
        self.executable = executable
        self.target = target
        detail = f" -> {target}" if target else ""
        super().__init__(
            "Stax executable must be the pinned regular file, not a symlink: "
            f"{executable}{detail}"
        )


class StaxExecutableReadError(StaxPreflightError):
    """The executable could not be read for digest verification."""

    def __init__(self, executable: str, reason: str) -> None:
        self.executable = executable
        self.reason = reason
        super().__init__(f"cannot read Stax executable {executable}: {reason}")


class StaxExecutableDigestMismatchError(StaxPreflightError):
    """The executable digest is not the pinned digest."""

    def __init__(self, executable: str, expected: str, actual: str) -> None:
        self.executable = executable
        self.expected = expected
        self.actual = actual
        super().__init__(
            "Stax executable digest mismatch for "
            f"{executable}: expected {expected}, got {actual}"
        )


class StaxPlatformError(StaxPreflightError):
    """Base class for platform-compatibility failures."""


class StaxUnsupportedPlatformError(StaxPlatformError):
    """The host/configured platform is not supported by the pin."""

    def __init__(self, platform: str, supported_platform: str) -> None:
        self.platform = platform
        self.supported_platform = supported_platform
        super().__init__(
            f"Stax platform {platform!r} is unsupported; pin requires "
            f"{supported_platform!r}"
        )


class StaxCommandExecutionError(StaxPreflightError):
    """The Stax invocation could not run or returned a non-zero exit code."""

    def __init__(
        self,
        argv: Sequence[str],
        reason: str,
        result: "CommandResult | None" = None,
    ) -> None:
        self.argv = tuple(argv)
        self.reason = reason
        self.result = result
        super().__init__(
            f"Stax command failed ({' '.join(self.argv)}): {reason}"
        )


class StaxMalformedVersionError(StaxPreflightError):
    """Version output was absent or did not match an accepted exact form."""

    def __init__(self, output: str) -> None:
        self.output = output
        super().__init__(
            "malformed Stax version output; expected exactly one line like "
            f"'stax 0.113.1', got {output!r}"
        )


class StaxVersionMismatchError(StaxPreflightError):
    """The executable reported a version other than the pin."""

    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Stax version mismatch: pinned {expected!r}, executable reported "
            f"{actual!r}"
        )


class StaxRepositoryNotFoundError(StaxPreflightError):
    """The repository argument is not an existing directory."""

    def __init__(self, repository: str) -> None:
        self.repository = repository
        super().__init__(f"Stax repository path not found: {repository}")


class StaxRepositoryNotInitializedError(StaxPreflightError):
    """Stax reported that the repository has not been initialized."""

    def __init__(self, repository: str, output: str) -> None:
        self.repository = repository
        self.output = output
        super().__init__(
            "Stax repository is not initialized; run the reviewed "
            f"initialization step explicitly: {repository}"
        )


class StaxMalformedDoctorOutputError(StaxPreflightError):
    """Doctor output did not contain an unambiguous initialization status."""

    def __init__(self, repository: str, output: str) -> None:
        self.repository = repository
        self.output = output
        super().__init__(
            "could not determine Stax repository initialization from doctor "
            f"output for {repository}: {output!r}"
        )


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured result of one injected command invocation."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    """Minimal command-execution seam used by :class:`StaxPreflight`."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> CommandResult: ...


def sha256_file(path: str | os.PathLike[str], *, chunk_size: int = 1 << 20) -> str:
    """Return the lowercase SHA-256 digest of *path*."""

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


class SubprocessCommandRunner:
    """Default runner: no shell, bounded timeout, captured output."""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.timeout = timeout
        self.env = dict(env) if env is not None else None

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> CommandResult:
        try:
            completed = subprocess.run(
                list(argv),
                cwd=cwd,
                env=self.env,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StaxCommandExecutionError(argv, str(exc)) from exc
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def current_platform() -> str:
    """Return a Rust target triple for the current host where known.

    Only the reviewed macOS arm64 target is supported by the production pin.
    Unknown hosts still return a deterministic descriptive value so preflight
    can fail closed with a useful platform error.
    """

    system = _platform.system().strip().lower()
    machine = _platform.machine().strip().lower()
    known = {
        ("darwin", "arm64"): "aarch64-apple-darwin",
        ("darwin", "aarch64"): "aarch64-apple-darwin",
        ("darwin", "x86_64"): "x86_64-apple-darwin",
        ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
        ("linux", "amd64"): "x86_64-unknown-linux-gnu",
        ("linux", "aarch64"): "aarch64-unknown-linux-gnu",
        ("linux", "arm64"): "aarch64-unknown-linux-gnu",
    }
    return known.get((system, machine), f"{machine}-{system}")


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_VERSION_RE = re.compile(
    r"^(?:stax(?:-cli)?\s+)?v?(?P<version>"
    r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)\s*$",
    re.IGNORECASE,
)
_INITIALIZED_RE = re.compile(
    r"(?im)^\s*[✓✔]\s+Repo initiali[sz]ed\b"
)
_UNINITIALIZED_RE = re.compile(
    r"(?im)^\s*[✗✘×]\s+Repo not initiali[sz]ed\b"
    r"|not in a git repository",
)


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def parse_stax_version(output: str) -> str:
    """Parse exact ``stax --version`` output into a normalized version.

    Exactly one non-empty line is accepted. Accepted examples are
    ``stax 0.113.1``, ``stax-cli 0.113.1``, ``0.113.1`` and ``v0.113.1``.
    Diagnostics, extra lines and free-form text all fail closed.
    """

    lines = [
        line.strip()
        for line in _strip_ansi(output).replace("\r\n", "\n").splitlines()
        if line.strip()
    ]
    if len(lines) != 1:
        raise StaxMalformedVersionError(output)
    match = _VERSION_RE.fullmatch(lines[0])
    if match is None:
        raise StaxMalformedVersionError(output)
    return match.group("version")


@dataclass(frozen=True, slots=True)
class StaxPreflightResult:
    """Verified facts about one local Stax executable."""

    pin: StaxPin
    executable: str
    version: str
    executable_sha256: str
    platform: str
    repository: str | None = None
    repository_initialized: bool | None = None


class StaxPreflight:
    """Verify a Stax executable against an exact pin before any use.

    Parameters are injectable so the default test suite is deterministic and
    offline: ``runner`` replaces process execution, ``platform`` replaces host
    detection and ``digest`` replaces file hashing. Production should call
    :meth:`check` with an absolute executable path and no overrides.
    """

    def __init__(
        self,
        pin: StaxPin = PINNED_STAX,
        *,
        runner: CommandRunner | None = None,
        platform: str | None = None,
        digest: Callable[[Path], str] = sha256_file,
        timeout: float = 15.0,
    ) -> None:
        self.pin = pin
        self._platform = platform if platform is not None else current_platform()
        self._digest = digest
        self._runner = runner if runner is not None else SubprocessCommandRunner(
            timeout=timeout
        )

    def check(
        self,
        executable: str | os.PathLike[str],
        *,
        repository: str | os.PathLike[str] | None = None,
    ) -> StaxPreflightResult:
        """Run the full fail-closed preflight and return verified facts."""

        executable_path = Path(executable)
        executable_text = os.fspath(executable_path)

        if not executable_path.is_absolute():
            raise StaxExecutableNotAbsoluteError(executable_text)
        if executable_path.is_symlink():
            target = os.readlink(executable_path)
            raise StaxExecutableSymlinkError(executable_text, target)
        if not executable_path.exists() or not executable_path.is_file():
            raise StaxExecutableNotFoundError(executable_text)
        if not os.access(executable_path, os.X_OK):
            raise StaxExecutableNotExecutableError(executable_text)

        if not self.pin.supports_platform(self._platform):
            raise StaxUnsupportedPlatformError(
                self._platform, self.pin.supported_platform
            )

        # Integrity before execution: never run a binary that is not pinned.
        try:
            actual_digest = str(self._digest(executable_path)).lower()
        except OSError as exc:
            raise StaxExecutableReadError(executable_text, str(exc)) from exc
        if actual_digest != self.pin.executable_sha256:
            raise StaxExecutableDigestMismatchError(
                executable_text,
                self.pin.executable_sha256,
                actual_digest,
            )

        version_result = self._run((executable_text, "--version"), cwd=None)
        reported_version = parse_stax_version(version_result.stdout)
        if reported_version != self.pin.version:
            raise StaxVersionMismatchError(
                self.pin.version, reported_version
            )

        repository_text: str | None = None
        repository_initialized: bool | None = None
        if repository is not None:
            repository_path = Path(repository)
            if (
                not repository_path.exists()
                or not repository_path.is_dir()
            ):
                raise StaxRepositoryNotFoundError(os.fspath(repository_path))
            repository_path = repository_path.resolve()
            repository_text = os.fspath(repository_path)

            doctor_result = self._run(
                (executable_text, "doctor"), cwd=repository_path
            )
            doctor_output = _strip_ansi(
                "\n".join(
                    part
                    for part in (
                        doctor_result.stdout,
                        doctor_result.stderr,
                    )
                    if part
                )
            )
            if _UNINITIALIZED_RE.search(doctor_output):
                raise StaxRepositoryNotInitializedError(
                    repository_text, doctor_output
                )
            if _INITIALIZED_RE.search(doctor_output) is None:
                raise StaxMalformedDoctorOutputError(
                    repository_text, doctor_output
                )
            repository_initialized = True

        return StaxPreflightResult(
            pin=self.pin,
            executable=executable_text,
            version=reported_version,
            executable_sha256=actual_digest,
            platform=self._platform,
            repository=repository_text,
            repository_initialized=repository_initialized,
        )

    def preflight(
        self,
        executable: str | os.PathLike[str],
        *,
        repository: str | os.PathLike[str] | None = None,
    ) -> StaxPreflightResult:
        """Alias for :meth:`check` for callers that name the operation."""

        return self.check(executable, repository=repository)

    def _run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None,
    ) -> CommandResult:
        try:
            result = self._runner(argv, cwd=cwd)
        except StaxPreflightError:
            raise
        except OSError as exc:
            raise StaxCommandExecutionError(argv, str(exc)) from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            reason = f"exit {result.returncode}"
            if detail:
                reason = f"{reason}: {detail}"
            raise StaxCommandExecutionError(argv, reason, result)
        return result
