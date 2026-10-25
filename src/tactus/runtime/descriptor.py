"""Pinned runtime descriptor and strict preflight.

A runtime is described by data, never discovered by convention: the descriptor
names one executable, the exact argv template, the exact probe that reports its
version, and the version token that must appear in that probe's output.

There is **no fallback list**. A missing, non-executable, unprobeable or
version-mismatched runtime raises :class:`RuntimePreflightError`; callers must
fail closed rather than silently selecting another provider or model (Issue #22).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "REQUEST_PLACEHOLDER",
    "RuntimeDescriptor",
    "RuntimePreflightError",
    "RuntimeProvenance",
    "preflight",
]

#: Replaced with the request path when a command is materialised.
REQUEST_PLACEHOLDER = "{request}"

#: Probe output is bounded: a runtime that floods its version command must not
#: be able to stall or balloon preflight.
_PROBE_OUTPUT_LIMIT = 4096


class RuntimePreflightError(RuntimeError):
    """The configured runtime cannot be used as pinned (fail closed)."""


@dataclass(frozen=True, slots=True)
class RuntimeDescriptor:
    """One pinned runtime, exactly as it will be executed.

    ``argv`` is the full command template and must contain exactly one
    ``{request}`` placeholder. ``preflight_argv`` is a complete probe command;
    its combined output must contain ``expected_version_token``.
    """

    runtime_id: str
    adapter: str
    executable: str
    argv: tuple[str, ...]
    preflight_argv: tuple[str, ...]
    expected_version_token: str

    def __post_init__(self) -> None:
        for name in ("runtime_id", "adapter", "executable", "expected_version_token"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"runtime descriptor {name} must be a non-empty string")
        executable = Path(self.executable)
        if not executable.is_absolute():
            raise ValueError("runtime descriptor executable must be an absolute path")
        argv = tuple(self.argv)
        if not argv:
            raise ValueError("runtime descriptor argv must not be empty")
        if argv.count(REQUEST_PLACEHOLDER) != 1:
            raise ValueError(
                f"runtime descriptor argv must contain exactly one {REQUEST_PLACEHOLDER!r}"
            )
        if not tuple(self.preflight_argv):
            raise ValueError("runtime descriptor preflight_argv must not be empty")
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "preflight_argv", tuple(self.preflight_argv))

    def command(self, request_path: str | os.PathLike[str]) -> list[str]:
        """Return the concrete argv for one request (placeholder substituted)."""
        return [
            str(request_path) if part == REQUEST_PLACEHOLDER else part for part in self.argv
        ]

    def as_dict(self) -> dict[str, object]:
        return {
            "runtime_id": self.runtime_id,
            "adapter": self.adapter,
            "executable": self.executable,
            "argv": list(self.argv),
            "preflight_argv": list(self.preflight_argv),
            "expected_version_token": self.expected_version_token,
        }


@dataclass(frozen=True, slots=True)
class RuntimeProvenance:
    """What preflight actually observed (evidence, not authorization)."""

    runtime_id: str
    adapter: str
    executable: str
    executable_sha256: str
    reported_version: str
    checked_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "runtime_id": self.runtime_id,
            "adapter": self.adapter,
            "executable": self.executable,
            "executable_sha256": self.executable_sha256,
            "reported_version": self.reported_version,
            "checked_at": self.checked_at.isoformat(),
        }


def _bounded(text: str, limit: int = _PROBE_OUTPUT_LIMIT) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "... [truncated]"


def preflight(
    descriptor: RuntimeDescriptor, *, timeout: float = 10.0
) -> RuntimeProvenance:
    """Verify the pinned runtime is present, executable and the pinned version.

    Fails closed on every anomaly. Never selects an alternative runtime.
    """
    if not isinstance(descriptor, RuntimeDescriptor):
        raise RuntimePreflightError("a RuntimeDescriptor is required")
    executable = Path(descriptor.executable)
    if not executable.is_file():
        raise RuntimePreflightError(
            f"runtime {descriptor.runtime_id!r}: executable not found: {executable}"
        )
    if not os.access(executable, os.X_OK):
        raise RuntimePreflightError(
            f"runtime {descriptor.runtime_id!r}: executable is not executable: {executable}"
        )

    try:
        completed = subprocess.run(
            list(descriptor.preflight_argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            # No inherited interpreter/tool configuration: the probe runs with a
            # fixed PATH and nothing else. This is hygiene, not a sandbox.
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimePreflightError(
            f"runtime {descriptor.runtime_id!r}: version probe timed out after {timeout}s"
        ) from exc
    except OSError as exc:
        raise RuntimePreflightError(
            f"runtime {descriptor.runtime_id!r}: version probe could not start: {exc}"
        ) from exc

    reported = _bounded((completed.stdout or "") + (completed.stderr or "")).strip()
    if completed.returncode != 0:
        raise RuntimePreflightError(
            f"runtime {descriptor.runtime_id!r}: version probe exited "
            f"{completed.returncode}: {reported or '<no output>'}"
        )
    if descriptor.expected_version_token not in reported:
        raise RuntimePreflightError(
            f"runtime {descriptor.runtime_id!r}: version mismatch; expected token "
            f"{descriptor.expected_version_token!r} in {reported!r}"
        )

    return RuntimeProvenance(
        runtime_id=descriptor.runtime_id,
        adapter=descriptor.adapter,
        executable=str(executable),
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        reported_version=reported,
        checked_at=datetime.now(timezone.utc),
    )
