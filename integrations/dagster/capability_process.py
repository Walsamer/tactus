"""Bounded local capability subprocess (Dagster-free, unit-testable).

One attempt of a capability executes exactly one fixed, deterministic,
stdlib-only program. This module owns that process: spawning it, feeding it one
small request, capturing **bounded** output until a deadline, and reaping it.

Boundaries and honest terminology
---------------------------------
* This is **not a sandbox**. The child runs as the same OS account with the same
  filesystem and network reach as the Dagster daemon. A minimal environment and
  ``python -I`` are *hygiene* (no inherited ``PYTHONPATH``/user site/config), not
  isolation. Filesystem and network isolation would require an OS mechanism this
  module deliberately does not add.
* Captures are bounded **while reading** (never "read everything, then slice"):
  at most ``max_bytes`` per stream is kept, the process is killed once either
  stream exceeds the cap, and both the truncation and the cap event are reported.
* The deadline covers the whole exchange (write + read + reap). A timeout kills
  and reaps the child and returns the partial capture.
* The working directory is a fresh temporary directory and is always removed —
  including when the spawn fails, the child times out, or capture raises.

Nothing here decides anything: it reports process facts to the caller.
"""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping

__all__ = [
    "MAX_CAPTURE_BYTES",
    "SUBPROCESS_TIMEOUT_SECONDS",
    "CAPABILITY_PROGRAM",
    "TRUNCATION_MARKER",
    "run_capability_subprocess",
    "sha256_hex",
]

#: Hard ceiling on one attempt's total wall clock.
SUBPROCESS_TIMEOUT_SECONDS = 30.0
#: Bytes kept per stream (the child's output is one small JSON receipt).
MAX_CAPTURE_BYTES = 4096
#: Appended when a stream is cut at the cap.
TRUNCATION_MARKER = "... [truncated]"
#: Read size per syscall while draining the pipes.
_READ_CHUNK = 65536
#: Rounds the reader makes per stream per select wake-up before re-checking the deadline.
_MAX_READS_PER_WAKEUP = 64

#: Harmless, deterministic, stdlib-only program. It reads one JSON request on
#: stdin, prints a canonical receipt (so the caller can prove *this* process ran
#: for *this* attempt), and exits with the requested status.
CAPABILITY_PROGRAM = """
import hashlib, json, sys

request = json.load(sys.stdin)
receipt = {
    "schema_version": 1,
    "intent_id": request["intent_id"],
    "attempt": request["attempt"],
    "capability": request["capability"],
    "payload_sha256": hashlib.sha256(
        (str(request["intent_id"]) + ":" + str(request["attempt"])).encode()
    ).hexdigest(),
}
print(json.dumps(receipt, sort_keys=True))
sys.exit(int(request["exit_code"]))
"""


