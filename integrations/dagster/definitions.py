"""Tactus-owned local Dagster code location for execution-failure scenarios.

This is **not** the Ictus execution backend. Ictus ships the production bridge
(``ictus_dagster``: submission, receipts, result mapping, workflow registry) and
keeps ownership of the durable-execution contract. This module owns one thing:
a *capability implementation* that runs its work in a bounded local
subprocess and can fail deterministically, so Tactus can exercise transient and
terminal execution failures against the real bridge without touching the pinned
Ictus revision or an authorized ``ExecutionIntent``.

Boundary contract (see ``docs/architecture/END_TO_END_EXECUTION_VALIDATION.md``)
---------------------------------------------------------------------------
* The Ictus bridge submits by code-location **name**
  (``ICTUS_DAGSTER_CODE_LOCATION``) and job name resolved from Ictus's own
  ``workflows.toml``. Our job therefore reuses the same job name and the same
  ``settings`` resource key/shape, and nothing else.
* Failure configuration comes from :mod:`failure_control` (an explicit,
  bounded JSON file keyed by the run's ``ictus.intent_id`` tag). It is never
  read from, written to, or merged into the ``ExecutionIntent``.
* Retry mechanics stay Dagster's: the execute step declares
  ``RetryPolicy(max_retries=3)`` and reads its own 1-based attempt from
  ``context.retry_number``. No attempt counter is kept in application state.
* The step names match the bridge's ``FAILED_STEP_CATEGORY`` table so a
  terminal failure normalizes to ``PROCESS_CRASH`` and Ictus — not this
  module — decides what that means.

Only ``failure_control`` is imported at module import time (stdlib only). The
Dagster import happens here because this file is loaded by ``dagster-daemon``
from the pinned Ictus virtualenv, not by Tactus's own test environment.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from dagster import (
    ConfigurableResource,
    Definitions,
    Failure,
    In,
    Out,
    RetryPolicy,
    job,
    op,
)

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # loaded by path (`python_file`) by the daemon
    sys.path.insert(0, str(_HERE))

from failure_control import (  # noqa: E402  (path bootstrap above)
    FailureProgramme,
    programme_for_intent,
    record_attempt,
)

def hashlib_sha256(text: str) -> str:
    """SHA-256 of one captured string (evidence digest, never a decision)."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


#: Intent-id run tag written by the Ictus submission bridge.
INTENT_TAG = "ictus.intent_id"

#: Mirrors the pinned bridge's own retry budget for the execute step.
EXECUTE_RETRY_POLICY = RetryPolicy(max_retries=3, delay=0)

#: The capability work runs as a bounded local subprocess: one process per
#: attempt, in its own throwaway working directory, with a hard timeout and
#: bounded captured output. This is the execution hop the acceptance criterion
#: names (`independently launched Dagster run -> subprocess -> IMPLEMENTED`).
SUBPROCESS_TIMEOUT_SECONDS = 30.0
MAX_CAPTURE_BYTES = 4096

