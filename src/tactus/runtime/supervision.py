"""Bounded runtime supervision: one agent process per attempt, fully accounted for.

The supervisor owns the process lifecycle of a single agent run inside a
workspace and returns structured evidence. It is a *supervision* port, not a
sandbox: the child runs as the same OS account with the same filesystem and
network reach (see "Not claimed" below).

Lifecycle invariants
--------------------
1. **Exclusive ownership first.** The writer lock is taken before anything in the
   state directory is created, modified or deleted. A concurrent attempt fails
   without touching another attempt's state.
2. **Per-attempt evidence.** Each attempt gets its own directory
   (``attempts/<slug>-<nonce>/``). Nothing is ever deleted to "make room", so two
   attempts can never overwrite each other's request, result or patch.
3. **One writer.** ``flock(LOCK_EX | LOCK_NB)`` per state directory.
4. **Termination is observed, not assumed.** SIGTERM → SIGKILL to the whole
   process group, then the group's disappearance is verified.
5. **The lock outlives the process.** It is released only after termination is
   confirmed *and* the durable terminal record has been written. A failed
   artifact validation still writes the record first, so the workspace is never
   falsely marked available.
6. **Restart is fail-closed.** A leftover attempt is settled only when its
   process identity can be verified; an attempt whose identity is unknown, or
   whose termination was never confirmed, blocks new writers until an operator
   resolves it explicitly.

Not claimed
-----------
* This is **not a sandbox**. Same OS account, same filesystem and network reach.
  Write scope is *detected and rejected after the fact*, never prevented.
* Git-worktree isolation (Issue #23) is a separate boundary, and even that does
  not stop a same-user child from reading files, using the network or using
  ambient credentials. A real agent (Issue #22) requires an execution-permission
  boundary that has not been established yet.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from tactus.runtime.descriptor import (
    RuntimeDescriptor,
    RuntimeProvenance,
    preflight,
)
from tactus.runtime.workspace import (
    Workspace,
    WorkspaceScopeError,
    normalize_changed_path,
    patch_header_paths,
    validate_changed_paths,
)

__all__ = [
    "ATTEMPT_FILE",
    "RECOVERY_LOG",
    "RecoveryReport",
    "RuntimeContractError",
    "RuntimeEvidence",
    "RuntimeLimits",
    "RuntimeRecoveryRequired",
    "RuntimeRequest",
    "RuntimeSupervisionError",
    "RuntimeSupervisor",
    "TerminationReport",
    "WriterLock",
    "WriterLockError",
    "terminate_process_group",
]

#: The agent contract: it reads the request JSON and writes ``result.json``
#: (plus ``patch.diff`` when it reports ``completed``) into ``output_dir``.
RESULT_FILE = "result.json"
PATCH_FILE = "patch.diff"
REQUEST_FILE = "request.json"
ATTEMPT_FILE = "attempt.json"
LOCK_FILE = "writer.lock"
RECOVERY_LOG = "recovery.jsonl"
ATTEMPTS_DIR = "attempts"
SUPPORTED_RESULT_SCHEMA = 1

#: Durable record phases.
PHASE_SPAWNING = "spawning"          # recorded before the child exists
PHASE_RUNNING = "running"            # pid/pgid/start marker recorded
PHASE_SETTLED = "settled"            # terminal: process gone, evidence written
PHASE_UNTERMINATED = "unterminated"  # termination could not be confirmed
PHASE_UNRESOLVED = "unresolved"      # identity unknown / recovery impossible
PHASE_RECOVERED = "recovered"        # a previous leftover was settled
PHASE_RESOLVED = "resolved"          # operator acknowledged an unresolved attempt

_SETTLED_PHASES = frozenset({PHASE_SETTLED, PHASE_RECOVERED, PHASE_RESOLVED})
_BLOCKING_PHASES = frozenset({PHASE_UNTERMINATED, PHASE_UNRESOLVED, PHASE_SPAWNING})


class RuntimeSupervisionError(RuntimeError):
    """A runtime attempt could not be supervised safely."""


class WriterLockError(RuntimeSupervisionError):
    """The writer lock cannot be taken or must not be released."""


class RuntimeContractError(RuntimeSupervisionError):
    """The agent's result artifacts violate the declared contract."""


