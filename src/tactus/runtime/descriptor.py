"""Pinned runtime descriptor and strict preflight — vendor-free.

A runtime is described by data, never discovered by convention. The descriptor
names:

* an **artifact set** — one file, or a tree with a declared digest order and
  file count (a single binary pins like Stax; a shell-script or node bundle
  pins as a tree);
* an optional **interpreter** artifact, for runtimes that are launched by one;
* an optional **launcher** (the OS containment wrapper) whose own digest is
  pinned and whose fixed arguments may contain placeholders;
* the exact argv template (with the ``{request}``/``{prompt}`` placeholders) and
  the exact probe that reports the runtime's version;
* the environment variable **names** the attempt may receive.

There is **no fallback list** and no vendor name anywhere in this module. A
missing, non-executable, unprobeable or version-mismatched runtime raises
:class:`RuntimePreflightError`; callers must fail closed rather than silently
selecting another provider or model (Issue #22).
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from tactus.runtime.containment import Launcher, digest_tree

__all__ = [
    "Artifact",
    "PLACEHOLDERS",
    "PROFILE_PLACEHOLDER",
    "PROMPT_PLACEHOLDER",
    "REQUEST_PLACEHOLDER",
    "RESULT_CONTRACTS",
    "STDIN_SOURCES",
    "TREE_DIGEST_ORDER",
    "WILDCARD_PLACEHOLDER",
    "RuntimeDescriptor",
    "RuntimePreflightError",
    "RuntimeProvenance",
    "preflight",
]

#: Replaced with the request path when a command is materialised.
REQUEST_PLACEHOLDER = "{request}"

#: Replaced with the task prompt file. The prompt is data on disk, never argv
#: (an argv is visible in ``ps`` and bounded by the platform).
PROMPT_PLACEHOLDER = "{prompt}"

#: Replaced with the rendered containment profile.
PROFILE_PLACEHOLDER = "{profile}"

#: Replaced with the workspace root.
WILDCARD_PLACEHOLDER = "{workspace}"

PLACEHOLDERS = (
    REQUEST_PLACEHOLDER,
    PROMPT_PLACEHOLDER,
    PROFILE_PLACEHOLDER,
    WILDCARD_PLACEHOLDER,
)

#: How the agent receives its task: nothing, or the prompt file on stdin.
STDIN_SOURCES = ("none", "prompt")

#: Where the result comes from. ``agent-artifacts`` expects the runtime to write
#: ``result.json``/``patch.diff`` itself (the fake canary does). ``workspace-diff``
#: expects nothing: the supervisor takes the changed paths and the patch from the
#: workspace, so a runtime that reports in its own shape — or not at all — is
#: still accounted for without inventing a fake artifact.
RESULT_CONTRACTS = ("agent-artifacts", "workspace-diff")

#: The only tree digest ordering this module defines. Declared in the pin so a
#: recorded digest is reproducible rather than incidental.
TREE_DIGEST_ORDER = "path-sorted"

#: Probe output is bounded: a runtime that floods its version command must not
#: be able to stall or balloon preflight.
_PROBE_OUTPUT_LIMIT = 4096

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ARTIFACT_KINDS = ("file", "tree")


class RuntimePreflightError(RuntimeError):
    """The configured runtime cannot be used as pinned (fail closed)."""


@dataclass(frozen=True, slots=True)
class Artifact:
    """One pinned artifact: a single file, or a tree with a manifest digest.

    ``sha256`` empty means "recorded, not yet pinned" and is refused by
    :meth:`verify` — an unpinned artifact is never accepted.
    """

    path: str
    sha256: str
    kind: str = "file"
    digest_order: str = TREE_DIGEST_ORDER
    file_count: int | None = None
    entrypoint: str = ""
    role: str = "runtime"

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.strip():
            raise ValueError("artifact path must be a non-empty string")
        candidate = Path(self.path).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"artifact path must be absolute: {self.path!r}")
        object.__setattr__(self, "path", str(candidate))
        if self.kind not in _ARTIFACT_KINDS:
            raise ValueError(f"artifact kind must be one of {list(_ARTIFACT_KINDS)}")
        if self.kind == "tree" and self.digest_order != TREE_DIGEST_ORDER:
            raise ValueError(
                f"artifact digest_order must be {TREE_DIGEST_ORDER!r} "
                f"(got {self.digest_order!r})"
            )
        if self.kind == "file" and self.file_count is not None:
            raise ValueError("a file artifact must not declare file_count")
        if self.kind == "tree" and (self.file_count is None or self.file_count < 1):
            raise ValueError("a tree artifact must declare its file_count")

    @property
    def resolved(self) -> Path:
        return Path(self.path)

    def verify(self) -> dict[str, Any]:
        """Verify the artifact against its pin, or raise (fail closed)."""
        if not self.sha256:
            raise RuntimePreflightError(
                f"artifact {self.path} ({self.role}) has no pinned digest; "
                "record it before use"
            )
        if not _SHA256_RE.fullmatch(self.sha256):
            raise RuntimePreflightError(
                f"artifact {self.path} ({self.role}) has a malformed digest"
            )
        if self.kind == "file":
            path = self.resolved
            if not path.is_file():
                raise RuntimePreflightError(f"artifact not found: {path} ({self.role})")
            observed = hashlib.sha256(path.read_bytes()).hexdigest()
            count = 1
        else:
            root = self.resolved
            if not root.is_dir():
                raise RuntimePreflightError(f"artifact tree not found: {root} ({self.role})")
            observed, count = digest_tree(root)
            if count != self.file_count:
                raise RuntimePreflightError(
                    f"artifact tree {root} ({self.role}) has {count} files; the pin "
                    f"records {self.file_count}"
                )
            if self.entrypoint:
                entry = root / self.entrypoint
                if not entry.exists():
                    raise RuntimePreflightError(
                        f"artifact tree {root} ({self.role}) has no entrypoint "
                        f"{self.entrypoint}"
                    )
        if observed != self.sha256:
            raise RuntimePreflightError(
                f"artifact digest mismatch for {self.path} ({self.role}): "
                f"pinned {self.sha256}, observed {observed}"
            )
        return {
            "path": self.path,
            "kind": self.kind,
            "role": self.role,
            "sha256": observed,
            "file_count": count,
            "digest_order": self.digest_order if self.kind == "tree" else "",
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "role": self.role,
            "sha256": self.sha256,
            "file_count": self.file_count,
            "digest_order": self.digest_order,
            "entrypoint": self.entrypoint,
        }


@dataclass(frozen=True, slots=True)
class RuntimeDescriptor:
    """One pinned runtime, exactly as it will be executed.

    ``argv`` is the full command template and must contain exactly one
    ``{request}`` placeholder. ``preflight_argv`` is a complete probe command;
    its combined output must contain ``expected_version_token``. When a
    ``launcher`` is pinned, the probe is executed *through the launcher* inside
    the containment self-test rather than outside it.
    """

    runtime_id: str
    adapter: str
    executable: str
    argv: tuple[str, ...]
    preflight_argv: tuple[str, ...]
    expected_version_token: str
    artifacts: tuple[Artifact, ...] = ()
    launcher: Launcher | None = None
    env_allowlist: tuple[str, ...] = ()
    stdin_from: str = "none"
    result_contract: str = "agent-artifacts"

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
        for part in tuple(argv) + tuple(self.preflight_argv):
            for placeholder in PLACEHOLDERS:
                if placeholder in part and placeholder == PROFILE_PLACEHOLDER:
                    if self.launcher is None:
                        raise ValueError(
                            "a {profile} placeholder requires a pinned launcher"
                        )
        artifacts = tuple(self.artifacts)
        for artifact in artifacts:
            if not isinstance(artifact, Artifact):
                raise ValueError("artifacts must be Artifact instances")
        object.__setattr__(self, "artifacts", artifacts)
        allowlist = tuple(self.env_allowlist)
        for name in allowlist:
            if not _ENV_NAME_RE.fullmatch(name):
                raise ValueError(f"environment names must be identifiers, not {name!r}")
        object.__setattr__(self, "env_allowlist", allowlist)
        if self.stdin_from not in STDIN_SOURCES:
            raise ValueError(
                f"stdin_from must be one of {list(STDIN_SOURCES)}, not {self.stdin_from!r}"
            )
        if self.result_contract not in RESULT_CONTRACTS:
            raise ValueError(
                f"result_contract must be one of {list(RESULT_CONTRACTS)}, "
                f"not {self.result_contract!r}"
            )
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "preflight_argv", tuple(self.preflight_argv))

    # -- commands --------------------------------------------------------

    def _substitute(self, parts: tuple[str, ...], values: Mapping[str, str]) -> list[str]:
        rendered: list[str] = []
        for part in parts:
            text = part
            for placeholder, value in values.items():
                text = text.replace(placeholder, value)
            remaining = [item for item in PLACEHOLDERS if item in text]
            if remaining:
                raise RuntimePreflightError(
                    f"cannot materialise {remaining[0]} in {part!r}: no value was supplied"
                )
            rendered.append(text)
        return rendered

    def command(
        self,
        request_path: str | os.PathLike[str],
        *,
        prompt_path: str | os.PathLike[str] | None = None,
        profile_path: str | os.PathLike[str] | None = None,
        workspace: str | os.PathLike[str] | None = None,
    ) -> list[str]:
        """Return the concrete argv for one attempt (placeholders substituted)."""
        values = {REQUEST_PLACEHOLDER: str(request_path)}
        if prompt_path is not None:
            values[PROMPT_PLACEHOLDER] = str(prompt_path)
        if profile_path is not None:
            values[PROFILE_PLACEHOLDER] = str(profile_path)
        if workspace is not None:
            values[WILDCARD_PLACEHOLDER] = str(workspace)
        prefix = self.launcher.argv_prefix if self.launcher is not None else ()
        return self._substitute(tuple(prefix) + self.argv, values)

    def probe_command(
        self,
        *,
        profile_path: str | os.PathLike[str] | None = None,
        workspace: str | os.PathLike[str] | None = None,
    ) -> list[str]:
        """The version probe, through the launcher when one is pinned."""
        values: dict[str, str] = {}
        if profile_path is not None:
            values[PROFILE_PLACEHOLDER] = str(profile_path)
        if workspace is not None:
            values[WILDCARD_PLACEHOLDER] = str(workspace)
        prefix = self.launcher.argv_prefix if self.launcher is not None else ()
        return self._substitute(tuple(prefix) + tuple(self.preflight_argv), values)

    def as_dict(self) -> dict[str, object]:
        return {
            "runtime_id": self.runtime_id,
            "adapter": self.adapter,
            "executable": self.executable,
            "argv": list(self.argv),
            "preflight_argv": list(self.preflight_argv),
            "expected_version_token": self.expected_version_token,
            "artifacts": [artifact.as_dict() for artifact in self.artifacts],
            "launcher": self.launcher.as_dict() if self.launcher else None,
            "env_allowlist": list(self.env_allowlist),
            "stdin_from": self.stdin_from,
            "result_contract": self.result_contract,
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
    artifacts: tuple[dict[str, Any], ...] = ()
    launcher_sha256: str = ""
    probe_mode: str = "executed"
    host: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "runtime_id": self.runtime_id,
            "adapter": self.adapter,
            "executable": self.executable,
            "executable_sha256": self.executable_sha256,
            "reported_version": self.reported_version,
            "checked_at": self.checked_at.isoformat(),
            "artifacts": [dict(item) for item in self.artifacts],
            "launcher_sha256": self.launcher_sha256,
            "probe_mode": self.probe_mode,
            "host": self.host,
        }


def _bounded(text: str, limit: int = _PROBE_OUTPUT_LIMIT) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "... [truncated]"


def _host() -> str:
    from tactus.runtime.containment import describe_host

    return describe_host()


def _verify_artifacts(descriptor: RuntimeDescriptor) -> tuple[dict[str, Any], ...]:
    """Verify every pinned artifact and return what was observed."""
    observed: list[dict[str, Any]] = []
    entrypoint_checked = False
    for artifact in descriptor.artifacts:
        record = artifact.verify()
        observed.append(record)
        if (
            artifact.kind == "tree"
            and artifact.entrypoint
            and Path(descriptor.executable).resolve()
            == (artifact.resolved / artifact.entrypoint).resolve()
        ):
            entrypoint_checked = True
    trees = [item for item in descriptor.artifacts if item.kind == "tree" and item.entrypoint]
    if trees and not entrypoint_checked:
        raise RuntimePreflightError(
            "the pinned executable "
            f"{descriptor.executable} is not the entrypoint of a pinned tree artifact"
        )
    return tuple(observed)


def preflight(
    descriptor: RuntimeDescriptor, *, timeout: float = 10.0
) -> RuntimeProvenance:
    """Verify the pinned runtime is present, unmodified and the pinned version.

    Fails closed on every anomaly. Never selects an alternative runtime. When a
    launcher is pinned, this function executes **nothing**: the version probe
    runs inside the containment self-test, so the runtime is never started
    outside the boundary.
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

    artifacts = _verify_artifacts(descriptor)
    launcher_sha256 = ""
    if descriptor.launcher is not None:
        launcher_sha256 = descriptor.launcher.verify()

    # The runtime we probe must be the runtime we launch: an argv naming a
    # different file could silently verify one executable and run another.
    for label, candidate in (
        ("argv[0]", descriptor.argv[0]),
        ("preflight_argv[0]", descriptor.preflight_argv[0]),
    ):
        if Path(candidate).resolve() != executable.resolve():
            raise RuntimePreflightError(
                f"runtime {descriptor.runtime_id!r}: {label} ({candidate}) is not the "
                f"pinned executable ({executable}); refusing to probe one binary and "
                "launch another"
            )
    if descriptor.launcher is not None:
        prefix_head = Path(descriptor.launcher.argv_prefix[0]).resolve()
        if prefix_head != Path(descriptor.launcher.path).resolve():
            raise RuntimePreflightError(
                f"runtime {descriptor.runtime_id!r}: launcher argv_prefix[0] is not the "
                "pinned launcher"
            )

    reported = ""
    probe_mode = "containment-self-test"
    if descriptor.launcher is None:
        probe_mode = "executed"
        try:
            completed = subprocess.run(
                list(descriptor.preflight_argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                # No inherited interpreter/tool configuration: the probe runs with
                # a fixed PATH and nothing else. This is hygiene, not a sandbox.
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
        artifacts=artifacts,
        launcher_sha256=launcher_sha256,
        probe_mode=probe_mode,
        host=_host(),
    )
