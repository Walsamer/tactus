"""Unit tests for the bounded capability failure-control surface.

These tests are hermetic and offline: ``failure_control`` must stay importable
in Tactus's own test environment, which does not install Dagster. The Dagster
code location that consumes it (``integrations/dagster/definitions.py``) is
loaded only by the daemon, from the pinned Ictus virtualenv.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[3] / "integrations" / "dagster" / "failure_control.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("tactus_failure_control", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before execution: ``@dataclass`` resolves annotations through
    # ``sys.modules[cls.__module__]``.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fc = _load()


def _write(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "control.json"
    path.write_text(json.dumps(document))
    return path


def _control(*programmes: tuple[str, dict]) -> dict:
    return {"version": 1, "intents": {intent: body for intent, body in programmes}}


def test_control_module_has_no_dagster_dependency():
    """The control surface must never drag Dagster into the default suite."""
    assert "dagster" not in sys.modules


def test_absent_control_file_means_no_injection(monkeypatch):
    monkeypatch.delenv(fc.CONTROL_PATH_ENV, raising=False)
    assert fc.control_path() is None
    assert fc.programme_for_intent("intent:any") is fc.NO_FAILURE
    assert fc.programme_for_intent("intent:any").injects_failure is False


def test_transient_programme_fails_only_the_first_attempts(tmp_path, monkeypatch):
    path = _write(tmp_path, _control(("intent:one", {"fail_attempts": 1})))
    monkeypatch.setenv(fc.CONTROL_PATH_ENV, str(path))

    programme = fc.programme_for_intent("intent:one")
    assert programme.fails_on(1) is True
    assert programme.fails_on(2) is False
    assert programme.fails_on(0) is False
    assert programme.injects_failure is True


def test_terminal_programme_fails_every_attempt(tmp_path, monkeypatch):
    path = _write(tmp_path, _control(("intent:two", {"fail_hard": True})))
    monkeypatch.setenv(fc.CONTROL_PATH_ENV, str(path))

    programme = fc.programme_for_intent("intent:two")
    assert [programme.fails_on(attempt) for attempt in (1, 2, 3, 4)] == [True] * 4


def test_subprocess_exit_code_programme_runs_the_capability(tmp_path, monkeypatch):
    """A non-zero exit code is a *post-subprocess* failure shape, not a step failure."""
    path = _write(tmp_path, _control(("intent:three", {"exit_code": 7})))
    monkeypatch.setenv(fc.CONTROL_PATH_ENV, str(path))

    programme = fc.programme_for_intent("intent:three")
    assert programme.exit_code == 7
    assert programme.injects_failure is True
    # It must not short-circuit the step: the capability subprocess still runs.
    assert programme.fails_on(1) is False
    assert programme.fails_on(4) is False


def test_unlisted_intent_gets_no_programme(tmp_path, monkeypatch):
    path = _write(tmp_path, _control(("intent:one", {"fail_attempts": 1})))
    monkeypatch.setenv(fc.CONTROL_PATH_ENV, str(path))

    assert fc.programme_for_intent("intent:other") is fc.NO_FAILURE


def test_attempts_beyond_the_retry_budget_are_rejected(tmp_path, monkeypatch):
    path = _write(tmp_path, _control(("intent:one", {"fail_attempts": 4})))
    monkeypatch.setenv(fc.CONTROL_PATH_ENV, str(path))

    with pytest.raises(fc.FailureControlError, match="0\\.\\.3"):
        fc.programme_for_intent("intent:one")


@pytest.mark.parametrize(
    "body, message",
    (
        ({"fail_hard": True, "fail_attempts": 1}, "mixes failure shapes"),
        ({"fail_hard": True, "exit_code": 7}, "mixes failure shapes"),
        ({"fail_attempts": 1, "exit_code": 7}, "mixes failure shapes"),
        ({"fail_attempts": 0}, "injects nothing"),
        ({}, "is empty"),
        ({"fail_attempts": "1"}, "must be an integer"),
        ({"fail_hard": "yes"}, "must be a boolean"),
        ({"fail_always": True}, "unknown key"),
        ({"exit_code": 0}, "injects nothing"),
        ({"exit_code": 126}, "exit_code must be within"),
        ({"exit_code": "7"}, "exit_code must be an integer"),
        ({"exit_code": True}, "exit_code must be an integer"),
    ),
)
def test_invalid_programmes_fail_closed(tmp_path, monkeypatch, body, message):
    path = _write(tmp_path, _control(("intent:one", body)))
    monkeypatch.setenv(fc.CONTROL_PATH_ENV, str(path))

    with pytest.raises(fc.FailureControlError, match=message):
        fc.programme_for_intent("intent:one")


@pytest.mark.parametrize(
    "key", ("fail_until_attempt", "fail_verify", "hold_seconds")
)
def test_intent_argument_keys_are_rejected(tmp_path, monkeypatch, key):
    """Execution knobs must never travel inside an authorized intent."""
    path = _write(tmp_path, _control(("intent:one", {key: 1})))
    monkeypatch.setenv(fc.CONTROL_PATH_ENV, str(path))

    with pytest.raises(fc.FailureControlError, match="intent-argument key"):
        fc.programme_for_intent("intent:one")


def test_document_shape_is_validated(tmp_path, monkeypatch):
    for document, message in (
        ({"intents": {}}, "must declare version 1"),
        ({"version": 2, "intents": {}}, "must declare version 1"),
        ({"version": 1}, "must contain an 'intents' object"),
        ({"version": 1, "intents": {}, "extra": 1}, "unknown top-level key"),
        ({"version": 1, "intents": {"": {"fail_hard": True}}}, "empty intent id"),
    ):
        path = _write(tmp_path, document)
        monkeypatch.setenv(fc.CONTROL_PATH_ENV, str(path))
        with pytest.raises(fc.FailureControlError, match=message):
            fc.programme_for_intent("intent:one")


def test_malformed_json_is_never_silently_ignored(tmp_path, monkeypatch):
    path = tmp_path / "control.json"
    path.write_text("{not json")
    monkeypatch.setenv(fc.CONTROL_PATH_ENV, str(path))

    with pytest.raises(fc.FailureControlError, match="not valid JSON"):
        fc.programme_for_intent("intent:one")


def test_missing_explicit_control_file_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv(fc.CONTROL_PATH_ENV, str(tmp_path / "absent.json"))

    with pytest.raises(fc.FailureControlError, match="missing"):
        fc.programme_for_intent("intent:one")


def test_attempt_ledger_is_opt_in_and_append_only(tmp_path, monkeypatch):
    monkeypatch.delenv(fc.ATTEMPT_LOG_ENV, raising=False)
    fc.record_attempt(
        intent_id="intent:one", attempt=1, programme=fc.NO_FAILURE, outcome="failed"
    )
    assert list(tmp_path.iterdir()) == []

    ledger = tmp_path / "attempts.jsonl"
    monkeypatch.setenv(fc.ATTEMPT_LOG_ENV, str(ledger))
    programme = fc.FailureProgramme(fail_attempts=1)
    fc.record_attempt(intent_id="intent:one", attempt=1, programme=programme, outcome="failed")
    fc.record_attempt(intent_id="intent:one", attempt=2, programme=programme, outcome="succeeded")

    entries = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [entry["attempt"] for entry in entries] == [1, 2]
    assert [entry["outcome"] for entry in entries] == ["failed", "succeeded"]
    assert entries[0]["intent_id"] == "intent:one"