#: Harmless, deterministic, stdlib-only program. It reads one JSON request on
#: stdin, prints a canonical receipt (so the caller can prove *this* process ran
#: for *this* attempt), and exits with the requested status.
_CAPABILITY_PROGRAM = """
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


class CapabilitySettings(ConfigurableResource):
    """Resource config the Ictus bridge sends for a capability intent.

    The fields must accept exactly what the pinned bridge's settings mapper
    emits so the run config validates. The intent-derived ``fail_*`` values are
    accepted for contract compatibility and deliberately **ignored**: an
    authorized intent is not a failure-injection channel. Injection is owned by
    :mod:`failure_control`.
    """

    capability: str = "demo.verify"
    fail_until_attempt: int = 0
    fail_hard: bool = False
    fail_verify: bool = False
    hold_seconds: float = 0.0


def _bounded(text: str, limit: int = MAX_CAPTURE_BYTES) -> tuple[str, bool]:
    """Bound one captured stream, keeping the head and flagging truncation."""
    text = text or ""
    if len(text) <= limit:
        return text, False
    return text[:limit] + "... [truncated]", True


def _run_capability_subprocess(
    *, intent_id: str, attempt: int, capability: str, exit_code: int
) -> dict[str, Any]:
    """Run the capability work as one bounded, isolated OS process.

    The child gets a minimal environment, its own working directory and a hard
    timeout; the caller receives the real pid, exit status and bounded captures.
    The authorized intent is *input* to the child, never modified by it.
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
    workspace = tempfile.mkdtemp(prefix="tactus-capability-")
    started = time.monotonic()
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "executable": sys.executable,
        "pid": None,
        "exit_code": None,
        "timed_out": False,
        "duration_ms": 0,
        "stdout": "",
        "stderr": "",
        "stdout_sha256": "",
        "stdout_bytes": 0,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "request_sha256": hashlib_sha256(request),
        "command": [Path(sys.executable).name, "-I", "-c", "<capability-program>"],
        "working_directory_isolated": True,
    }
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", _CAPABILITY_PROGRAM],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=workspace,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    evidence["pid"] = process.pid
    try:
        try:
            stdout, stderr = process.communicate(request, timeout=SUBPROCESS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            evidence["timed_out"] = True
            process.kill()
            stdout, stderr = process.communicate()
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    stdout, stdout_truncated = _bounded(stdout)
    stderr, stderr_truncated = _bounded(stderr)
    evidence.update(
        {
            "exit_code": process.returncode,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "stdout": stdout,
            "stderr": stderr,
            "stdout_sha256": hashlib_sha256(stdout),
            "stdout_bytes": len(stdout),
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }
    )
    return evidence


def _programme(context) -> FailureProgramme:
    intent_id = str((context.run_tags or {}).get(INTENT_TAG, ""))
    return programme_for_intent(intent_id)


@op(out=Out(dict))
def prepare_op(context, settings: CapabilitySettings) -> dict:
    """Deterministic prepare step: no domain or policy semantics."""
    context.log.info("prepare: capability=%s", settings.capability)
    return {"capability": settings.capability, "prepared": True}


@op(ins={"prepared": In(dict)}, out=Out(dict), retry_policy=EXECUTE_RETRY_POLICY)
def execute_op(context, prepared: dict, settings: CapabilitySettings) -> dict:
    """Execute the capability, failing only if the bounded programme says so.

    ``context.retry_number`` is Dagster's own count (0 on the first attempt), so
    step retries never advance Tactus's semantic attempt count.
    """
    attempt = int(context.retry_number) + 1
    programme = _programme(context)
    intent_id = str((context.run_tags or {}).get(INTENT_TAG, ""))
    context.log.info(
        "execute: attempt=%d capability=%s programme=%s",
        attempt,
        settings.capability,
        programme.as_dict(),
    )
    if programme.fails_on(attempt):
        record_attempt(
            intent_id=intent_id, attempt=attempt, programme=programme, outcome="failed"
        )
        raise Failure(
            description=(
                "tactus capability failure injection: attempt "
                f"{attempt} of {settings.capability}"
            )
        )

    evidence = _run_capability_subprocess(
        intent_id=intent_id,
        attempt=attempt,
        capability=settings.capability,
        exit_code=programme.exit_code,
    )
    # One structured line so the backend's own event log carries the process
    # boundary; the same payload is mirrored into the attempt ledger.
    context.log.info(
        "tactus.capability.subprocess %s", json.dumps(evidence, sort_keys=True)
    )
    failed = bool(evidence["timed_out"]) or evidence["exit_code"] != 0
    record_attempt(
        intent_id=intent_id,
        attempt=attempt,
        programme=programme,
        outcome="failed" if failed else "succeeded",
        details={"subprocess": evidence, "run_id": context.run_id},
    )
    if evidence["timed_out"]:
        raise Failure(
            description=(
                "tactus capability subprocess timed out after "
                f"{SUBPROCESS_TIMEOUT_SECONDS:.0f}s"
            )
        )
    if evidence["exit_code"] != 0:
        raise Failure(
            description=(
                f"tactus capability subprocess exited {evidence['exit_code']} "
                f"(intent {intent_id})"
            )
        )
    return {
        **prepared,
        "execute_attempts": attempt,
        "executed": True,
        "subprocess": evidence,
    }


@op(ins={"executed": In(dict)}, out=Out(dict))
def verify_op(context, executed: dict) -> dict:
    """Deterministic verify step: reports the executed payload unchanged."""
    if not executed.get("executed"):
        raise Failure(description="verify observed an unexecuted capability")
    context.log.info("verify: capability=%s", executed.get("capability"))
    return {**executed, "verified": True}


@op(ins={"verified": In(dict)}, out=Out(dict))
def finalize_op(context, verified: dict) -> dict:
    """Deterministic finalize step: returns the terminal execution facts."""
    context.log.info("finalize: capability=%s", verified.get("capability"))
    return {**verified, "finalized": True}


@job(
    name="capability_execution_job",
    resource_defs={"settings": CapabilitySettings()},
    description=(
        "Tactus-owned capability implementation used by the execution-failure "
        "scenarios. Same job name/resource shape as the pinned bridge expects; "
        "failure injection is bounded and lives outside the intent."
    ),
)
def capability_execution_job() -> None:
    prepared = prepare_op()
    executed = execute_op(prepared)
    verified = verify_op(executed)
    finalize_op(verified)


defs = Definitions(jobs=[capability_execution_job])
