"""Bounded runtime supervision: one agent process per attempt, fully accounted for.

The supervisor owns the process lifecycle of a single agent run inside a
workspace and returns structured evidence. It is a *supervision* port, not a
sandbox: the child runs as the same OS account with the same filesystem and
network reach. What it enforces is:

* one pinned runtime (strict preflight, no fallback — see :mod:`descriptor`);
* an explicit workspace and a declared write scope (validated after the run);
* a hard deadline and an explicit cancellation signal;
* **confirmed process-tree termination** — the whole process group is signalled
  and observed gone before anything else proceeds;
* a writer lock that is **never released while termination is unconfirmed**;
* bounded output capture and structured evidence (exit, timing, patch digest,
  changed paths, validation errors).

Termination confirmation also survives a restart: :meth:`RuntimeSupervisor.recover`
reads the durable attempt record and terminates any process group a previous
process left behind before the attempt is considered settled.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from tactus.runtime.descriptor import (
    RuntimeDescriptor,
    RuntimeProvenance,
    preflight,
)
from tactus.runtime.workspace import (
    Workspace,
    WorkspaceScopeError,
    patch_header_paths,
    validate_changed_paths,
)

__all__ = [
    "RecoveryReport",
    "RuntimeContractError",
    "RuntimeEvidence",
    "RuntimeLimits",
    "RuntimeRequest",
    "RuntimeSupervisionError",
    "RuntimeSupervisor",
    "TerminationReport",
    "WriterLock",
    "WriterLockError",
]

#: The agent contract: it reads the request JSON and writes ``result.json``
#: (plus ``patch.diff`` when it reports ``completed``) into ``output_dir``.
RESULT_FILE = "result.json"
PATCH_FILE = "patch.diff"
REQUEST_FILE = "request.json"
ATTEMPT_FILE = "attempt.json"
LOCK_FILE = "writer.lock"
SUPPORTED_RESULT_SCHEMA = 1


class RuntimeSupervisionError(RuntimeError):
    """A runtime attempt could not be supervised safely."""


class WriterLockError(RuntimeSupervisionError):
    """The writer lock cannot be taken or must not be released."""


class RuntimeContractError(RuntimeSupervisionError):
    """The agent's result artifacts violate the declared contract."""


@dataclass(frozen=True, slots=True)
class RuntimeLimits:
    """Bounds applied to one attempt."""

    timeout_seconds: float = 300.0
    max_output_bytes: int = 4096
    max_patch_bytes: int = 200_000
    max_changed_paths: int = 20
    termination_grace_seconds: float = 5.0
    poll_interval_seconds: float = 0.02

    def __post_init__(self) -> None:
        for name in (
            "timeout_seconds",
            "termination_grace_seconds",
            "poll_interval_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("max_output_bytes", "max_patch_bytes", "max_changed_paths"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")

    def as_dict(self) -> dict[str, Any]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "max_patch_bytes": self.max_patch_bytes,
            "max_changed_paths": self.max_changed_paths,
            "termination_grace_seconds": self.termination_grace_seconds,
        }


@dataclass(frozen=True, slots=True)
class RuntimeRequest:
    """One bounded attempt: which intent it serves and which capability it runs."""

    attempt_id: str
    intent_id: str
    capability: str

    def __post_init__(self) -> None:
        for name in ("attempt_id", "intent_id", "capability"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"runtime request {name} must be a non-empty string")

    def as_dict(self) -> dict[str, str]:
        return {
            "attempt_id": self.attempt_id,
            "intent_id": self.intent_id,
            "capability": self.capability,
        }


@dataclass(frozen=True, slots=True)
class TerminationReport:
    """What was signalled, and whether the whole group is demonstrably gone."""

    confirmed: bool
    signals: tuple[str, ...] = ()
    alive_after: bool = False
    confirmed_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "confirmed": self.confirmed,
            "signals": list(self.signals),
            "alive_after": self.alive_after,
            "confirmed_at": None if self.confirmed_at is None else self.confirmed_at.isoformat(),
        }