class RuntimeRecoveryRequired(RuntimeSupervisionError):
    """A previous attempt is unresolved; no new writer may proceed."""

    def __init__(self, message: str, *, record_path: Path, phase: str, attempt_id: str) -> None:
        super().__init__(message)
        self.record_path = record_path
        self.phase = phase
        self.attempt_id = attempt_id


@dataclass(frozen=True, slots=True)
class RuntimeLimits:
    """Bounds applied to one attempt."""

    timeout_seconds: float = 300.0
    max_output_bytes: int = 4096
    max_patch_bytes: int = 200_000
    max_result_bytes: int = 65_536
    max_changed_paths: int = 20
    termination_grace_seconds: float = 5.0
    poll_interval_seconds: float = 0.02

    def __post_init__(self) -> None:
        for name in ("timeout_seconds", "termination_grace_seconds", "poll_interval_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in (
            "max_output_bytes",
            "max_patch_bytes",
            "max_result_bytes",
            "max_changed_paths",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")

    def as_dict(self) -> dict[str, Any]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "max_patch_bytes": self.max_patch_bytes,
            "max_result_bytes": self.max_result_bytes,
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


def process_start_marker(pid: int) -> str | None:
    """Return an OS start-time marker for ``pid`` (identity check for recovery).

    Used to decide whether a recorded pgid still names *our* attempt or has been
    recycled by an unrelated process. Returns ``None`` when identity cannot be
    verified — callers must then fail closed rather than signal.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5.0,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    marker = (completed.stdout or "").strip()
    return marker or None


class WriterLock:
    """Exclusive writer lock for one workspace state directory.

    Two release conditions must both hold: process termination is confirmed, and
    the durable record for the attempt has been written. Otherwise ``release()``
    raises and the lock stays held — an unknown-live agent, or a workspace whose
    outcome is not yet recorded, must not be handed to the next writer.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._handle = None
        self._termination_confirmed = False
        self._bookkeeping_complete = False

    @property
    def held(self) -> bool:
        return self._handle is not None

    @property
    def termination_confirmed(self) -> bool:
        return self._termination_confirmed

    @property
    def bookkeeping_complete(self) -> bool:
        return self._bookkeeping_complete

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

    def mark_settled(self) -> None:
        """Record that the durable outcome is written (validation complete)."""
        self._bookkeeping_complete = True

    def abandon(self) -> None:
        """Release without preconditions — only when the attempt mutated nothing.

        Used when a new attempt is refused *before* touching any state (e.g. a
        previous attempt is unresolved): there is nothing to protect, and holding
        the lock would only block the operator's own recovery.
        """
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None

    def release(self) -> None:
        if self._handle is None:
            return
        if not self._termination_confirmed:
            raise WriterLockError(
                "refusing to release the writer lock while process termination is "
                "unconfirmed"
            )
        if not self._bookkeeping_complete:
            raise WriterLockError(
                "refusing to release the writer lock before the attempt record is written"
            )
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """What settling a previous attempt found and did."""

    found: bool
    attempt_id: str = ""
    previous_phase: str = ""
    phase: str = ""
    pgid: int | None = None
    signalled: bool = False
    reused_pgid: bool = False
    termination: TerminationReport | None = None
    requires_operator: bool = False
    reason: str = ""

    @property
    def recovered(self) -> bool:
        return self.phase in {PHASE_RECOVERED, PHASE_RESOLVED}

    def as_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "attempt_id": self.attempt_id,
            "previous_phase": self.previous_phase,
            "phase": self.phase,
            "pgid": self.pgid,
            "signalled": self.signalled,
            "reused_pgid": self.reused_pgid,
            "termination": None if self.termination is None else self.termination.as_dict(),
            "recovered": self.recovered,
            "requires_operator": self.requires_operator,
            "reason": self.reason,
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
    attempt_dir: str
    pid: int | None
    pgid: int | None
    exit_code: int | None
    timed_out: bool
    cancelled: bool
    cancel_reason: str
    termination: TerminationReport
    writer_lock: Mapping[str, Any]
    recovery: Mapping[str, Any]
    stdout: str
    stderr: str
    output_truncated: bool
    patch_sha256: str
    patch_bytes: int
    declared_paths: tuple[str, ...]
    patch_paths: tuple[str, ...]
    filesystem: Mapping[str, Any]
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
            "attempt_dir": self.attempt_dir,
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
            "recovery": dict(self.recovery),
            "output": {
                "stdout": self.stdout,
                "stderr": self.stderr,
                "truncated": self.output_truncated,
            },
            "artifacts": {
                "agent_status": self.agent_status,
                "agent_summary": self.agent_summary,
                "declared_paths": list(self.declared_paths),
                "patch_paths": list(self.patch_paths),
                "patch_sha256": self.patch_sha256,
                "patch_bytes": self.patch_bytes,
                "filesystem": dict(self.filesystem),
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


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return cleaned[:48] or "attempt"


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
        workspace_paths: Callable[[Path], Sequence[str]] | None = None,
        start_marker: Callable[[int], str | None] | None = None,
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
        self._workspace_paths = workspace_paths
        self._start_marker = start_marker or process_start_marker
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

    @property
    def limits(self) -> RuntimeLimits:
        return self._limits

    @property
    def attempt_record_path(self) -> Path:
        return self._state_dir / ATTEMPT_FILE

    # -- lifecycle -------------------------------------------------------

    def preflight(self) -> RuntimeProvenance:
        return preflight(self._descriptor)

    def cancel(self, reason: str = "operator cancelled the attempt") -> None:
        """Request cancellation; the run loop terminates the process group."""
        self._cancel_reason = reason
        self._cancel.set()

    def run(self, request: RuntimeRequest) -> RuntimeEvidence:
        """Supervise one attempt under exclusive ownership of the workspace."""
        if not isinstance(request, RuntimeRequest):
            raise RuntimeSupervisionError("a RuntimeRequest is required")
        self._state_dir.mkdir(parents=True, exist_ok=True)
        provenance = self.preflight()

        # Ownership before any mutation: no file is created, changed or removed
        # until this succeeds, so a concurrent attempt cannot touch our state.
        lock = WriterLock(self._state_dir / LOCK_FILE).acquire()
        record_owned = False
        settled = False
        termination = TerminationReport(confirmed=False)
        lock_state: dict[str, Any] = {"acquired": True, "released": False, "refused": ""}
        recovery: dict[str, Any] = {"found": False}
        started_at = _now()
        stopwatch = time.monotonic()
        process: subprocess.Popen[bytes] | None = None
        pid: int | None = None
        pgid: int | None = None
        timed_out = False
        cancelled = False
        stdout_reader: _BoundedReader | None = None
        stderr_reader: _BoundedReader | None = None
        exit_code: int | None = None
        try:
            # A previous attempt must be settled (or explicitly block us) while
            # we still own the lock and before we write anything of our own.
            previous = self._settle_previous()
            recovery = previous.as_dict()
            if previous.requires_operator:
                lock.abandon()
                lock_state["abandoned"] = True
                raise RuntimeRecoveryRequired(
                    f"previous attempt {previous.attempt_id!r} is {previous.phase!r}: "
                    f"{previous.reason}. A new writer may not proceed until it is "
                    "resolved.",
                    record_path=self.attempt_record_path,
                    phase=previous.phase,
                    attempt_id=previous.attempt_id,
                )

            attempt_dir, output_dir = self._create_attempt_dir(request.attempt_id)
            record_owned = True
            request_path = attempt_dir / REQUEST_FILE
            _write_json(
                request_path,
                {
                    **request.as_dict(),
                    "workspace": self._workspace.as_dict(),
                    "output_dir": str(output_dir),
                    "attempt_dir": str(attempt_dir),
                    "limits": self._limits.as_dict(),
                },
            )
            _write_json(
                self.attempt_record_path,
                {
                    "attempt_id": request.attempt_id,
                    "phase": PHASE_SPAWNING,
                    "attempt_dir": str(attempt_dir),
                    "workspace": str(self._workspace.root),
                    "started_at": started_at.isoformat(),
                    "pid": None,
                    "pgid": None,
                },
            )
            try:
                process = subprocess.Popen(
                    self._descriptor.command(request_path),
                    cwd=str(self._workspace.root),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=_child_environment(request),
                    start_new_session=True,
                )
            except OSError as exc:
                # No child exists: termination is trivially confirmed, and the
                # attempt still gets a durable record before the lock is released.
                termination = TerminationReport(confirmed=True)
                lock.confirm_termination()
                errors = (f"runtime could not be started: {exc}",)
                evidence = self._finish(
                    request=request,
                    provenance=provenance,
                    started_at=started_at,
                    stopwatch=stopwatch,
                    attempt_dir=attempt_dir,
                    pid=None,
                    pgid=None,
                    exit_code=None,
                    timed_out=False,
                    cancelled=False,
                    termination=termination,
                    lock_state=lock_state,
                    recovery=recovery,
                    stdout="",
                    stderr="",
                    truncated=False,
                    agent_status="missing",
                    agent_summary="",
                    declared=(),
                    patch_paths=(),
                    filesystem={"verified": False, "reason": "process did not start"},
                    patch_sha256="",
                    patch_bytes=0,
                    errors=errors,
                    status="failed",
                )
                self._write_terminal(evidence)
                settled = True
                lock.mark_settled()
                lock.release()
                lock_state["released"] = True
                return self._replace_lock_state(evidence, lock_state)

            pid = process.pid
            pgid = process.pid  # start_new_session == new process group leader
            _write_json(
                self.attempt_record_path,
                {
                    "attempt_id": request.attempt_id,
                    "phase": PHASE_RUNNING,
                    "attempt_dir": str(attempt_dir),
                    "workspace": str(self._workspace.root),
                    "started_at": started_at.isoformat(),
                    "pid": pid,
                    "pgid": pgid,
                    "start_marker": self._start_marker(pid),
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
            # Only wait for the streams of a process we know is gone: an
            # unconfirmed termination must not block the supervisor on a pipe
            # that a live child still owns.
            if termination.confirmed:
                for reader in (stdout_reader, stderr_reader):
                    if reader is not None:
                        reader.join(timeout=5.0)

        assert process is not None  # a spawn failure returned above
        exit_code = process.returncode
        stdout = stdout_reader.text() if stdout_reader else ""
        stderr = stderr_reader.text() if stderr_reader else ""
        truncated = bool(
            (stdout_reader and stdout_reader.truncated)
            or (stderr_reader and stderr_reader.truncated)
        )
        (
            agent_status,
            agent_summary,
            declared,
            patch_paths,
            filesystem,
            patch_sha256,
            patch_bytes,
            errors,
            contract_violation,
        ) = self._collect_artifacts(output_dir=attempt_dir / "output", exit_code=exit_code)
        status = _status_for(
            exit_code=exit_code,
            timed_out=timed_out,
            cancelled=cancelled,
            termination=termination,
            contract_violation=contract_violation,
            agent_status=agent_status,
        )
        evidence = self._finish(
            request=request,
            provenance=provenance,
            started_at=started_at,
            stopwatch=stopwatch,
            attempt_dir=attempt_dir,
            pid=pid,
            pgid=pgid,
            exit_code=exit_code,
            timed_out=timed_out,
            cancelled=cancelled,
            termination=termination,
            lock_state=lock_state,
            recovery=recovery,
            stdout=stdout,
            stderr=stderr,
            truncated=truncated,
            agent_status=agent_status,
            agent_summary=agent_summary,
            declared=declared,
            patch_paths=patch_paths,
            filesystem=filesystem,
            patch_sha256=patch_sha256,
            patch_bytes=patch_bytes,
            errors=errors,
            status=status,
        )
        # Durable outcome before release: a failed validation still records first.
        self._write_terminal(evidence)
        settled = True
        if termination.confirmed:
            lock.mark_settled()
            try:
                lock.release()
                lock_state["released"] = True
            except WriterLockError as exc:  # pragma: no cover - defensive
                lock_state["refused"] = str(exc)
        return self._replace_lock_state(evidence, lock_state)

    # -- recovery --------------------------------------------------------

    def recover(self) -> RecoveryReport:
        """Settle a previous attempt, fail-closed on unknown identity."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        return self._settle_previous()

    def resolve_unresolved(self, *, operator: str, reason: str) -> RecoveryReport:
        """Operator-only: acknowledge an unresolved attempt so writers may proceed.

        Nothing is terminated here. This is an explicit human decision recorded in
        the audit log and in the attempt record; it is the only way past
        ``unresolved``/``unterminated`` short of a verified recovery.
        """
        if not operator.strip() or not reason.strip():
            raise RuntimeSupervisionError("operator and reason are required")
        record = self._read_record()
        if record is None:
            return RecoveryReport(found=False, reason="no attempt record")
        phase = str(record.get("phase", ""))
        if phase not in _BLOCKING_PHASES:
            return RecoveryReport(
                found=True,
                attempt_id=str(record.get("attempt_id", "")),
                previous_phase=phase,
                phase=phase,
                reason="nothing to resolve",
            )
        updated = {
            **record,
            "phase": PHASE_RESOLVED,
            "resolved_at": _now().isoformat(),
            "resolved_by": operator,
            "resolution_reason": reason,
        }
        _write_json(self.attempt_record_path, updated)
        self._audit(
            {
                "action": "operator_resolved",
                "attempt_id": updated.get("attempt_id", ""),
                "previous_phase": phase,
                "operator": operator,
                "reason": reason,
            }
        )
        return RecoveryReport(
            found=True,
            attempt_id=str(updated.get("attempt_id", "")),
            previous_phase=phase,
            phase=PHASE_RESOLVED,
            pgid=updated.get("pgid") if isinstance(updated.get("pgid"), int) else None,
            reason=reason,
        )

    def _read_record(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.attempt_record_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _settle_previous(self) -> RecoveryReport:
        record = self._read_record()
        if record is None:
            return RecoveryReport(found=False, phase="none")
        phase = str(record.get("phase", ""))
        attempt_id = str(record.get("attempt_id", ""))
        raw_pgid = record.get("pgid")
        pgid = raw_pgid if isinstance(raw_pgid, int) and raw_pgid > 0 else None

        if phase in _SETTLED_PHASES:
            return RecoveryReport(found=True, attempt_id=attempt_id, phase=phase)

        if pgid is None:
            # Unknown process identity (crashed while spawning, or a record that
            # never carried a pid): never assume termination.
            self._mark(record, PHASE_UNRESOLVED, reason="process identity unknown")
            self._audit(
                {
                    "action": "unresolved_unknown_identity",
                    "attempt_id": attempt_id,
                    "previous_phase": phase,
                }
            )
            return RecoveryReport(
                found=True,
                attempt_id=attempt_id,
                previous_phase=phase,
                phase=PHASE_UNRESOLVED,
                requires_operator=True,
                reason="process identity unknown",
            )

        state = _group_state(pgid)
        if state != "alive":
            self._mark(record, PHASE_RECOVERED, reason=f"group already {state}")
            self._audit(
                {
                    "action": "recovered_group_absent",
                    "attempt_id": attempt_id,
                    "pgid": pgid,
                    "group_state": state,
                }
            )
            return RecoveryReport(
                found=True,
                attempt_id=attempt_id,
                previous_phase=phase,
                phase=PHASE_RECOVERED,
                pgid=pgid,
                reason=f"group already {state}",
            )

        recorded_marker = str(record.get("start_marker") or "")
        live_marker = self._start_marker(pgid)
        if live_marker is None:
            self._mark(record, PHASE_UNRESOLVED, reason="process identity unverifiable")
            self._audit(
                {
                    "action": "unresolved_identity_unverifiable",
                    "attempt_id": attempt_id,
                    "pgid": pgid,
                }
            )
            return RecoveryReport(
                found=True,
                attempt_id=attempt_id,
                previous_phase=phase,
                phase=PHASE_UNRESOLVED,
                pgid=pgid,
                requires_operator=True,
                reason="process identity unverifiable",
            )
        if recorded_marker != live_marker:
            # The recorded pgid now names a different process: signalling it
            # would kill an unrelated group. Our attempt is gone.
            self._mark(
                record,
                PHASE_RECOVERED,
                reason="pgid reused by an unrelated process",
                extra={"pgid_reused": True},
            )
            self._audit(
                {
                    "action": "recovered_pgid_reused",
                    "attempt_id": attempt_id,
                    "pgid": pgid,
                }
            )
            return RecoveryReport(
                found=True,
                attempt_id=attempt_id,
                previous_phase=phase,
                phase=PHASE_RECOVERED,
                pgid=pgid,
                reused_pgid=True,
                reason="pgid reused by an unrelated process",
            )

        termination = self._terminate(pgid, None, self._limits.termination_grace_seconds)
        new_phase = PHASE_RECOVERED if termination.confirmed else PHASE_UNRESOLVED
        self._mark(
            record,
            new_phase,
            reason=(
                "leftover group terminated"
                if termination.confirmed
                else "leftover group did not terminate"
            ),
            extra={"termination_confirmed": termination.confirmed},
        )
        self._audit(
            {
                "action": "recovered_terminated" if termination.confirmed else "recovery_failed",
                "attempt_id": attempt_id,
                "pgid": pgid,
                "termination": termination.as_dict(),
            }
        )
        return RecoveryReport(
            found=True,
            attempt_id=attempt_id,
            previous_phase=phase,
            phase=new_phase,
            pgid=pgid,
            signalled=True,
            termination=termination,
            requires_operator=not termination.confirmed,
            reason=(
                "leftover group terminated"
                if termination.confirmed
                else "leftover group did not terminate"
            ),
        )

    def _mark(
        self,
        record: Mapping[str, Any],
        phase: str,
        *,
        reason: str,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        _write_json(
            self.attempt_record_path,
            {
                **dict(record),
                "phase": phase,
                "recovery_reason": reason,
                "recovered_at": _now().isoformat(),
                **(dict(extra) if extra else {}),
            },
        )

    def _audit(self, entry: Mapping[str, Any]) -> None:
        payload = {"at": _now().isoformat(), **dict(entry)}
        path = self._state_dir / RECOVERY_LOG
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    # -- attempt state ---------------------------------------------------

    def _create_attempt_dir(self, attempt_id: str) -> tuple[Path, Path]:
        """Create a unique, never-reused directory for this attempt's evidence."""
        base = self._state_dir / ATTEMPTS_DIR
        base.mkdir(parents=True, exist_ok=True)
        attempt_dir = base / f"{_slug(attempt_id)}-{uuid.uuid4().hex[:12]}"
        output_dir = attempt_dir / "output"
        output_dir.mkdir(parents=True)
        return attempt_dir, output_dir

    def _write_terminal(self, evidence: RuntimeEvidence) -> None:
        _write_json(
            self.attempt_record_path,
            {
                "attempt_id": evidence.request.attempt_id,
                "phase": (
                    PHASE_SETTLED
                    if evidence.termination.confirmed
                    else PHASE_UNTERMINATED
                ),
                "attempt_dir": evidence.attempt_dir,
                "workspace": str(self._workspace.root),
                "started_at": evidence.started_at.isoformat(),
                "finished_at": evidence.finished_at.isoformat(),
                "pid": evidence.pid,
                "pgid": evidence.pgid,
                "status": evidence.status,
                "termination_confirmed": evidence.termination.confirmed,
                "validation_errors": list(evidence.errors),
            },
        )

    # -- artifact contract ----------------------------------------------

    def _collect_artifacts(
        self, *, output_dir: Path, exit_code: int | None
    ) -> tuple[
        str,
        str,
        tuple[str, ...],
        tuple[str, ...],
        dict[str, Any],
        str,
        int,
        tuple[str, ...],
        bool,
    ]:
        """Return artifacts plus ``(errors, contract_violation)``.

        An **absent** result is a failed run. A result or patch that breaks the
        declared contract is a violation — that distinction drives ``failed``
        versus ``invalid``. Both files are read with a hard byte cap: nothing is
        loaded and then measured.
        """
        result_path = output_dir / RESULT_FILE
        if not result_path.is_file():
            return (
                "missing",
                "",
                (),
                (),
                {"verified": False, "reason": "no result"},
                "",
                0,
                ("agent produced no result.json",),
                False,
            )
        try:
            raw = _read_bounded(result_path, self._limits.max_result_bytes)
        except RuntimeContractError as exc:
            return ("invalid", "", (), (), {"verified": False, "reason": str(exc)}, "", 0, (str(exc),), True)
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return (
                "invalid",
                "",
                (),
                (),
                {"verified": False, "reason": "unparsable result"},
                "",
                0,
                (f"result.json is not valid JSON: {exc}",),
                True,
            )
        if not isinstance(result, Mapping):
            return (
                "invalid",
                "",
                (),
                (),
                {"verified": False, "reason": "result not an object"},
                "",
                0,
                ("result.json must be a JSON object",),
                True,
            )
        if result.get("schema_version") != SUPPORTED_RESULT_SCHEMA:
            return (
                "invalid",
                "",
                (),
                (),
                {"verified": False, "reason": "unsupported schema"},
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
                (),
                {"verified": False, "reason": "bad status"},
                "",
                0,
                (f"result.json status must be 'completed' or 'failed', got {agent_status!r}",),
                True,
            )

        errors: list[str] = []
        declared: tuple[str, ...] = ()
        if agent_status == "completed":
            try:
                declared = validate_changed_paths(
                    result.get("changed_paths"),
                    workspace=self._workspace,
                    max_paths=self._limits.max_changed_paths,
                )
            except WorkspaceScopeError as exc:
                errors.append(str(exc))

        patch_paths: tuple[str, ...] = ()
        patch_sha256 = ""
        patch_bytes = 0
        if agent_status == "completed":
            patch_path = output_dir / PATCH_FILE
            if not patch_path.is_file():
                errors.append("a completed run must produce patch.diff")
            else:
                try:
                    declared_size = patch_path.stat().st_size
                    if declared_size > self._limits.max_patch_bytes:
                        raise RuntimeContractError(
                            f"patch is {declared_size} bytes; limit is "
                            f"{self._limits.max_patch_bytes}"
                        )
                    raw_patch = _read_bounded(patch_path, self._limits.max_patch_bytes)
                except (RuntimeContractError, OSError) as exc:
                    errors.append(str(exc))
                else:
                    patch_bytes = len(raw_patch)
                    patch_sha256 = hashlib.sha256(raw_patch).hexdigest()
                    try:
                        text = raw_patch.decode("utf-8")
                    except UnicodeDecodeError:
                        errors.append("patch.diff must be UTF-8 text")
                        text = ""
                    patch_paths = patch_header_paths(text)
                    if text and not patch_paths:
                        errors.append("patch.diff names no file headers")
                    undeclared = [path for path in patch_paths if path not in declared]
                    if undeclared:
                        errors.append(
                            "patch.diff touches paths that were not declared: "
                            + ", ".join(undeclared)
                        )
                if exit_code not in (0, None):
                    errors.append("agent exited non-zero but reported completion")

        filesystem = self._verify_filesystem(declared)
        if filesystem.get("mismatches"):
            errors.append(
                "workspace changes do not match the declared paths: "
                + ", ".join(filesystem["mismatches"])
            )

        return (
            agent_status,
            summary,
            declared,
            patch_paths,
            filesystem,
            patch_sha256,
            patch_bytes,
            tuple(errors),
            bool(errors),
        )

    def _verify_filesystem(self, declared: tuple[str, ...]) -> dict[str, Any]:
        """Compare declared paths with the workspace's actual changes, if known.

        Without a ``workspace_paths`` provider this is explicitly reported as
        *not verified*: scope validation is post-execution detection, and it only
        sees what the agent and the diff say unless a workspace diff is supplied.
        """
        if self._workspace_paths is None:
            return {
                "verified": False,
                "reason": "no workspace diff provider configured",
                "paths": [],
                "mismatches": [],
            }
        try:
            paths = tuple(
                normalize_changed_path(item) for item in self._workspace_paths(self._workspace.root)
            )
        except (WorkspaceScopeError, OSError) as exc:
            return {
                "verified": False,
                "reason": f"workspace diff unavailable: {exc}",
                "paths": [],
                "mismatches": [],
            }
        mismatches = sorted(set(paths) ^ set(declared))
        return {
            "verified": True,
            "reason": "",
            "paths": list(paths),
            "mismatches": mismatches,
        }

    def _finish(
        self,
        *,
        request: RuntimeRequest,
        provenance: RuntimeProvenance,
        started_at: datetime,
        stopwatch: float,
        attempt_dir: Path,
        pid: int | None,
        pgid: int | None,
        exit_code: int | None,
        timed_out: bool,
        cancelled: bool,
        termination: TerminationReport,
        lock_state: dict[str, Any],
        recovery: Mapping[str, Any],
        stdout: str,
        stderr: str,
        truncated: bool,
        agent_status: str,
        agent_summary: str,
        declared: tuple[str, ...],
        patch_paths: tuple[str, ...],
        filesystem: Mapping[str, Any],
        patch_sha256: str,
        patch_bytes: int,
        errors: tuple[str, ...],
        status: str,
    ) -> RuntimeEvidence:
        return RuntimeEvidence(
            status=status,
            request=request,
            provenance=provenance,
            workspace=self._workspace,
            limits=self._limits,
            started_at=started_at,
            finished_at=_now(),
            duration_ms=round((time.monotonic() - stopwatch) * 1000, 3),
            attempt_dir=str(attempt_dir),
            pid=pid,
            pgid=pgid,
            exit_code=exit_code,
            timed_out=timed_out,
            cancelled=cancelled,
            cancel_reason=self._cancel_reason,
            termination=termination,
            writer_lock=dict(lock_state),
            recovery=dict(recovery),
            stdout=stdout,
            stderr=stderr,
            output_truncated=truncated,
            patch_sha256=patch_sha256,
            patch_bytes=patch_bytes,
            declared_paths=declared,
            patch_paths=patch_paths,
            filesystem=dict(filesystem),
            agent_status=agent_status,
            agent_summary=agent_summary,
            errors=errors,
        )

    @staticmethod
    def _replace_lock_state(
        evidence: RuntimeEvidence, lock_state: Mapping[str, Any]
    ) -> RuntimeEvidence:
        return replace(evidence, writer_lock=dict(lock_state))


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


def _read_bounded(path: Path, limit: int) -> bytes:
    """Read at most ``limit`` bytes + 1, so an oversized file is never loaded."""
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise RuntimeContractError(f"{path.name} exceeds the {limit}-byte limit")
    return data


def _child_environment(request: RuntimeRequest) -> dict[str, str]:
    """Minimal child environment: no credentials, no inherited configuration.

    Publication credentials are deliberately absent — an agent that may edit
    files has no publication authority (Issue #22). This is hygiene, not an
    isolation boundary: the child can still read whatever the OS account can,
    including ambient network access and any credential on disk.
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
