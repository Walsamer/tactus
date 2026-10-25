"""Supervision tests against a real fake-agent process.

These are not simulations of supervision: each test starts a real child (and, in
the process-tree cases, a real grandchild), then checks what the supervisor
observed and enforced.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tactus.runtime import (
    REQUEST_PLACEHOLDER,
    RuntimeDescriptor,
    RuntimeLimits,
    RuntimeRequest,
    RuntimeSupervisor,
    TerminationReport,
    Workspace,
    WriterLock,
    WriterLockError,
    terminate_process_group,
)

FAKE_AGENT = Path(__file__).with_name("fake_agent.py")


def _descriptor(mode: str) -> RuntimeDescriptor:
    return RuntimeDescriptor(
        runtime_id="fake-agent",
        adapter="process.exec@1",
        executable=sys.executable,
        argv=(sys.executable, str(FAKE_AGENT), mode, REQUEST_PLACEHOLDER),
        preflight_argv=(sys.executable, str(FAKE_AGENT), "--version"),
        expected_version_token="fake-agent 1.0.0",
    )


def _workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True, exist_ok=True)
    return Workspace(root=root, allowed_scope=("src",))


def _request() -> RuntimeRequest:
    return RuntimeRequest(
        attempt_id="attempt-1", intent_id="intent-1", capability="software.implement"
    )


def _supervisor(tmp_path: Path, mode: str, **kwargs) -> RuntimeSupervisor:
    return RuntimeSupervisor(
        _descriptor(mode),
        _workspace(tmp_path),
        state_dir=tmp_path / "state",
        **kwargs,
    )


# -- success -----------------------------------------------------------------


def test_success_returns_structured_evidence_and_releases_the_lock(tmp_path):
    evidence = _supervisor(tmp_path, "success").run(_request())
    data = evidence.as_dict()

    assert data["status"] == "success"
    assert data["process"]["exit_code"] == 0
    assert data["termination"]["confirmed"] is True
    assert data["writer_lock"] == {"acquired": True, "released": True, "refused": ""}
    assert data["artifacts"]["changed_paths"] == ["src/example.py"]
    assert data["artifacts"]["patch_sha256"]
    assert data["artifacts"]["patch_bytes"] > 0
    assert data["validation"]["errors"] == []
    # The runtime provenance is evidence, not an assumption.
    assert data["runtime"]["executable_sha256"]
    assert "fake-agent 1.0.0" in data["runtime"]["reported_version"]


def test_agent_environment_carries_no_publication_credentials(tmp_path):
    supervisor = _supervisor(tmp_path, "success")
    supervisor.run(_request())

    result = json.loads(
        (supervisor.state_dir / "agent-output" / "result.json").read_text()
    )
    env_keys = set(result["env_keys"])
    assert {"TACTUS_ATTEMPT_ID", "TACTUS_INTENT_ID", "PATH"} <= env_keys
    for forbidden in ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "HOME", "AWS_SECRET_ACCESS_KEY"):
        assert forbidden not in env_keys


def test_request_file_describes_the_bound_workspace(tmp_path):
    supervisor = _supervisor(tmp_path, "success")
    supervisor.run(_request())

    request = json.loads((supervisor.state_dir / "request.json").read_text())
    assert request["attempt_id"] == "attempt-1"
    assert request["intent_id"] == "intent-1"
    assert request["capability"] == "software.implement"
    assert request["workspace"]["allowed_scope"] == ["src"]
    assert request["limits"]["max_changed_paths"] >= 1


# -- agent faults ------------------------------------------------------------


def test_crash_is_recorded_as_failed_with_confirmed_termination(tmp_path):
    evidence = _supervisor(tmp_path, "crash").run(_request())
    data = evidence.as_dict()

    assert data["status"] == "failed"
    assert data["process"]["exit_code"] == 3
    assert data["termination"]["confirmed"] is True
    assert data["writer_lock"]["released"] is True
    assert any("no result.json" in error for error in data["validation"]["errors"])
    assert "fake agent crashed" in data["output"]["stderr"]


def test_no_result_is_failed_not_success(tmp_path):
    evidence = _supervisor(tmp_path, "no-result").run(_request())
    assert evidence.status == "failed"
    assert any("no result.json" in error for error in evidence.errors)


def test_agent_reported_failure_needs_no_patch(tmp_path):
    evidence = _supervisor(tmp_path, "failed").run(_request())
    assert evidence.agent_status == "failed"
    assert evidence.status == "failed"
    assert evidence.changed_paths == ()
    assert evidence.termination.confirmed is True


def test_timeout_terminates_the_tree_within_the_deadline(tmp_path):
    supervisor = _supervisor(
        tmp_path, "timeout", limits=RuntimeLimits(timeout_seconds=0.4)
    )
    started = time.monotonic()
    evidence = supervisor.run(_request())
    elapsed = time.monotonic() - started

    assert evidence.status == "timeout"
    assert evidence.timed_out is True
    assert elapsed < 20.0
    assert evidence.termination.confirmed is True
    assert evidence.termination.signals  # SIGTERM (and possibly SIGKILL) were sent
    assert evidence.writer_lock["released"] is True


def test_cancellation_terminates_the_tree_and_records_the_reason(tmp_path):
    supervisor = _supervisor(
        tmp_path, "timeout", limits=RuntimeLimits(timeout_seconds=30)
    )

    def cancel_soon() -> None:
        time.sleep(0.3)
        supervisor.cancel("operator cancelled")

    thread = threading.Thread(target=cancel_soon)
    thread.start()
    try:
        evidence = supervisor.run(_request())
    finally:
        thread.join()

    assert evidence.status == "cancelled"
    assert evidence.cancelled is True
    assert evidence.cancel_reason == "operator cancelled"
    assert evidence.termination.confirmed is True
    assert evidence.writer_lock["released"] is True


# -- artifact contract -------------------------------------------------------


def test_path_escape_is_detected_and_rejected(tmp_path):
    evidence = _supervisor(tmp_path, "bad-scope").run(_request())
    assert evidence.status == "invalid"
    assert any("escapes the workspace" in error for error in evidence.errors)


def test_patch_may_not_touch_undeclared_paths(tmp_path):
    evidence = _supervisor(tmp_path, "undeclared").run(_request())
    assert evidence.status == "invalid"
    assert any("not declared" in error for error in evidence.errors)


def test_oversized_patch_is_rejected(tmp_path):
    supervisor = _supervisor(
        tmp_path, "wide-patch", limits=RuntimeLimits(max_patch_bytes=1024)
    )
    evidence = supervisor.run(_request())
    assert evidence.status == "invalid"
    assert any("limit is 1024" in error for error in evidence.errors)


# -- writer lock -------------------------------------------------------------


def test_writer_lock_refuses_release_until_termination_is_confirmed(tmp_path):
    lock = WriterLock(tmp_path / "state" / "writer.lock").acquire()
    try:
        with pytest.raises(WriterLockError, match="termination is"):
            lock.release()
        assert lock.held is True

        lock.confirm_termination()
        lock.release()
        assert lock.held is False
    finally:
        lock.confirm_termination()
        lock.release()


def test_a_held_writer_lock_excludes_a_second_writer(tmp_path):
    path = tmp_path / "state" / "writer.lock"
    first = WriterLock(path).acquire()
    try:
        with pytest.raises(WriterLockError, match="already owned"):
            WriterLock(path).acquire()
    finally:
        first.confirm_termination()
        first.release()


def test_unconfirmed_termination_keeps_the_lock_and_reports_unterminated(tmp_path):
    """A terminator that cannot confirm must not hand the workspace to a writer."""
    pgids: list[int] = []

    def unconfirmed(pgid: int, process, grace: float) -> TerminationReport:
        pgids.append(pgid)
        assert process is not None
        try:
            os.killpg(pgid, signal.SIGKILL)  # clean up the real child for the test
        except ProcessLookupError:  # pragma: no cover - already gone
            pass
        with contextlib.suppress(Exception):
            process.wait(timeout=5)
        return TerminationReport(confirmed=False, signals=("SIGKILL",), alive_after=True)

    # A live process group, but a short deadline so the run loop reaches the
    # injected terminator quickly instead of waiting for the agent to finish.
    supervisor = _supervisor(
        tmp_path,
        "timeout",
        limits=RuntimeLimits(timeout_seconds=0.3),
        terminate=unconfirmed,
    )
    evidence = supervisor.run(_request())

    assert evidence.status == "unterminated"
    assert evidence.termination.confirmed is False
    assert evidence.writer_lock["released"] is False
    assert "refusing to release" in evidence.writer_lock["refused"]
    assert pgids  # the supervisor really asked the terminator to settle the group


# -- restart recovery --------------------------------------------------------


def test_recover_terminates_a_process_group_left_by_a_previous_process(tmp_path):
    supervisor = _supervisor(tmp_path, "success")
    state = supervisor.state_dir
    state.mkdir(parents=True, exist_ok=True)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        (state / "attempt.json").write_text(
            json.dumps(
                {
                    "attempt_id": "attempt-left-behind",
                    "phase": "running",
                    "pid": child.pid,
                    "pgid": child.pid,
                }
            )
        )

        report = supervisor.recover()

        assert report.found is True
        assert report.recovered is True
        assert report.termination is not None and report.termination.confirmed is True
        assert report.pgid == child.pid
        assert child.wait(timeout=5) is not None
        record = json.loads((state / "attempt.json").read_text())
        assert record["phase"] == "recovered"
        assert record["termination_confirmed"] is True
    finally:
        if child.poll() is None:
            terminate_process_group(child.pid, child, 1.0)


def test_recover_is_a_no_op_when_the_last_attempt_finished(tmp_path):
    supervisor = _supervisor(tmp_path, "success")
    supervisor.run(_request())

    report = supervisor.recover()

    assert report.found is True
    assert report.recovered is False
    assert report.termination is None


def test_recover_is_a_no_op_without_a_record(tmp_path):
    supervisor = _supervisor(tmp_path, "success")
    assert supervisor.recover().found is False