def _group_state(pgid: int) -> str:
    """Classify a process group from *our* point of view.

    ``alive``   — the group still names at least one process we may signal.
    ``gone``    — no such group.
    ``foreign`` — the pgid exists but is not ours (it was recycled after our
                  group emptied), so our group is demonstrably gone.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        return "foreign"
    return "alive"


def terminate_process_group(
    pgid: int, process: "subprocess.Popen[bytes] | None", grace_seconds: float
) -> TerminationReport:
    """SIGTERM then SIGKILL the group, and confirm it is gone.

    Confirmation is an observation, not an assumption: the report is only
    ``confirmed`` when the group no longer names a process we could signal. A
    group whose pgid was recycled after emptying is reported as gone (we cannot
    have members left in a group we no longer own), and a group that survives
    both signals is reported unconfirmed so the caller keeps the writer lock.

    ``process`` (when known) is reaped before confirmation so a zombie leader
    cannot be mistaken for a live child.
    """
    if process is not None:
        # Reap first: a zombie leader keeps the process group visible, which
        # would make confirmation wait out the whole grace period for nothing.
        with contextlib.suppress(Exception):
            process.wait(timeout=0.5)
    signals_sent: list[str] = []
    for name, sig, wait_for in (
        ("SIGTERM", signal.SIGTERM, grace_seconds),
        ("SIGKILL", signal.SIGKILL, 2.0),
    ):
        if _group_state(pgid) != "alive":
            break
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            break
        signals_sent.append(name)
        deadline = time.monotonic() + wait_for
        while time.monotonic() < deadline:
            if process is not None:
                with contextlib.suppress(Exception):
                    process.poll()
            if _group_state(pgid) != "alive":
                break
            time.sleep(0.02)
    if process is not None:
        with contextlib.suppress(Exception):
            process.wait(timeout=5.0)
    alive = _group_state(pgid) == "alive"
    return TerminationReport(
        confirmed=not alive,
        signals=tuple(signals_sent),
        alive_after=alive,
        confirmed_at=None if alive else datetime.now(timezone.utc),
    )


class WriterLock:
    """Exclusive writer lock for one workspace attempt.

    The lock is released **only** after termination has been confirmed. A caller
    that has not confirmed termination gets :class:`WriterLockError` and the lock
    stays held: an unknown-live agent must not hand the workspace to the next
    writer.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._handle = None
        self._termination_confirmed = False

    @property
    def held(self) -> bool:
        return self._handle is not None

    @property
    def termination_confirmed(self) -> bool:
        return self._termination_confirmed

    def acquire(self) -> "WriterLock":
        if self._handle is not None:
            return self
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise WriterLockError(
                    f"workspace {self._path.parent} is already owned by another writer"
                ) from exc
            raise
        self._handle = handle
        return self

    def confirm_termination(self) -> None:
        self._termination_confirmed = True

    def release(self) -> None:
        if self._handle is None:
            return
        if not self._termination_confirmed:
            raise WriterLockError(
                "refusing to release the writer lock while process termination is "
                "unconfirmed"
            )
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """What a restart found and settled for a previous attempt."""

    found: bool
    attempt_id: str = ""
    pgid: int | None = None
    termination: TerminationReport | None = None
    recovered: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "attempt_id": self.attempt_id,
            "pgid": self.pgid,
            "termination": None if self.termination is None else self.termination.as_dict(),
            "recovered": self.recovered,
        }


@dataclass(frozen=True, slots=True)
class RuntimeEvidence:
    """Structured, JSON-serialisable account of one supervised attempt."""

    status: str
    request: RuntimeRequest
    provenance: RuntimeProvenance
    workspace: Workspace
    limits: RuntimeLimits
    started_at: datetime
    finished_at: datetime
    duration_ms: float
    pid: int | None
    pgid: int | None
    exit_code: int | None
    timed_out: bool
    cancelled: bool
    cancel_reason: str
    termination: TerminationReport
    writer_lock: Mapping[str, Any]
    stdout: str
    stderr: str
    output_truncated: bool
    patch_sha256: str
    patch_bytes: int
    changed_paths: tuple[str, ...]
    agent_status: str
    agent_summary: str
    errors: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": self.status,
            "request": self.request.as_dict(),
            "runtime": self.provenance.as_dict(),
            "workspace": self.workspace.as_dict(),
            "limits": self.limits.as_dict(),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_ms": self.duration_ms,
            "process": {
                "pid": self.pid,
                "pgid": self.pgid,
                "exit_code": self.exit_code,
                "timed_out": self.timed_out,
                "cancelled": self.cancelled,
                "cancel_reason": self.cancel_reason,
            },
            "termination": self.termination.as_dict(),
            "writer_lock": dict(self.writer_lock),
            "output": {
                "stdout": self.stdout,
                "stderr": self.stderr,
                "truncated": self.output_truncated,
            },
            "artifacts": {
                "agent_status": self.agent_status,
                "agent_summary": self.agent_summary,
                "changed_paths": list(self.changed_paths),
                "patch_sha256": self.patch_sha256,
                "patch_bytes": self.patch_bytes,
            },
            "validation": {"errors": list(self.errors)},
        }


