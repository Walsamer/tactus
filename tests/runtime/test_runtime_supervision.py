"""Supervision tests against a real fake-agent process.

These are not simulations of supervision: each test starts a real child (and, in
the process-tree cases, a real grandchild), then checks what the supervisor
observed and enforced — including what it refuses to do.
"""

from __future__ import annotations

import contextlib
import hashlib
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
    RuntimeSupervisionError,
    RecoveryReport,
    RuntimeDescriptor,
    RuntimeLimits,
    RuntimeRecoveryRequired,
    RuntimeRequest,
    RuntimeSupervisor,
    TerminationReport,
    Workspace,
    WriterLock,
    WriterLockError,
    process_start_marker,
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


def _request(attempt_id: str = "attempt-1") -> RuntimeRequest:
    return RuntimeRequest(
        attempt_id=attempt_id, intent_id="intent-1", capability="software.implement"
    )


def _supervisor(tmp_path: Path, mode: str, **kwargs) -> RuntimeSupervisor:
    return RuntimeSupervisor(
        _descriptor(mode),
        _workspace(tmp_path),
        state_dir=tmp_path / "state",
        **kwargs,
    )


def _snapshot(state_dir: Path) -> dict[str, str]:
    """Content digest of every file under the state dir (evidence fingerprint)."""
    snapshot: dict[str, str] = {}
    for path in sorted(state_dir.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(state_dir))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return snapshot


def _write_record(supervisor: RuntimeSupervisor, payload: dict) -> None:
    supervisor.state_dir.mkdir(parents=True, exist_ok=True)
    supervisor.attempt_record_path.write_text(json.dumps(payload))


