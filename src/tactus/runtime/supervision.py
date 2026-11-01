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

Containment (opt-in, Issue #22)
------------------------------
When the descriptor pins a launcher, this supervisor becomes an *enforcement*
port as well: it renders the containment profile into the attempt directory,
proves the boundary with real children (denial matrix) before any agent process
exists, launches the runtime through the pinned launcher, and refuses to release
the writer lock while any process still carries the attempt marker. A failed
self-test starts nothing.

Not claimed
-----------
* Without a pinned launcher this is **not a sandbox**: same OS account, same
  filesystem and network reach, and write scope is *detected and rejected after
  the fact*, never prevented. With one, writes are an allowlist and reads are a
  deny-list — the latter is weaker than an allowlist and is stated as such.
* Git-worktree isolation (Issue #23) is a separate boundary and does not stop a
  same-user child from reading files, using the network or using ambient
  credentials on its own.
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

from tactus.runtime.containment import (
    ContainmentError,
    ContainmentPolicy,
    residual_processes,
    run_self_test,
    sweep_is_visible,
)
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
PROMPT_FILE = "prompt.txt"
CONTAINMENT_PROFILE = "containment.sb"
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
    #: The task the agent must perform. Written to the prompt file; never argv,
    #: which would expose it in ``ps``. Empty is valid for a runtime that reads
    #: no prompt, and refused when the descriptor feeds stdin from one.
    task: str = ""

    def __post_init__(self) -> None:
        for name in ("attempt_id", "intent_id", "capability"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"runtime request {name} must be a non-empty string")
        if not isinstance(self.task, str):
            raise ValueError("runtime request task must be a string")

    def as_dict(self) -> dict[str, str]:
        return {
            "attempt_id": self.attempt_id,
            "intent_id": self.intent_id,
            "capability": self.capability,
            "task": self.task,
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
    #: What the boundary observed: policy summary, profile digest, the denial
    #: matrix, and any process that outlived its group.
    containment: Mapping[str, Any] = field(default_factory=dict)

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
            "containment": dict(self.containment),
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
        containment: ContainmentPolicy | None = None,
        credentials: Mapping[str, str] | None = None,
    ) -> None:
        if descriptor.launcher is not None and containment is None:
            # A launcher without a policy would mean launching through a wrapper
            # whose denials nobody stated. Refuse rather than guess.
            raise RuntimeSupervisionError(
                "the descriptor pins a launcher, so a containment policy is required"
            )
        if containment is not None and descriptor.launcher is None:
            raise RuntimeSupervisionError(
                "a containment policy requires the descriptor to pin its launcher"
            )
        if containment is not None and descriptor.launcher is not None:
            # The matrix exercises the policy's launcher; the command uses the
            # descriptor's. If they differ, a green matrix would certify a
            # boundary the run never applies.
            policy_launcher = containment.launcher
            run_launcher = descriptor.launcher
            if (
                Path(policy_launcher.path).resolve() != Path(run_launcher.path).resolve()
                or tuple(policy_launcher.argv_prefix) != tuple(run_launcher.argv_prefix)
                or policy_launcher.sha256 != run_launcher.sha256
            ):
                raise RuntimeSupervisionError(
                    "the containment policy's launcher and the descriptor's launcher "
                    "differ; the run would not apply the boundary that was proven"
                )
        self._descriptor = descriptor
        self._workspace = workspace
        self._containment = containment
        self._credentials = dict(credentials or {})
        self._limits = limits or RuntimeLimits()
        self._state_dir = (
            Path(state_dir).resolve()
            if state_dir is not None
            else (workspace.root / ".tactus-runtime").resolve()
        )
        if containment is not None:
            # Attempt state inside the workspace would be inside the write
            # allowlist: the child could rewrite its own recovery record, and the
            # "write outside the workspace" probe would land in an allowed path.
            state_root = Path(self._state_dir)
            if state_root == workspace.root or workspace.root in state_root.parents:
                raise RuntimeSupervisionError(
                    f"the state directory {state_root} is inside the workspace "
                    f"{workspace.root}; a contained attempt needs its state outside "
                    "the write allowlist"
                )
        self._terminate = terminate or terminate_process_group
        self._workspace_paths = workspace_paths
        self._start_marker = start_marker or process_start_marker
        self._cancel = threading.Event()
        self._cancel_reason = ""
        self._sweep_visible: tuple[bool, str] | None = None
        self._base_revision: str | None = None
        self._sweep_detail = ""

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
    def containment(self) -> ContainmentPolicy | None:
        return self._containment

    def _sweep(self, marker: str) -> tuple[Any, ...] | None:
        """Processes still carrying the attempt marker; None if unverifiable.

        An empty result is only meaningful when this host's process table can show
        a marker at all: ``ps -E`` does not expose the environment of every binary,
        so "nothing found" from a blind sweep must never be read as "nothing alive".
        """
        visible, detail = self._sweep_visibility()
        if not visible:
            self._sweep_detail = detail
            return None
        try:
            return tuple(residual_processes(marker))
        except ContainmentError as exc:
            self._sweep_detail = str(exc)
            return None

    def _sweep_visibility(self) -> tuple[bool, str]:
        """Whether a planted marker is visible to the sweep (measured once)."""
        if self._sweep_visible is None:
            self._sweep_visible = sweep_is_visible()
        return self._sweep_visible

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
            attempt_marker = attempt_dir.name
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
            containment_evidence: dict[str, Any] = {
                "enabled": False,
                "reason": "no launcher pinned in the descriptor",
                "attempt_marker": attempt_marker,
            }

            def finish_without_child(
                *, status: str, errors: tuple[str, ...], reason: str
            ) -> RuntimeEvidence:
                """Record a pre-spawn failure: no child existed, so termination holds.

                Nothing was launched, so the writer lock is released: the workspace
                is provably free. The durable record carries the reason, which is
                what an operator needs — holding the lock would only block them.
                """
                nonlocal termination, settled
                termination = TerminationReport(confirmed=True)
                lock.confirm_termination()
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
                    filesystem={"verified": False, "reason": reason},
                    patch_sha256="",
                    patch_bytes=0,
                    errors=errors,
                    status=status,
                    containment=containment_evidence,
                )
                self._write_terminal(evidence)
                settled = True
                lock.mark_settled()
                lock.release()
                lock_state["released"] = True
                return self._replace_lock_state(evidence, lock_state)

            # The task is data on disk, never argv: an argv is visible in ``ps``.
            prompt_path: Path | None = None
            if self._descriptor.stdin_from == "prompt":
                if not request.task.strip():
                    return finish_without_child(
                        status="invalid",
                        errors=(
                            "the runtime reads its task from stdin, but the request "
                            "carries no task",
                        ),
                        reason="no task to send",
                    )
                prompt_path = attempt_dir / PROMPT_FILE
                prompt_path.write_text(request.task, encoding="utf-8")

            # Containment is proven before any agent process exists. A boundary
            # that cannot be proven starts nothing.
            profile_path: Path | None = None
            if self._containment is not None:
                try:
                    self._containment.validate_workspace(self._workspace.root)
                    self._containment.validate_readable(
                        Path(self._descriptor.executable), what="runtime"
                    )
                    profile_path = self._containment.write_profile(
                        attempt_dir / CONTAINMENT_PROFILE,
                        workspace=self._workspace.root,
                        attempt_dir=attempt_dir,
                    )
                    self_test = run_self_test(
                        self._containment,
                        workspace=self._workspace.root,
                        attempt_dir=attempt_dir,
                        runtime_probe_argv=self._descriptor.preflight_argv,
                        expected_version_token=self._descriptor.expected_version_token,
                    )
                except (ContainmentError, OSError) as exc:
                    containment_evidence = {
                        "enabled": True,
                        "attempt_marker": attempt_marker,
                        "policy": self._containment.as_dict(),
                        "self_test": None,
                        "error": str(exc),
                    }
                    return finish_without_child(
                        status="invalid",
                        errors=(f"containment could not be established: {exc}",),
                        reason="containment unavailable",
                    )
                containment_evidence = {
                    "enabled": True,
                    "attempt_marker": attempt_marker,
                    "policy": self._containment.as_dict(),
                    "profile_path": str(profile_path),
                    "profile_sha256": self_test.profile_sha256,
                    "self_test": self_test.as_dict(),
                }
                if not self_test.ok:
                    return finish_without_child(
                        status="invalid",
                        errors=(
                            "containment self-test did not hold: "
                            + ", ".join(
                                check.name
                                for check in self_test.checks
                                if check.required and not check.ok
                            ),
                        ),
                        reason="containment self-test failed",
                    )

            # The baseline for a workspace-diff account must be taken *before* the
            # child runs: a runtime may commit its own work, and a diff against the
            # post-run HEAD would show nothing.
            if self._descriptor.result_contract == "workspace-diff":
                self._workspace_base()

            stdin_handle = (
                prompt_path.open("rb") if prompt_path is not None else subprocess.DEVNULL
            )
            try:
                process = subprocess.Popen(
                    self._descriptor.command(
                        request_path,
                        prompt_path=prompt_path,
                        profile_path=profile_path,
                        workspace=self._workspace.root,
                    ),
                    cwd=str(self._workspace.root),
                    stdin=stdin_handle,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=_child_environment(
                        request,
                        attempt_dir=attempt_dir,
                        allowlist=self._descriptor.env_allowlist,
                        credentials=self._credentials,
                        attempt_marker=attempt_marker,
                    ),
                    start_new_session=True,
                )
            except OSError as exc:
                return finish_without_child(
                    status="failed",
                    errors=(f"runtime could not be started: {exc}",),
                    reason="process did not start",
                )
            finally:
                if prompt_path is not None:
                    stdin_handle.close()  # type: ignore[union-attr]

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
                    "attempt_marker": attempt_marker,
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
                    # A group can be gone while a detached helper still runs.
                    # Termination stays unconfirmed until nothing carries the
                    # attempt marker, so the writer lock cannot be handed over
                    # while any part of the attempt is still alive.
                    survivors = self._sweep(attempt_marker)
                    if survivors is None:
                        containment_evidence["sweep"] = "unverifiable"
                        termination = replace(termination, confirmed=False)
                    elif survivors:
                        containment_evidence["survivors"] = [
                            item.as_dict() for item in survivors
                        ]
                        termination = replace(termination, confirmed=False, alive_after=True)
                    else:
                        containment_evidence["sweep"] = "clear"
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
            containment=containment_evidence,
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

        # A process that still carries the attempt marker means the attempt is not
        # over, whatever the recorded group says: a detached helper can outlive
        # its group. That is never "recovered" — it needs an operator.
        recorded_attempt_marker = str(record.get("attempt_marker") or "")
        if recorded_attempt_marker:
            survivors = self._sweep(recorded_attempt_marker)
            if survivors is None:
                self._mark(
                    record,
                    PHASE_UNRESOLVED,
                    reason="survivor sweep unverifiable",
                )
                self._audit(
                    {
                        "action": "unresolved_sweep_unverifiable",
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
                    reason="survivor sweep unverifiable",
                )
            if survivors:
                self._mark(
                    record,
                    PHASE_UNRESOLVED,
                    reason="processes still carry the attempt marker",
                    extra={"survivors": [item.as_dict() for item in survivors]},
                )
                self._audit(
                    {
                        "action": "unresolved_survivors",
                        "attempt_id": attempt_id,
                        "previous_phase": phase,
                        "survivors": [item.as_dict() for item in survivors],
                    }
                )
                return RecoveryReport(
                    found=True,
                    attempt_id=attempt_id,
                    previous_phase=phase,
                    phase=PHASE_UNRESOLVED,
                    requires_operator=True,
                    reason="processes still carry the attempt marker",
                )

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
                # The marker must survive into the terminal record: it is how a
                # later attempt proves that nothing of this one still runs.
                "attempt_marker": str(evidence.containment.get("attempt_marker", "")),
                "survivors": list(evidence.containment.get("survivors") or []),
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

        With ``result_contract='workspace-diff'`` the runtime is expected to write
        nothing in that shape: the changed paths and the patch are taken from the
        workspace, so a real coding agent is accounted for by what it actually
        changed instead of by an artifact format it does not speak.
        """
        if self._descriptor.result_contract == "workspace-diff":
            return self._collect_from_workspace_diff(
                output_dir=output_dir, exit_code=exit_code
            )

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
            # A verified diff that disagrees with the declaration is not a
            # reporting nit: the agent's account of what it changed is wrong.
            contract_violation = True
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

    #: The revision the workspace was at before the attempt spawned. A runtime may
    #: commit its own work, so the diff is taken against this, not against HEAD.
    EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

    def _git(self, *args: str, timeout: float = 120.0) -> bytes:
        try:
            completed = subprocess.run(
                ["git", "-C", str(self._workspace.root), *args],
                capture_output=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeContractError(f"workspace diff unavailable: {exc}") from exc
        if completed.returncode != 0:
            raise RuntimeContractError(
                "workspace diff failed: "
                + (completed.stderr or b"").decode("utf-8", "replace").strip()[:200]
            )
        return completed.stdout or b""

    def _workspace_base(self) -> str:
        """The workspace revision at attempt start (empty tree if no commit yet)."""
        if self._base_revision is not None:
            return self._base_revision
        try:
            self._base_revision = self._git("rev-parse", "HEAD").decode().strip()
        except RuntimeContractError:
            self._base_revision = self.EMPTY_TREE
        return self._base_revision

    def _workspace_diff_snapshot(self) -> tuple[tuple[str, ...], bytes]:
        """Changed paths and patch from one source: the workspace's own git diff.

        Staging first is deliberate. ``git status`` and ``git diff`` disagree about
        staged and untracked files, so an agent that stages its work would report
        paths with no matching patch; taking both from ``diff --cached <base>``
        makes the two halves of the account the same observation.
        """
        base = self._workspace_base()
        self._git("add", "-A")
        names = self._git("diff", "--cached", "--name-only", "-z", base)
        paths = tuple(part for part in names.decode("utf-8", "replace").split("\0") if part)
        patch = self._git("diff", "--cached", "--no-color", "--binary", base)
        if len(patch) > self._limits.max_patch_bytes:
            raise RuntimeContractError(
                f"workspace diff is {len(patch)} bytes; the limit is "
                f"{self._limits.max_patch_bytes}"
            )
        return paths, patch

    def _collect_from_workspace_diff(
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
        """Account for a run whose result is the workspace, not an artifact."""
        errors: list[str] = []
        contract_violation = False
        try:
            observed, raw = self._workspace_diff_snapshot()
        except RuntimeContractError as exc:
            return (
                "failed",
                "",
                (),
                (),
                {"verified": False, "reason": str(exc), "source": "workspace-diff"},
                "",
                0,
                (str(exc),),
                True,
            )
        try:
            declared = validate_changed_paths(
                list(observed),
                workspace=self._workspace,
                max_paths=self._limits.max_changed_paths,
            )
        except WorkspaceScopeError as exc:
            errors.append(f"workspace changes are outside the allowed scope: {exc}")
            contract_violation = True
            declared = tuple()

        patch_bytes = len(raw)
        patch_sha256 = hashlib.sha256(raw).hexdigest() if raw else ""
        patch_paths = patch_header_paths(raw.decode("utf-8", "replace"))
        if patch_paths:
            undeclared = [path for path in patch_paths if path not in declared]
            if undeclared:
                errors.append("patch touches undeclared paths: " + ", ".join(undeclared))
                contract_violation = True
        if declared and not raw:
            errors.append("the workspace shows no diff for the declared paths")
            contract_violation = True
        if raw:
            repaired = output_dir / PATCH_FILE
            repaired.parent.mkdir(parents=True, exist_ok=True)
            repaired.write_bytes(raw)

        filesystem: dict[str, Any] = {
            "verified": True,
            "source": "workspace-diff",
            "paths": list(observed),
            "mismatches": [],
        }
        status = "completed" if exit_code in (0, None) else "failed"
        return (
            status,
            ", ".join(declared),
            declared,
            patch_paths,
            filesystem,
            patch_sha256,
            patch_bytes,
            tuple(errors),
            contract_violation,
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
        containment: Mapping[str, Any] | None = None,
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
            containment=dict(containment or {}),
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


def _child_environment(
    request: RuntimeRequest,
    *,
    attempt_dir: Path | None = None,
    allowlist: Sequence[str] = (),
    credentials: Mapping[str, str] | None = None,
    attempt_marker: str = "",
) -> dict[str, str]:
    """Minimal child environment: no inherited configuration, no credentials.

    Publication credentials are deliberately absent — an agent that may edit
    files has no publication authority (Issue #22). What the child gets is
    ``PATH``, the two attempt identifiers, attempt-local ``HOME``/``TMPDIR``/git
    configuration (when an attempt directory is given), and only those variables
    the descriptor named in its allowlist. Values come from the supervisor, never
    from the ambient environment, so an unrelated secret cannot leak into the run
    and an allowlisted name cannot be inherited.

    This is hygiene, not containment on its own: without an OS boundary the child
    can still read whatever the OS account can.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TACTUS_ATTEMPT_ID": request.attempt_id,
        "TACTUS_INTENT_ID": request.intent_id,
    }
    if attempt_marker:
        # Per-run marker for the survivor sweep (see containment.ATTEMPT_MARKER_VARIABLE).
        env["TACTUS_ATTEMPT_DIR"] = attempt_marker
    if attempt_dir is not None:
        home = attempt_dir / "home"
        temp = attempt_dir / "tmp"
        home.mkdir(parents=True, exist_ok=True)
        temp.mkdir(parents=True, exist_ok=True)
        gitconfig = home / "gitconfig"
        if not gitconfig.exists():
            gitconfig.write_text(
                "[user]\n\tname = tactus agent\n\temail = agent@tactus.invalid\n"
                "[safe]\n\tdirectory = *\n",
                encoding="utf-8",
            )
        env.update(
            {
                "HOME": str(home),
                "TMPDIR": str(temp),
                "GIT_CONFIG_GLOBAL": str(gitconfig),
                "GIT_CONFIG_SYSTEM": "/dev/null",
            }
        )
    supplied = dict(credentials or {})
    for name in allowlist:
        value = supplied.get(name)
        if value:
            env[name] = value
    return env


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