class _BoundedReader(threading.Thread):
    """Drain one pipe, retaining at most ``limit`` bytes."""

    def __init__(self, stream, limit: int) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self.buffer = bytearray()
        self.truncated = False
        self.total = 0

    def run(self) -> None:  # pragma: no cover - thread body
        try:
            while True:
                chunk = self._stream.read(8192)
                if not chunk:
                    break
                self.total += len(chunk)
                room = self._limit - len(self.buffer)
                if room > 0:
                    self.buffer.extend(chunk[:room])
                if len(chunk) > max(room, 0):
                    self.truncated = True
        except (ValueError, OSError):
            return
        finally:
            with contextlib.suppress(Exception):
                self._stream.close()

    def text(self) -> str:
        return self.buffer.decode("utf-8", "replace")


class RuntimeSupervisor:
    """Supervise one agent runtime inside one workspace."""

    def __init__(
        self,
        descriptor: RuntimeDescriptor,
        workspace: Workspace,
        *,
        limits: RuntimeLimits | None = None,
        state_dir: str | os.PathLike[str] | None = None,
        terminate: Callable[[int, "subprocess.Popen[bytes] | None", float], TerminationReport]
        | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._workspace = workspace
        self._limits = limits or RuntimeLimits()
        self._state_dir = (
            Path(state_dir).resolve()
            if state_dir is not None
            else (workspace.root / ".tactus-runtime").resolve()
        )
        self._terminate = terminate or terminate_process_group
        self._cancel = threading.Event()
        self._cancel_reason = ""

    # -- read-only views -------------------------------------------------

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    @property
    def workspace(self) -> Workspace:
        return self._workspace

    @property
    def descriptor(self) -> RuntimeDescriptor:
        return self._descriptor

    # -- lifecycle -------------------------------------------------------

    def preflight(self) -> RuntimeProvenance:
        return preflight(self._descriptor)

    def cancel(self, reason: str = "operator cancelled the attempt") -> None:
        """Request cancellation; the run loop terminates the process group."""
        self._cancel_reason = reason
        self._cancel.set()

    def run(self, request: RuntimeRequest) -> RuntimeEvidence:
        """Supervise one attempt and return evidence. Never raises for agent faults."""
        if not isinstance(request, RuntimeRequest):
            raise RuntimeSupervisionError("a RuntimeRequest is required")
        provenance = self.preflight()
        self._state_dir.mkdir(parents=True, exist_ok=True)
        output_dir = self._state_dir / "agent-output"
        output_dir.mkdir(parents=True, exist_ok=True)
        for stale in (output_dir / RESULT_FILE, output_dir / PATCH_FILE):
            with contextlib.suppress(FileNotFoundError):
                stale.unlink()

        lock = WriterLock(self._state_dir / LOCK_FILE).acquire()
        request_path = self._state_dir / REQUEST_FILE
        _write_json(
            request_path,
            {
                **request.as_dict(),
                "workspace": self._workspace.as_dict(),
                "output_dir": str(output_dir),
                "limits": self._limits.as_dict(),
            },
        )
        started_at = datetime.now(timezone.utc)
        stopwatch = time.monotonic()
        process: subprocess.Popen[bytes] | None = None
        pgid: int | None = None
        pid: int | None = None
        timed_out = False
        cancelled = False
        termination = TerminationReport(confirmed=False)
        lock_state: dict[str, Any] = {"acquired": True, "released": False, "refused": ""}
        stdout_reader: _BoundedReader | None = None
        stderr_reader: _BoundedReader | None = None
        try:
            _write_json(
                self._state_dir / ATTEMPT_FILE,
                {
                    "attempt_id": request.attempt_id,
                    "phase": "spawning",
                    "pid": None,
                    "pgid": None,
                    "started_at": started_at.isoformat(),
                },
            )
            process = subprocess.Popen(
                self._descriptor.command(request_path),
                cwd=str(self._workspace.root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_child_environment(request),
                start_new_session=True,
            )
            pid = process.pid
            pgid = process.pid  # start_new_session == new process group leader
            _write_json(
                self._state_dir / ATTEMPT_FILE,
                {
                    "attempt_id": request.attempt_id,
                    "phase": "running",
                    "pid": pid,
                    "pgid": pgid,
                    "started_at": started_at.isoformat(),
                },
            )
            stdout_reader = _BoundedReader(process.stdout, self._limits.max_output_bytes)
            stderr_reader = _BoundedReader(process.stderr, self._limits.max_output_bytes)
            stdout_reader.start()
            stderr_reader.start()

            deadline = stopwatch + self._limits.timeout_seconds
            while process.poll() is None:
                if self._cancel.is_set():
                    cancelled = True
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(self._limits.poll_interval_seconds)
        finally:
            if pgid is not None:
                termination = self._terminate(
                    pgid, process, self._limits.termination_grace_seconds
                )
                if termination.confirmed:
                    lock.confirm_termination()
                try:
                    lock.release()
                    lock_state["released"] = True
                except WriterLockError as exc:
                    lock_state["refused"] = str(exc)
                _write_json(
                    self._state_dir / ATTEMPT_FILE,
                    {
                        "attempt_id": request.attempt_id,
                        "phase": "finished" if termination.confirmed else "unterminated",
                        "pid": pid,
                        "pgid": pgid,
                        "started_at": started_at.isoformat(),
                        "termination_confirmed": termination.confirmed,
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            for reader in (stdout_reader, stderr_reader):
                if reader is not None:
                    reader.join(timeout=5.0)

        stdout = stdout_reader.text() if stdout_reader else ""
        stderr = stderr_reader.text() if stderr_reader else ""
        truncated = bool(
            (stdout_reader and stdout_reader.truncated)
            or (stderr_reader and stderr_reader.truncated)
        )
        exit_code = process.returncode if process is not None else None

        (
            agent_status,
            agent_summary,
            changed_paths,
            patch_sha256,
            patch_bytes,
            errors,
            contract_violation,
        ) = self._collect_artifacts(output_dir, exit_code=exit_code)
        status = _status_for(
            exit_code=exit_code,
            timed_out=timed_out,
            cancelled=cancelled,
            termination=termination,
            contract_violation=contract_violation,
            agent_status=agent_status,
        )
        return RuntimeEvidence(
            status=status,
            request=request,
            provenance=provenance,
            workspace=self._workspace,
            limits=self._limits,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            duration_ms=round((time.monotonic() - stopwatch) * 1000, 3),
            pid=pid,
            pgid=pgid,
            exit_code=exit_code,
            timed_out=timed_out,
            cancelled=cancelled,
            cancel_reason=self._cancel_reason,
            termination=termination,
            writer_lock=lock_state,
            stdout=stdout,
            stderr=stderr,
            output_truncated=truncated,
            patch_sha256=patch_sha256,
            patch_bytes=patch_bytes,
            changed_paths=changed_paths,
            agent_status=agent_status,
            agent_summary=agent_summary,
            errors=errors,
        )

    def recover(self) -> RecoveryReport:
        """Settle an attempt a previous process left behind (restart safety)."""
        attempt_path = self._state_dir / ATTEMPT_FILE
        try:
            record = json.loads(attempt_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return RecoveryReport(found=False)
        phase = str(record.get("phase", ""))
        attempt_id = str(record.get("attempt_id", ""))
        raw_pgid = record.get("pgid")
        pgid = raw_pgid if isinstance(raw_pgid, int) else None
        if phase not in {"spawning", "running"} or pgid is None:
            return RecoveryReport(found=True, attempt_id=attempt_id, pgid=pgid)
        termination = self._terminate(pgid, None, self._limits.termination_grace_seconds)
        _write_json(
            attempt_path,
            {
                **record,
                "phase": "recovered" if termination.confirmed else "unterminated",
                "recovered_at": datetime.now(timezone.utc).isoformat(),
                "termination_confirmed": termination.confirmed,
            },
        )
        return RecoveryReport(
            found=True,
            attempt_id=attempt_id,
            pgid=pgid,
            termination=termination,
            recovered=termination.confirmed,
        )

    # -- artifact contract ----------------------------------------------

    def _collect_artifacts(
        self, output_dir: Path, *, exit_code: int | None
    ) -> tuple[str, str, tuple[str, ...], str, int, tuple[str, ...], bool]:
        """Return artifacts plus ``(errors, contract_violation)``.

        An **absent** result is a failed run (the agent produced nothing). A
        result or patch that **breaks the declared contract** is a violation —
        that distinction drives ``failed`` versus ``invalid``.
        """
        result_path = output_dir / RESULT_FILE
        if not result_path.is_file():
            return (
                "missing",
                "",
                (),
                "",
                0,
                ("agent produced no result.json",),
                False,
            )
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return (
                "invalid",
                "",
                (),
                "",
                0,
                (f"result.json is not valid JSON: {exc}",),
                True,
            )
        if not isinstance(result, Mapping):
            return ("invalid", "", (), "", 0, ("result.json must be a JSON object",), True)
        if result.get("schema_version") != SUPPORTED_RESULT_SCHEMA:
            return (
                "invalid",
                "",
                (),
                "",
                0,
                (f"result.json schema_version must be {SUPPORTED_RESULT_SCHEMA}",),
                True,
            )
        agent_status = str(result.get("status", ""))
        summary = str(result.get("summary", ""))
        if agent_status not in {"completed", "failed"}:
            return (
                "invalid",
                summary,
                (),
                "",
                0,
                (f"result.json status must be 'completed' or 'failed', got {agent_status!r}",),
                True,
            )

        errors: list[str] = []
        changed_paths: tuple[str, ...] = ()
        if agent_status == "completed":
            try:
                changed_paths = validate_changed_paths(
                    result.get("changed_paths"),
                    workspace=self._workspace,
                    max_paths=self._limits.max_changed_paths,
                )
            except WorkspaceScopeError as exc:
                errors.append(str(exc))

        patch_path = output_dir / PATCH_FILE
        patch_sha256 = ""
        patch_bytes = 0
        if agent_status == "completed":
            if not patch_path.is_file():
                errors.append("a completed run must produce patch.diff")
            else:
                raw = patch_path.read_bytes()
                patch_bytes = len(raw)
                if patch_bytes > self._limits.max_patch_bytes:
                    errors.append(
                        f"patch is {patch_bytes} bytes; limit is {self._limits.max_patch_bytes}"
                    )
                patch_sha256 = hashlib.sha256(raw).hexdigest()
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    errors.append("patch.diff must be UTF-8 text")
                    text = ""
                headers = patch_header_paths(text)
                if text and not headers:
                    errors.append("patch.diff names no file headers")
                undeclared = [path for path in headers if path not in changed_paths]
                if undeclared:
                    errors.append(
                        "patch.diff touches paths that were not declared: "
                        + ", ".join(undeclared)
                    )
                if exit_code not in (0, None):
                    errors.append("agent exited non-zero but reported completion")
        return (
            agent_status,
            summary,
            changed_paths,
            patch_sha256,
            patch_bytes,
            tuple(errors),
            bool(errors),
        )


def _status_for(
    *,
    exit_code: int | None,
    timed_out: bool,
    cancelled: bool,
    termination: TerminationReport,
    contract_violation: bool,
    agent_status: str,
) -> str:
    """Precedence: settled process → deadline/cancel → contract → agent outcome."""
    if not termination.confirmed:
        return "unterminated"
    if timed_out:
        return "timeout"
    if cancelled:
        return "cancelled"
    if contract_violation:
        return "invalid"
    if exit_code != 0:
        return "failed"
    if agent_status != "completed":
        return "failed"
    return "success"


def _child_environment(request: RuntimeRequest) -> dict[str, str]:
    """Minimal child environment: no credentials, no inherited configuration.

    Publication credentials are deliberately absent — an agent that may edit
    files has no publication authority (Issue #22). This is hygiene, not an
    isolation boundary: the child could still read whatever the OS account can.
    """
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TACTUS_ATTEMPT_ID": request.attempt_id,
        "TACTUS_INTENT_ID": request.intent_id,
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
