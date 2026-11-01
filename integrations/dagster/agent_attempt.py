"""Run one supervised coding-agent attempt for a Dagster capability (Dagster-free).

The Dagster code location (:mod:`agent_location`) owns the job, its steps and the
process environment. This module owns exactly one attempt: load the
operator-owned runtime pin, verify the pinned executable against it, supervise
one child process inside one workspace, and return a compact, JSON-safe account
of what happened.

What is proven, and what is not
-------------------------------
* The runtime is **pinned by data**: ``config/agent-runtime.json`` names one
  executable, its exact SHA-256, its argv template, its version probe and the
  token that probe must contain. There is no discovery and no fallback: a
  missing, unreadable, non-executable or digest-mismatched runtime raises
  :class:`AgentRuntimePinError` *before* any child process exists.
* The process is supervised by :class:`tactus.runtime.RuntimeSupervisor`: writer
  lock before any mutation, whole-exchange deadline, SIGTERM then SIGKILL to the
  process group, verified termination, and restart recovery that is fail-closed.
* The patch evidence is checked by
  :class:`tactus.integrations.agents.RuntimeAgentAdapter`: declared paths, patch
  headers, patch size and the patch digest must agree with the artifacts on disk.
* This is **not a sandbox**. The child runs as the same OS account with the same
  filesystem and network reach, and the runtime pinned for this canary is the
  deterministic **fake** agent, not a model provider. Filesystem, network and
  credential containment (Issue #22 AC3, gate B2) are undone, and every summary
  says so in ``isolation`` and ``runtime_kind``.

Nothing here decides anything. It reports process and artifact facts; what a
failure *means*, whether to try again and which runtime to prefer belong to
Ictus and Tactus.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from tactus.integrations.agents import RuntimeAgentAdapter
from tactus.runtime import (
    REQUEST_PLACEHOLDER,
    RuntimeDescriptor,
    RuntimeLimits,
    RuntimeRequest,
    RuntimeSupervisor,
    Workspace,
)

__all__ = [
    "ATTEMPT_SUMMARY_FILE",
    "DEFAULT_CONFIG_RELATIVE",
    "AgentAttemptError",
    "AgentRouteMismatch",
    "AgentRuntimePin",
    "AgentRuntimePinError",
    "assert_route_capability",
    "load_agent_runtime_pin",
    "run_agent_attempt",
]

#: The operator-owned runtime pin, relative to the repository root.
DEFAULT_CONFIG_RELATIVE = "config/agent-runtime.json"

#: Written next to the supervisor's artifacts in the attempt directory.
ATTEMPT_SUMMARY_FILE = "agent-attempt.json"

#: Replaced with the pinned executable's absolute path when a command is built.
EXECUTABLE_PLACEHOLDER = "{executable}"

#: Schema version of the pin file this module accepts.
RUNTIME_CONFIG_SCHEMA = 1

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "runtime_id",
        "runtime_kind",
        "adapter",
        "executable",
        "executable_sha256",
        "argv",
        "preflight_argv",
        "expected_version_token",
        "allowed_scope",
    }
)

_OPTIONAL_KEYS = frozenset({"limits", "purpose"})

_LIMIT_KEYS = frozenset(
    {
        "timeout_seconds",
        "max_output_bytes",
        "max_patch_bytes",
        "max_result_bytes",
        "max_changed_paths",
        "termination_grace_seconds",
        "poll_interval_seconds",
    }
)


class AgentAttemptError(RuntimeError):
    """One supervised agent attempt could not be accounted for."""


class AgentRouteMismatch(AgentAttemptError):
    """A run config named a capability this code location does not implement."""


def assert_route_capability(label: object, capability: str) -> str:
    """Return ``capability`` or refuse a label that does not match it (route guard).

    The capability of an attempt is a route constant. The label arrives in the run
    config the bridge derives from an intent payload, so it is *checked* here and
    never used to select anything.
    """

    if label != capability:
        raise AgentRouteMismatch(
            f"this code location implements {capability!r}, not {label!r}"
        )
    return capability


class AgentRuntimePinError(AgentAttemptError):
    """The operator-owned runtime pin is missing, malformed or does not match."""


@dataclass(frozen=True, slots=True)
class AgentRuntimePin:
    """One pinned coding-agent runtime, exactly as it will be executed.

    ``executable`` is the absolute path resolved from the pin file; the digest is
    verified against the pinned value before the descriptor is built, so the
    runtime that is probed is demonstrably the runtime that is launched.
    """

    schema_version: int
    runtime_id: str
    runtime_kind: str
    adapter: str
    executable: Path
    executable_sha256: str
    argv: tuple[str, ...]
    preflight_argv: tuple[str, ...]
    expected_version_token: str
    allowed_scope: tuple[str, ...]
    limits: RuntimeLimits
    purpose: str = ""

    def __post_init__(self) -> None:
        for name in (
            "runtime_id",
            "runtime_kind",
            "adapter",
            "expected_version_token",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise AgentRuntimePinError(f"runtime pin {name} must be a non-empty string")
        if not _SHA256_RE.fullmatch(self.executable_sha256):
            raise AgentRuntimePinError(
                "runtime pin executable_sha256 must be a lowercase SHA-256 hex digest"
            )
        if not self.allowed_scope:
            raise AgentRuntimePinError("runtime pin must declare a non-empty allowed_scope")
        if not self.argv or not self.preflight_argv:
            raise AgentRuntimePinError("runtime pin must declare argv and preflight_argv")

    def descriptor(self) -> RuntimeDescriptor:
        """The exact descriptor the supervisor will execute."""
        return RuntimeDescriptor(
            runtime_id=self.runtime_id,
            adapter=self.adapter,
            executable=str(self.executable),
            argv=self.argv,
            preflight_argv=self.preflight_argv,
            expected_version_token=self.expected_version_token,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "runtime_id": self.runtime_id,
            "runtime_kind": self.runtime_kind,
            "adapter": self.adapter,
            "executable": str(self.executable),
            "executable_sha256": self.executable_sha256,
            "argv": list(self.argv),
            "preflight_argv": list(self.preflight_argv),
            "expected_version_token": self.expected_version_token,
            "allowed_scope": list(self.allowed_scope),
            "limits": self.limits.as_dict(),
            "purpose": self.purpose,
        }


def _read_config(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AgentRuntimePinError(f"runtime pin file not found: {path}") from exc
    except OSError as exc:
        raise AgentRuntimePinError(f"runtime pin file is unreadable: {path}: {exc}") from exc
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AgentRuntimePinError(f"runtime pin file is not valid JSON: {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise AgentRuntimePinError(f"runtime pin file must contain a JSON object: {path}")
    return config


def _required_str(config: Mapping[str, Any], name: str, path: Path) -> str:
    value = config.get(name)
    if not isinstance(value, str) or not value.strip():
        raise AgentRuntimePinError(f"runtime pin {name} must be a non-empty string ({path})")
    return value


def _str_tuple(config: Mapping[str, Any], name: str, path: Path) -> tuple[str, ...]:
    value = config.get(name)
    if not isinstance(value, list) or not value:
        raise AgentRuntimePinError(
            f"runtime pin {name} must be a non-empty list of strings ({path})"
        )
    for item in value:
        if not isinstance(item, str) or not item:
            raise AgentRuntimePinError(
                f"runtime pin {name} must contain only non-empty strings ({path})"
            )
    return tuple(value)


def _resolve_executable(raw: str, repo_root: Path) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise AgentRuntimePinError(f"pinned runtime executable not found: {resolved}")
    if not os.access(resolved, os.X_OK):
        raise AgentRuntimePinError(
            f"pinned runtime executable is not executable: {resolved}"
        )
    return resolved


def _limits(config: Mapping[str, Any], path: Path) -> RuntimeLimits:
    raw = config.get("limits")
    if raw is None:
        return RuntimeLimits()
    if not isinstance(raw, dict):
        raise AgentRuntimePinError(f"runtime pin limits must be an object ({path})")
    unknown = set(raw) - _LIMIT_KEYS
    if unknown:
        raise AgentRuntimePinError(
            "runtime pin limits has unknown keys: " + ", ".join(sorted(unknown))
        )
    try:
        return RuntimeLimits(**raw)
    except (TypeError, ValueError) as exc:
        raise AgentRuntimePinError(f"runtime pin limits are invalid ({path}): {exc}") from exc


def load_agent_runtime_pin(
    config_path: str | os.PathLike[str] | None = None,
    *,
    repo_root: str | os.PathLike[str] | None = None,
) -> AgentRuntimePin:
    """Load and verify the operator-owned runtime pin (fail closed).

    The digest of the resolved executable is compared with the pinned digest here,
    so a changed or replaced runtime is rejected before preflight or spawning can
    observe it.
    """

    root = Path(repo_root).resolve() if repo_root is not None else _repo_root()
    path = (
        Path(config_path).expanduser().resolve()
        if config_path is not None
        else (root / DEFAULT_CONFIG_RELATIVE)
    )
    config = _read_config(path)

    schema_version = config.get("schema_version")
    if schema_version != RUNTIME_CONFIG_SCHEMA:
        raise AgentRuntimePinError(
            f"runtime pin schema_version must be {RUNTIME_CONFIG_SCHEMA} "
            f"(got {schema_version!r}): {path}"
        )
    unknown = set(config) - _REQUIRED_KEYS - _OPTIONAL_KEYS
    if unknown:
        raise AgentRuntimePinError(
            "runtime pin has unknown keys: " + ", ".join(sorted(unknown)) + f" ({path})"
        )
    missing = _REQUIRED_KEYS - set(config)
    if missing:
        raise AgentRuntimePinError(
            "runtime pin is missing required keys: " + ", ".join(sorted(missing))
        )

    executable = _resolve_executable(_required_str(config, "executable", path), root)
    pinned_digest = _required_str(config, "executable_sha256", path)
    if not _SHA256_RE.fullmatch(pinned_digest):
        raise AgentRuntimePinError(
            f"runtime pin executable_sha256 must be a lowercase SHA-256 hex digest ({path})"
        )
    actual_digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    if actual_digest != pinned_digest:
        raise AgentRuntimePinError(
            f"pinned runtime executable {executable} has digest {actual_digest}, "
            f"but the pin requires {pinned_digest}; refusing to run an unpinned runtime"
        )

    def command(values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            str(executable) if value == EXECUTABLE_PLACEHOLDER else value for value in values
        )

    argv = command(_str_tuple(config, "argv", path))
    if sum(1 for part in argv if part == REQUEST_PLACEHOLDER) != 1:
        raise AgentRuntimePinError(
            f"runtime pin argv must contain exactly one {REQUEST_PLACEHOLDER!r} ({path})"
        )
    preflight_argv = command(_str_tuple(config, "preflight_argv", path))
    purpose = config.get("purpose", "")
    if not isinstance(purpose, str):
        raise AgentRuntimePinError(f"runtime pin purpose must be a string ({path})")

    return AgentRuntimePin(
        schema_version=RUNTIME_CONFIG_SCHEMA,
        runtime_id=_required_str(config, "runtime_id", path),
        runtime_kind=_required_str(config, "runtime_kind", path),
        adapter=_required_str(config, "adapter", path),
        executable=executable,
        executable_sha256=pinned_digest,
        argv=argv,
        preflight_argv=preflight_argv,
        expected_version_token=_required_str(config, "expected_version_token", path),
        allowed_scope=_str_tuple(config, "allowed_scope", path),
        limits=_limits(config, path),
        purpose=purpose,
    )


def _repo_root() -> Path:
    """The repository root that contains ``config/`` and ``src/``."""

    return Path(__file__).resolve().parents[2]


def _summary(
    *,
    request: RuntimeRequest,
    pin: AgentRuntimePin,
    contract_version: str,
    result: Any,
) -> dict[str, Any]:
    evidence = result.evidence
    return {
        "schema_version": 1,
        "kind": "tactus.agent.attempt",
        "status": result.status,
        "attempt_id": request.attempt_id,
        "intent_id": request.intent_id,
        "capability": request.capability,
        "contract_version": contract_version,
        "runtime": {
            "runtime_id": pin.runtime_id,
            "runtime_kind": pin.runtime_kind,
            "adapter": pin.adapter,
            "executable": str(pin.executable),
            "pinned_executable_sha256": pin.executable_sha256,
            "observed_executable_sha256": evidence.provenance.executable_sha256,
            "reported_version": evidence.provenance.reported_version,
        },
        "workspace": {
            "root": str(evidence.workspace.root),
            "allowed_scope": list(evidence.workspace.allowed_scope),
        },
        "limits": evidence.limits.as_dict(),
        "process": {
            "attempt_dir": evidence.attempt_dir,
            "pid": evidence.pid,
            "pgid": evidence.pgid,
            "exit_code": evidence.exit_code,
            "duration_ms": evidence.duration_ms,
            "timed_out": evidence.timed_out,
            "cancelled": evidence.cancelled,
            "cancel_reason": evidence.cancel_reason,
        },
        "termination": evidence.termination.as_dict(),
        "writer_lock": dict(evidence.writer_lock),
        # What the supervisor could actually verify about the write scope. Without
        # a workspace diff provider this is unverified, and the account must say
        # so rather than let a reader assume the scope was checked on disk.
        "write_scope": dict(evidence.filesystem),
        "artifacts": {
            "agent_status": evidence.agent_status,
            "agent_summary": evidence.agent_summary,
            "changed_paths": list(result.changed_paths),
            "declared_paths": list(evidence.declared_paths),
            "patch_paths": list(evidence.patch_paths),
            "patch_sha256": result.patch_sha256,
            "patch_bytes": result.patch_bytes,
            "patch_path": result.patch_path,
        },
        "errors": list(result.errors),
        "publication_authority": result.publication_authority,
        "isolation": "none (same OS account: no filesystem or network boundary)",
    }


def run_agent_attempt(
    *,
    intent_id: str,
    capability: str,
    attempt: int,
    workspace_root: str | os.PathLike[str],
    repo_root: str | os.PathLike[str] | None = None,
    config_path: str | os.PathLike[str] | None = None,
    state_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Supervise one pinned agent attempt and return its bounded account.

    Raises :class:`AgentRuntimePinError` before anything runs when the pin is not
    satisfied, and lets supervisor errors (:class:`tactus.runtime` failures and
    recovery holds) propagate unchanged: the caller must not paper over them.
    """

    if not isinstance(attempt, int) or attempt < 1:
        raise AgentAttemptError("attempt must be a positive integer")
    root = Path(repo_root).resolve() if repo_root is not None else _repo_root()
    pin = load_agent_runtime_pin(config_path, repo_root=root)
    workspace = Workspace(
        root=Path(workspace_root),
        allowed_scope=pin.allowed_scope,
        # The workspace must never be the checkout the code lives in: an allowed
        # path inside this repository is not a bounded agent workspace.
        forbidden_roots=(root,),
    )
    supervisor = RuntimeSupervisor(
        pin.descriptor(),
        workspace,
        limits=pin.limits,
        state_dir=state_dir,
    )
    adapter = RuntimeAgentAdapter(supervisor)
    request = RuntimeRequest(
        attempt_id=f"{intent_id}.attempt-{attempt}",
        intent_id=intent_id,
        capability=capability,
    )
    result = adapter.run(request)
    summary = _summary(
        request=request,
        pin=pin,
        contract_version=adapter.contract.version,
        result=result,
    )
    # Durable, discoverable evidence next to the supervisor's own artifacts: the
    # attempt directory is per-attempt and never reused, so this cannot collide.
    summary_path = Path(result.evidence.attempt_dir) / ATTEMPT_SUMMARY_FILE
    _write_json(summary_path, summary)
    summary["process"]["summary_path"] = str(summary_path)
    return summary


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    try:
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        raise AgentAttemptError(f"cannot write attempt evidence {path}: {exc}") from exc