def _live_group() -> tuple[int, subprocess.Popen]:
    """Start an orphan-to-be process group and return (pgid, handle)."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 5
    while process_start_marker(child.pid) is None and time.monotonic() < deadline:
        time.sleep(0.02)
    return child.pid, child


# -- success -----------------------------------------------------------------


def test_success_returns_structured_evidence_and_releases_the_lock(tmp_path):
    evidence = _supervisor(tmp_path, "success").run(_request())
    data = evidence.as_dict()

    assert data["status"] == "success"
    assert data["process"]["exit_code"] == 0
    assert data["termination"]["confirmed"] is True
    assert data["writer_lock"] == {"acquired": True, "released": True, "refused": ""}
    assert data["artifacts"]["declared_paths"] == ["src/example.py"]
    assert data["artifacts"]["patch_paths"] == ["src/example.py"]
    assert data["artifacts"]["patch_sha256"]
    assert data["artifacts"]["patch_bytes"] > 0
    assert data["artifacts"]["filesystem"]["verified"] is False  # no diff provider
    assert data["validation"]["errors"] == []
    assert data["runtime"]["executable_sha256"]
    assert "fake-agent 1.0.0" in data["runtime"]["reported_version"]
    assert Path(data["attempt_dir"]).is_dir()


def test_each_attempt_gets_its_own_evidence_directory(tmp_path):
    supervisor = _supervisor(tmp_path, "success")
    first = supervisor.run(_request("attempt-1"))
    second = supervisor.run(_request("attempt-1"))  # same id, new nonce

    assert first.attempt_dir != second.attempt_dir
    for evidence in (first, second):
        directory = Path(evidence.attempt_dir)
        assert (directory / "request.json").is_file()
        assert (directory / "output" / "result.json").is_file()
        assert (directory / "output" / "patch.diff").is_file()


def test_agent_environment_carries_no_publication_credentials(tmp_path):
    evidence = _supervisor(tmp_path, "success").run(_request())

    result = json.loads(
        (Path(evidence.attempt_dir) / "output" / "result.json").read_text()
    )
    env_keys = set(result["env_keys"])
    assert {"TACTUS_ATTEMPT_ID", "TACTUS_INTENT_ID", "PATH"} <= env_keys
    for forbidden in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "SSH_AUTH_SOCK",
        "HOME",
        "AWS_SECRET_ACCESS_KEY",
    ):
        assert forbidden not in env_keys


def test_request_file_describes_the_bound_workspace(tmp_path):
    evidence = _supervisor(tmp_path, "success").run(_request())

    request = json.loads((Path(evidence.attempt_dir) / "request.json").read_text())
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
    assert evidence.declared_paths == ()
    assert evidence.termination.confirmed is True


def test_timeout_terminates_the_tree_within_the_deadline(tmp_path):
    supervisor = _supervisor(tmp_path, "timeout", limits=RuntimeLimits(timeout_seconds=0.4))
    started = time.monotonic()
    evidence = supervisor.run(_request())
    elapsed = time.monotonic() - started

    assert evidence.status == "timeout"
    assert evidence.timed_out is True
    assert elapsed < 20.0
    assert evidence.termination.confirmed is True
    assert evidence.termination.signals
    assert evidence.writer_lock["released"] is True


def test_cancellation_terminates_the_tree_and_records_the_reason(tmp_path):
    supervisor = _supervisor(tmp_path, "timeout", limits=RuntimeLimits(timeout_seconds=30))

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
    assert evidence.declared_paths == ("src/example.py",)
    assert evidence.patch_paths == ("src/example.py", "src/other.py")
    assert any("not declared" in error for error in evidence.errors)


def test_oversized_patch_is_rejected_before_being_loaded(tmp_path):
    supervisor = _supervisor(
        tmp_path, "wide-patch", limits=RuntimeLimits(max_patch_bytes=1024)
    )
    evidence = supervisor.run(_request())

    assert evidence.status == "invalid"
    assert any("limit is 1024" in error for error in evidence.errors)
    # Rejected on its stat size: nothing was loaded, so no digest is reported.
    assert evidence.patch_bytes == 0
    assert evidence.patch_sha256 == ""


def test_oversized_result_is_rejected_before_being_loaded(tmp_path):
    supervisor = _supervisor(
        tmp_path, "huge-result", limits=RuntimeLimits(max_result_bytes=512)
    )
    evidence = supervisor.run(_request())

    assert evidence.status == "invalid"
    assert any("result.json exceeds the 512-byte limit" in error for error in evidence.errors)
    assert evidence.declared_paths == ()
    # No false success, and the workspace is still usable afterwards.
    assert evidence.writer_lock["released"] is True
    again = _supervisor(tmp_path, "success").run(_request("attempt-2"))
    assert again.status == "success"


def test_filesystem_changes_are_verified_when_a_diff_provider_is_supplied(tmp_path):
    def provider(_root: Path):
        return ("src/example.py", "src/extra.py")

    supervisor = _supervisor(tmp_path, "success", workspace_paths=provider)
    evidence = supervisor.run(_request())

    assert evidence.filesystem["verified"] is True
    assert evidence.filesystem["paths"] == ["src/example.py", "src/extra.py"]
    assert evidence.filesystem["mismatches"] == ["src/extra.py"]
    assert evidence.status == "invalid"


# -- writer lock -------------------------------------------------------------


def test_writer_lock_refuses_release_until_termination_is_confirmed(tmp_path):
    lock = WriterLock(tmp_path / "state" / "writer.lock").acquire()
    try:
        with pytest.raises(WriterLockError, match="termination is"):
            lock.release()
        lock.confirm_termination()
        with pytest.raises(WriterLockError, match="record is written"):
            lock.release()
        lock.mark_settled()
        lock.release()
        assert lock.held is False
    finally:
        lock.mark_settled()
        lock.confirm_termination()
        lock.release()


def test_a_held_writer_lock_excludes_a_second_writer(tmp_path):
    path = tmp_path / "state" / "writer.lock"
    first = WriterLock(path).acquire()
    try:
        with pytest.raises(WriterLockError, match="already owned"):
            WriterLock(path).acquire()
    finally:
        first.mark_settled()
        first.confirm_termination()
        first.release()


def test_unconfirmed_termination_keeps_the_lock_and_reports_unterminated(tmp_path):
    """A terminator that cannot confirm must not hand the workspace to a writer."""

    killed: list[int] = []

    def unconfirmed(pgid: int, process, grace: float) -> TerminationReport:
        # Deliberately does not terminate: the group stays alive, which is what
        # an unconfirmable termination looks like to the supervisor.
        killed.append(pgid)
        return TerminationReport(confirmed=False, signals=(), alive_after=True)

    supervisor = _supervisor(
        tmp_path,
        "timeout",
        limits=RuntimeLimits(timeout_seconds=0.3),
        terminate=unconfirmed,
    )
    try:
        evidence = supervisor.run(_request())

        assert evidence.status == "unterminated"
        assert evidence.termination.confirmed is False
        assert evidence.writer_lock["released"] is False
        record = json.loads(supervisor.attempt_record_path.read_text())
        assert record["phase"] == "unterminated"
    finally:
        for pgid in killed:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)


def test_unterminated_attempt_blocks_a_new_writer_when_recovery_cannot_verify(tmp_path):
    """The hold survives until recovery is verified or an operator resolves it."""
    supervisor = _supervisor(tmp_path, "success", start_marker=lambda _pid: None)
    pgid, child = _live_group()
    try:
        _write_record(
            supervisor,
            {
                "attempt_id": "attempt-unterminated",
                "phase": "unterminated",
                "pid": pgid,
                "pgid": pgid,
                "start_marker": "unknown",
            },
        )

        with pytest.raises(RuntimeRecoveryRequired, match="unresolved|unterminated"):
            supervisor.run(_request("attempt-2"))

        assert child.poll() is None, "an unverified group must not be signalled"
        assert json.loads(supervisor.attempt_record_path.read_text())["phase"] == "unresolved"
    finally:
        terminate_process_group(pgid, child, 1.0)


# -- concurrency -------------------------------------------------------------


def test_concurrent_attempt_cannot_touch_the_first_attempts_state(tmp_path):
    """Two supervisors, same workspace/state dir: the second must change nothing."""
    holder = _supervisor(tmp_path, "timeout", limits=RuntimeLimits(timeout_seconds=30))
    first: list = []

    def run_first() -> None:
        first.append(holder.run(_request()))

    thread = threading.Thread(target=run_first)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            record = holder.attempt_record_path
            if record.exists() and json.loads(record.read_text()).get("phase") == "running":
                break
            time.sleep(0.02)
        else:  # pragma: no cover - would mean the first attempt never started
            pytest.fail("first attempt never reached the running phase")

        before = _snapshot(holder.state_dir)
        intruder = _supervisor(tmp_path, "success")
        with pytest.raises(WriterLockError):
            intruder.run(_request("attempt-2"))
        after = _snapshot(holder.state_dir)

        assert after == before, "a refused attempt modified state it does not own"
    finally:
        holder.cancel("test teardown")
        thread.join(timeout=60)

    assert first and first[0].status == "cancelled"


# -- restart recovery --------------------------------------------------------


def test_recover_terminates_a_verified_leftover_group(tmp_path):
    supervisor = _supervisor(tmp_path, "success")
    pgid, child = _live_group()
    try:
        _write_record(
            supervisor,
            {
                "attempt_id": "attempt-left-behind",
                "phase": "running",
                "pid": pgid,
                "pgid": pgid,
                "start_marker": process_start_marker(pgid),
            },
        )

        report = supervisor.recover()

        assert report.found is True
        assert report.recovered is True, report.termination
        assert report.signalled is True
        assert report.termination is not None and report.termination.confirmed is True
        assert child.wait(timeout=5) is not None
        record = json.loads(supervisor.attempt_record_path.read_text())
        assert record["phase"] == "recovered"
    finally:
        if child.poll() is None:
            terminate_process_group(pgid, child, 1.0)


def test_recover_never_signals_a_reused_pgid(tmp_path):
    """A recorded pgid may now belong to an unrelated process: never signal it."""
    supervisor = _supervisor(tmp_path, "success")
    pgid, child = _live_group()
    try:
        _write_record(
            supervisor,
            {
                "attempt_id": "attempt-left-behind",
                "phase": "running",
                "pid": pgid,
                "pgid": pgid,
                "start_marker": "Thu Jan  1 00:00:00 1970",
            },
        )

        report = supervisor.recover()

        assert report.phase == "recovered"
        assert report.reused_pgid is True
        assert report.signalled is False
        assert child.poll() is None, "an unrelated process group was signalled"
    finally:
        terminate_process_group(pgid, child, 1.0)


def test_recover_marks_unverifiable_identity_as_unresolved(tmp_path):
    supervisor = _supervisor(tmp_path, "success", start_marker=lambda _pid: None)
    pgid, child = _live_group()
    try:
        _write_record(
            supervisor,
            {
                "attempt_id": "attempt-unknown",
                "phase": "running",
                "pid": pgid,
                "pgid": pgid,
                "start_marker": "whatever",
            },
        )

        report = supervisor.recover()

        assert report.phase == "unresolved"
        assert report.requires_operator is True
        assert report.signalled is False
        assert child.poll() is None
        assert json.loads(supervisor.attempt_record_path.read_text())["phase"] == "unresolved"
    finally:
        terminate_process_group(pgid, child, 1.0)


def test_crash_while_spawning_blocks_new_writers(tmp_path):
    """phase=spawning has no verified process identity: fail closed, mutate nothing."""
    supervisor = _supervisor(tmp_path, "success")
    record = {
        "attempt_id": "attempt-spawning",
        "phase": "spawning",
        "pid": None,
        "pgid": None,
    }
    _write_record(supervisor, record)

    report = supervisor.recover()
    assert report.phase == "unresolved"
    assert report.requires_operator is True

    with pytest.raises(RuntimeRecoveryRequired, match="identity unknown|unresolved"):
        _supervisor(tmp_path, "success").run(_request("attempt-2"))

    preserved = json.loads(supervisor.attempt_record_path.read_text())
    assert preserved["attempt_id"] == "attempt-spawning"
    assert preserved["phase"] == "unresolved"


def test_settled_previous_attempt_allows_the_next_run(tmp_path):
    supervisor = _supervisor(tmp_path, "success")
    supervisor.run(_request("attempt-1"))

    report = supervisor.recover()

    assert report.found is True
    assert report.phase == "settled"
    assert _supervisor(tmp_path, "success").run(_request("attempt-2")).status == "success"


def test_operator_resolution_releases_the_hold_and_is_audited(tmp_path):
    supervisor = _supervisor(tmp_path, "success")
    _write_record(
        supervisor,
        {"attempt_id": "attempt-spawning", "phase": "spawning", "pid": None, "pgid": None},
    )
    with pytest.raises(RuntimeRecoveryRequired):
        supervisor.run(_request("attempt-2"))

    with pytest.raises(RuntimeSupervisionError, match="operator and reason"):
        supervisor.resolve_unresolved(operator="", reason="")

    report = supervisor.resolve_unresolved(
        operator="operator@example", reason="confirmed no child process was started"
    )
    assert report.phase == "resolved"

    audit = [
        json.loads(line)
        for line in (supervisor.state_dir / "recovery.jsonl").read_text().splitlines()
    ]
    assert any(entry["action"] == "operator_resolved" for entry in audit)
    assert _supervisor(tmp_path, "success").run(_request("attempt-3")).status == "success"
