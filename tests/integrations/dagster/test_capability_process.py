"""Regression tests for the bounded capability subprocess.

Hermetic and offline: the module under test is Dagster-free, so these run in
Tactus's own test environment (which does not install Dagster). The properties
under test are the ones the acceptance evidence depends on:

* a real OS process runs and its identity/exit status are reported;
* captures are bounded *while reading*, not after buffering;
* the deadline kills and reaps the child;
* the temporary working directory is removed on every path — including a failed
  spawn and an unexpected capture error.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "integrations"
    / "dagster"
    / "capability_process.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("tactus_capability_process", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cap = _load()


def _run(tmp_path: Path, **overrides):
    kwargs = {
        "intent_id": "intent:proposal:snapshot-entry",
        "attempt": 2,
        "capability": "demo.verify",
        "exit_code": 0,
        "workspace_root": tmp_path,
    }
    kwargs.update(overrides)
    return cap.run_capability_subprocess(**kwargs)


def test_success_captures_identity_exit_status_and_receipt(tmp_path):
    evidence = _run(tmp_path)

    assert isinstance(evidence["pid"], int) and evidence["pid"] > 0
    assert evidence["exit_code"] == 0
    assert evidence["timed_out"] is False
    assert evidence["output_limit_exceeded"] is False
    assert evidence["stdout_truncated"] is False
    assert evidence["stderr_truncated"] is False
    # The receipt proves *this* attempt of *this* intent ran in that process.
    assert "intent:proposal:snapshot-entry" in evidence["stdout"]
    assert '"attempt": 2' in evidence["stdout"]
    assert evidence["stdout_sha256"] == cap.sha256_hex(evidence["stdout"])
    assert evidence["stdout_bytes"] == len(evidence["stdout"].encode("utf-8"))
    assert evidence["timeout_seconds"] == cap.SUBPROCESS_TIMEOUT_SECONDS
    assert evidence["capture_limit_bytes"] == cap.MAX_CAPTURE_BYTES
    # Honest terminology: hygiene, not isolation.
    assert evidence["isolation"].startswith("none")
    assert "temporary directory" in evidence["working_directory"]
    assert list(tmp_path.iterdir()) == []


def test_non_zero_exit_status_is_reported_with_the_receipt(tmp_path):
    evidence = _run(tmp_path, exit_code=7)

    assert evidence["exit_code"] == 7
    assert evidence["timed_out"] is False
    assert "intent:proposal:snapshot-entry" in evidence["stdout"]
    assert list(tmp_path.iterdir()) == []


def test_deadline_kills_and_reaps_the_child(tmp_path):
    started = time.monotonic()
    evidence = _run(
        tmp_path,
        program="import time; time.sleep(30)",
        timeout=0.3,
    )
    elapsed = time.monotonic() - started

    assert evidence["timed_out"] is True
    assert evidence["exit_code"] != 0  # killed, never a clean exit
    assert elapsed < 10.0  # the deadline, not the child, ended the exchange
    assert evidence["stdout"] == ""
    assert list(tmp_path.iterdir()) == []


def test_capture_is_bounded_while_reading(tmp_path):
    """A flooding child is cut at the cap, not buffered and then truncated."""
    evidence = _run(
        tmp_path,
        program="import sys\nfor _ in range(20000): sys.stdout.write('x' * 100)\n",
        max_bytes=64,
    )

    assert evidence["output_limit_exceeded"] is True
    assert evidence["stdout_truncated"] is True
    # Exactly the cap was retained; the process was killed rather than drained.
    assert evidence["stdout_bytes"] == 64
    assert evidence["stdout"] == "x" * 64 + cap.TRUNCATION_MARKER
    assert evidence["exit_code"] != 0
    assert list(tmp_path.iterdir()) == []


def test_workspace_is_removed_when_the_spawn_fails(tmp_path):
    def refuse(*_args, **_kwargs):
        raise OSError("cannot spawn the capability program")

    with pytest.raises(OSError, match="cannot spawn"):
        _run(tmp_path, popen_factory=refuse)

    assert list(tmp_path.iterdir()) == []


def test_workspace_is_removed_when_capture_raises(tmp_path, monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("injected capture failure")

    monkeypatch.setattr(cap, "_drain", explode)

    with pytest.raises(RuntimeError, match="injected capture failure"):
        _run(tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_invalid_utf8_output_is_replaced_not_raised(tmp_path):
    evidence = _run(
        tmp_path,
        program=(
            "import sys\n"
            "sys.stdout.buffer.write(b'\\xff\\xfe ok\\n')\n"
            "sys.stdout.flush()\n"
        ),
    )

    assert evidence["exit_code"] == 0
    assert "ok" in evidence["stdout"]
    assert "\ufffd" in evidence["stdout"]