def sha256_hex(text: str) -> str:
    """SHA-256 of one captured string (evidence digest, never a decision)."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


class _Capture:
    """Bounded multi-stream capture result."""

    __slots__ = ("buffers", "truncated", "limit_exceeded", "timed_out")

    def __init__(self) -> None:
        self.buffers: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        self.truncated: dict[str, bool] = {"stdout": False, "stderr": False}
        self.limit_exceeded = False
        self.timed_out = False


def _drain(process: "subprocess.Popen[bytes]", *, deadline: float, limit: int) -> _Capture:
    """Read both pipes until EOF, the byte cap, or the deadline.

    Reads are incremental and capped, so a child that floods either stream can
    never make this module buffer unbounded data: the cap is enforced before the
    bytes are retained, and the caller kills the child afterwards.
    """
    capture = _Capture()
    selector = selectors.DefaultSelector()
    streams = {"stdout": process.stdout, "stderr": process.stderr}
    for name, stream in streams.items():
        if stream is None:  # pragma: no cover - both pipes are always requested
            continue
        os.set_blocking(stream.fileno(), False)
        selector.register(stream.fileno(), selectors.EVENT_READ, name)
    open_names = {name for name, stream in streams.items() if stream is not None}
    try:
        while open_names:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                capture.timed_out = True
                break
            for key, _ in selector.select(remaining):
                name = key.data  # type: ignore[assignment]
                for _ in range(_MAX_READS_PER_WAKEUP):
                    try:
                        chunk = os.read(key.fd, _READ_CHUNK)
                    except (BlockingIOError, InterruptedError):
                        # Drained for now: go back to the selector (which also
                        # re-checks the deadline).
                        break
                    if not chunk:
                        selector.unregister(key.fd)
                        open_names.discard(name)
                        break
                    buffer = capture.buffers[name]
                    room = limit - len(buffer)
                    if room > 0:
                        buffer.extend(chunk[:room])
                    if len(chunk) > max(room, 0):
                        capture.truncated[name] = True
                        capture.limit_exceeded = True
                if capture.limit_exceeded:
                    break
            if capture.limit_exceeded:
                break
    finally:
        selector.close()
    return capture


def _decode(raw: bytearray, *, truncated: bool) -> str:
    text = raw.decode("utf-8", "replace")
    return text + TRUNCATION_MARKER if truncated else text


def run_capability_subprocess(
    *,
    intent_id: str,
    attempt: int,
    capability: str,
    exit_code: int,
    timeout: float = SUBPROCESS_TIMEOUT_SECONDS,
    max_bytes: int = MAX_CAPTURE_BYTES,
    workspace_root: str | os.PathLike[str] | None = None,
    program: str = CAPABILITY_PROGRAM,
    popen_factory: Callable[..., "subprocess.Popen[bytes]"] = subprocess.Popen,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run one capability attempt and return bounded process evidence.

    ``program``, ``popen_factory``, ``environment`` and ``workspace_root`` exist
    so the bounded-capture and cleanup behaviour can be regression-tested
    hermetically; production callers use the defaults.
    """
    request = json.dumps(
        {
            "intent_id": intent_id,
            "attempt": attempt,
            "capability": capability,
            "exit_code": exit_code,
        },
        sort_keys=True,
    )
    command = [sys.executable, "-I", "-c", "<capability-program>"]
    started = time.monotonic()
    deadline = started + timeout
    workspace = tempfile.mkdtemp(prefix="tactus-capability-", dir=workspace_root)
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "executable": sys.executable,
        "pid": None,
        "exit_code": None,
        "timed_out": False,
        "output_limit_exceeded": False,
        "duration_ms": 0.0,
        "stdout": "",
        "stderr": "",
        "stdout_sha256": "",
        "stdout_bytes": 0,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "request_sha256": sha256_hex(request),
        "command": command,
        "working_directory": "fresh temporary directory (removed after the attempt)",
        "environment": sorted((environment or {"PATH": os.environ.get("PATH", "/usr/bin:/bin")})),
        "isolation": "none (same OS account: no filesystem or network boundary)",
        "capture_limit_bytes": max_bytes,
        "timeout_seconds": timeout,
    }
    capture = _Capture()
    process = None
    try:
        process = popen_factory(
            [sys.executable, "-I", "-c", program],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workspace,
            env=dict(environment or {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}),
        )
        evidence["pid"] = getattr(process, "pid", None)
        try:
            # The request is small; a child may exit before reading it, which is
            # reported through the capture rather than raised here.
            try:
                assert process.stdin is not None
                process.stdin.write(request.encode("utf-8"))
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            capture = _drain(process, deadline=deadline, limit=max_bytes)
        finally:
            # Kill before waiting so a timed-out or flooding child cannot block
            # the reaper; the partial capture is already collected above.
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:  # pragma: no cover - kill should suffice
                process.kill()
                process.wait()
    finally:
        # Covers spawn failure, timeout, capture error and normal completion.
        shutil.rmtree(workspace, ignore_errors=True)

    stdout = _decode(capture.buffers["stdout"], truncated=capture.truncated["stdout"])
    stderr = _decode(capture.buffers["stderr"], truncated=capture.truncated["stderr"])
    evidence.update(
        {
            "exit_code": getattr(process, "returncode", None),
            "timed_out": capture.timed_out,
            "output_limit_exceeded": capture.limit_exceeded,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "stdout": stdout,
            "stderr": stderr,
            "stdout_sha256": sha256_hex(stdout),
            "stdout_bytes": len(capture.buffers["stdout"]),
            "stdout_truncated": capture.truncated["stdout"],
            "stderr_truncated": capture.truncated["stderr"],
        }
    )
    return evidence
