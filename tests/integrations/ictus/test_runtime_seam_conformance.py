"""Conformance of Tactus's runtime backend against Ictus's *published* seam contract.

Nothing here reads a copy of the contract, a schema we vendored, or our own
transcription of a rule. Every assertion is made against the modules that ship
in the pinned Ictus revision (``PINNED_ICTUS_COMMIT``), loaded by
:mod:`seam_support`: the real ``RuntimeBackend`` protocol, the real
``validate_evidence``, the real failure-kind vocabulary and the real
``out_of_scope`` rule.

That is deliberate. A conformance test that validates against our reading of a
contract can only ever confirm that we are self-consistent; it fails to notice
the contract moving underneath us, which is the one thing it exists to notice.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

import seam_support

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_PATH = REPO_ROOT / "integrations" / "dagster" / "agent_backend.py"
SHIPPED_PIN = REPO_ROOT / "config" / "agent-runtime.json"
FAKE_AGENT = REPO_ROOT / "tests" / "integrations" / "agents" / "fake_workspace_agent.py"

#: Supervisor statuses, and the seam outcome each one must become.
STATUS_OUTCOMES = {
    "success": "succeeded",
    "failed": "failed",
    "timeout": "timed_out",
    "cancelled": "cancelled",
    "invalid": "failed",
    "unterminated": "failed",
}


@pytest.fixture(scope="module")
def seam() -> dict[str, Any]:
    """The published seam modules, loaded from the pinned checkout.

    ``tests/integrations/ictus/conftest.py`` materialises the exact pinned
    revision and sets ``TACTUS_ICTUS_ROOT``, so this needs no resolver of its own.
    """

    root = os.environ.get("TACTUS_ICTUS_ROOT")
    assert root, "the pinned-checkout fixture must set TACTUS_ICTUS_ROOT"
    return seam_support.load_seam(Path(root) / "python")


@pytest.fixture(scope="module")
def backend_module() -> Any:
    """Tactus's backend, loaded by path the way Ictus's worker loads it by name."""

    spec = importlib.util.spec_from_file_location("tactus_agent_backend", BACKEND_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def backend(backend_module: Any) -> Any:
    return backend_module.create_backend()


def _summary(**overrides: Any) -> dict[str, Any]:
    """A supervisor account, in the shape ``agent_attempt._summary`` produces."""

    account: dict[str, Any] = {
        "status": "success",
        "attempt_id": "software.implement.attempt-1",
        "intent_id": "software.implement",
        "capability": "software.implement",
        "runtime": {
            "runtime_id": "fake-workspace-agent",
            "runtime_kind": "fake-canary",
            "adapter": "process.exec@1",
            "reported_version": "fake-workspace-agent 1.0.0",
        },
        "workspace": {"root": "/tmp/workspace", "allowed_scope": ["src"]},
        "process": {
            "attempt_dir": "/tmp/workspace/.tactus-runtime/attempts/a-1",
            "pid": 4242,
            "pgid": 4242,
            "exit_code": 0,
            "duration_ms": 1250.0,
            "timed_out": False,
            "cancelled": False,
            "cancel_reason": "",
        },
        "termination": {"confirmed": True, "signals": [], "alive_after": False},
        "artifacts": {
            "agent_status": "completed",
            "agent_summary": "edited",
            "changed_paths": ["src/example.py"],
            "declared_paths": ["src/example.py"],
            "patch_paths": ["src/example.py"],
            "patch_sha256": "a" * 64,
            "patch_bytes": 42,
            "patch_path": "",
        },
        "errors": [],
        "publication_authority": False,
        "isolation": "none (same OS account: no filesystem or network boundary)",
        "write_scope": {"verified": True},
    }
    account.update(overrides)
    return account


# -- the port ---------------------------------------------------------------


def test_backend_satisfies_the_published_port(backend: Any, seam: dict[str, Any]) -> None:
    """Structural conformance against the *real* protocol object.

    ``RuntimeBackend`` is ``@runtime_checkable``, so this is the same check
    ``resolve_backend`` performs before Ictus will use a backend at all.
    """

    port = seam["port"]
    assert isinstance(backend, port.RuntimeBackend), (
        "the backend does not satisfy the published RuntimeBackend protocol"
    )
    for method in ("describe_workspace", "execute", "verify"):
        assert callable(getattr(backend, method))


def test_attempt_request_carries_every_field_the_backend_reads(seam: dict[str, Any]) -> None:
    """The request the backend consumes is the one Ictus actually constructs.

    If Ictus renames or drops a field, this fails here rather than at run time in
    a deployment nobody is watching.
    """

    port = seam["port"]
    request = port.AttemptRequest(
        capability="software.implement",
        write_scope=("src", "tests"),
        wall_clock_seconds=600,
        step_budget=200,
        workspace=port.WorkspaceRef(descriptor_path="config/agent-runtime.json"),
    )
    assert request.capability == "software.implement"
    assert tuple(request.write_scope) == ("src", "tests")
    assert request.wall_clock_seconds == 600
    assert request.step_budget == 200
    assert request.workspace.descriptor_path == "config/agent-runtime.json"
    assert request.workspace.to_json() == {
        "descriptor_path": "config/agent-runtime.json"
    }


# -- evidence ---------------------------------------------------------------


@pytest.mark.parametrize(("status", "outcome"), sorted(STATUS_OUTCOMES.items()))
def test_evidence_is_accepted_unchanged_by_the_published_validator(
    backend_module: Any, seam: dict[str, Any], status: str, outcome: str
) -> None:
    """The translator's output must satisfy the real validator, not a copy of it."""

    evidence = seam["evidence"]
    document = backend_module._evidence(_summary(status=status), descriptor_path="/pin.json")
    assert evidence.validate_evidence(document) == document
    assert document["outcome"] == outcome
    assert document["outcome"] in evidence.OUTCOMES


@pytest.mark.parametrize("status", sorted(STATUS_OUTCOMES))
def test_failure_kind_is_null_exactly_when_the_attempt_succeeded(
    backend_module: Any, seam: dict[str, Any], status: str
) -> None:
    """Ictus refuses the wrong half of this pair, in both directions."""

    evidence = seam["evidence"]
    document = backend_module._evidence(_summary(status=status), descriptor_path="/pin.json")
    if document["outcome"] == "succeeded":
        assert document["failure_kind"] is None
        return
    assert document["failure_kind"] is not None
    assert document["failure_kind"] in evidence.FAILURE_KINDS
    assert evidence.category_for_failure_kind(document["failure_kind"]) in (
        evidence.FAILURE_CATEGORIES.values()
    )


def test_a_non_zero_exit_is_the_process_failing_not_the_backend(
    backend_module: Any, seam: dict[str, Any]
) -> None:
    """Two different facts, deliberately not merged into one kind."""

    exit_2 = backend_module._evidence(
        _summary(status="failed", process={**_summary()["process"], "exit_code": 2}),
        descriptor_path="/pin.json",
    )
    assert exit_2["failure_kind"] == "PROCESS_EXIT"
    no_exit = backend_module._evidence(
        _summary(status="failed", process={**_summary()["process"], "exit_code": 0}),
        descriptor_path="/pin.json",
    )
    assert no_exit["failure_kind"] == "BACKEND_ERROR"
    seam["evidence"].validate_evidence(exit_2)
    seam["evidence"].validate_evidence(no_exit)


def test_digest_is_reported_in_the_contract_form_and_absent_when_there_is_no_patch(
    backend_module: Any,
) -> None:
    with_patch = backend_module._evidence(_summary(), descriptor_path="/pin.json")
    assert with_patch["patch_digest"] == f"sha256:{'a' * 64}"
    without = backend_module._evidence(
        _summary(
            artifacts={**_summary()["artifacts"], "patch_sha256": "", "patch_bytes": 0}
        ),
        descriptor_path="/pin.json",
    )
    assert without["patch_digest"] is None


def test_termination_requested_and_confirmed_survive_the_translation(
    backend_module: Any, seam: dict[str, Any]
) -> None:
    """A confirmed kill is evidence; an unconfirmed one must never look like it."""

    killed = backend_module._evidence(
        _summary(
            status="cancelled",
            process={**_summary()["process"], "exit_code": None, "cancel_reason": "wall_clock"},
            termination={"confirmed": True, "signals": ["SIGTERM", "SIGKILL"], "alive_after": False},
        ),
        descriptor_path="/pin.json",
    )
    assert killed["termination"] == {
        "requested": True,
        "signals": ["SIGTERM", "SIGKILL"],
        "confirmed": True,
    }
    unconfirmed = backend_module._evidence(
        _summary(
            status="unterminated",
            termination={"confirmed": False, "signals": ["SIGTERM"], "alive_after": True},
        ),
        descriptor_path="/pin.json",
    )
    assert unconfirmed["termination"]["confirmed"] is False
    assert unconfirmed["failure_kind"] == "TERMINATION_UNCONFIRMED"
    seam["evidence"].validate_evidence(killed)
    seam["evidence"].validate_evidence(unconfirmed)


def test_a_signal_death_is_reported_as_a_signal_not_as_an_exit_code(
    backend_module: Any, seam: dict[str, Any]
) -> None:
    """``Popen.returncode`` is negative exactly when the child died by a signal.

    Reporting that number as an exit code would be false in both halves of the
    pair Ictus defines, and ``finalize`` copies the pair into the summary a
    control plane reads.
    """

    killed = backend_module._evidence(
        _summary(
            status="timeout",
            process={**_summary()["process"], "exit_code": -15, "timed_out": True},
        ),
        descriptor_path="/pin.json",
    )
    assert killed["exit_status"] == {"code": None, "signal": "SIGTERM"}
    seam["evidence"].validate_evidence(killed)
    # A plain non-zero exit is still an exit code.
    exited = backend_module._evidence(
        _summary(status="failed", process={**_summary()["process"], "exit_code": 2}),
        descriptor_path="/pin.json",
    )
    assert exited["exit_status"] == {"code": 2, "signal": None}


def test_an_attempt_that_never_started_still_reports_valid_evidence(
    backend_module: Any, seam: dict[str, Any]
) -> None:
    """A refusal must be a typed fact, not a bare exception Ictus calls BACKEND_ERROR."""

    cancelled = backend_module._halted_evidence(status="cancelled", reason="pre-cancelled")
    assert cancelled["outcome"] == "cancelled"
    assert cancelled["failure_kind"] == "CANCEL_REQUESTED"
    assert cancelled["steps_used"] == 0
    assert cancelled["termination"]["confirmed"] is True, (
        "nothing ran, so nothing needs confirming — which is not the same as a "
        "confirmed kill, and the outcome says which happened"
    )
    seam["evidence"].validate_evidence(cancelled)


# -- describe_workspace -----------------------------------------------------


def _pin_file(tmp_path: Path) -> Path:
    """A real pin in the narrowed identity-only shape (no scope, no deadline)."""

    shipped = json.loads(SHIPPED_PIN.read_text(encoding="utf-8"))
    document = {
        "schema_version": shipped["schema_version"],
        "runtime_id": shipped["runtime_id"],
        "runtime_kind": shipped["runtime_kind"],
        "adapter": shipped["adapter"],
        "executable": str(FAKE_AGENT),
        "executable_sha256": hashlib.sha256(FAKE_AGENT.read_bytes()).hexdigest(),
        "argv": shipped["argv"],
        "preflight_argv": shipped["preflight_argv"],
        "expected_version_token": shipped["expected_version_token"],
        "runtime_caps": shipped["runtime_caps"],
        "purpose": "conformance fixture",
    }
    # The narrowing this fixture rides on is asserted against the committed pin,
    # not against the dict built three lines above.
    shipped = json.loads(SHIPPED_PIN.read_text(encoding="utf-8"))
    assert "runtime_caps" in shipped
    assert "allowed_scope" not in shipped
    assert "limits" not in shipped
    path = tmp_path / "agent-runtime.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_describe_workspace_reports_a_probe_the_way_ictus_requires(
    tmp_path: Path, backend: Any, backend_module: Any, seam: dict[str, Any]
) -> None:
    """The report is what Ictus records instead of opening the descriptor itself."""

    pin = _pin_file(tmp_path)
    report = backend.describe_workspace(
        seam["port"].WorkspaceRef(descriptor_path=str(pin))
    )
    assert {"executable", "version_token", "probe"} <= set(report)
    assert isinstance(report["executable"], str) and report["executable"]
    assert isinstance(report["version_token"], str)
    assert report["probe"]["ok"] is True, report["probe"]["detail"]
    assert isinstance(report["probe"]["detail"], str)


def test_a_broken_pin_is_reported_as_a_failed_probe_not_raised(
    tmp_path: Path, backend: Any, seam: dict[str, Any]
) -> None:
    """``probe.ok = false`` is how Ictus learns WORKSPACE_PROBE_FAILED, with a reason."""

    broken = tmp_path / "broken.json"
    broken.write_text('{"schema_version": 1}', encoding="utf-8")
    report = backend.describe_workspace(
        seam["port"].WorkspaceRef(descriptor_path=str(broken))
    )
    assert report["probe"]["ok"] is False
    assert report["probe"]["detail"], "a failed probe with no reason is useless evidence"


# -- containment: three properties, not unconditional agreement --------------
#
# Two implementations now stand between an agent and a write it may not make.
# Ours (``tactus.runtime.workspace``) normalises with ``posixpath.normpath`` and
# refuses absolute paths and escapes by raising. Ictus's
# (``seam.evidence.out_of_scope``) is purely syntactic: it never resolves
# anything, refuses any ``..`` segment outright, and compares a path to a scope
# entry by string prefix.
#
# Neither is authoritative for both systems, and we do not pretend they are
# equivalent. Instead these tests pin the three properties that matter:
# agreement wherever the two see the same thing, an explicit record of where
# they differ on purpose, and the safety invariant that holds because of *what
# we report*, not because the normalisers happen to match.

#: Paths where both implementations must reach the same verdict.
AGREEMENT_CORPUS = (
    "src/a.py",  # plainly in scope
    "src/nested/b.py",  # nested
    "srcx/c.py",  # shares the prefix but is not a subpath
    "src/../etc/passwd",  # escapes by collapsing out of the scope
    "../outside",  # escapes outright
    "src/./d.py",  # a '.' component optimistically inside the scope
    "src/",  # trailing slash
    "src",  # equal to the scope entry itself
    "Src/a.py",  # case-only difference: this repository is case-sensitive
    "/abs/path.py",  # absolute: Ictus has no workspace root, we refuse absolutes
    "src//e.py",  # doubled separator
)

#: Paths where the two differ *on purpose*, with the reason. Each entry is
#: asserted by name so that a new divergence fails rather than passing silently.
INTENTIONAL_DIFFERENCES = {
    "src/../src/a.py": (
        "ours accepts: posixpath.normpath collapses 'src/../src/a.py' to 'src/a.py', "
        "which is inside the scope. Ictus refuses any '..' segment outright because "
        "it never resolves a path and will not pretend to name a target it cannot "
        "see. Same file, two honest readings of the same string."
    ),
}


def _ours(workspace: Any, path: str) -> str:
    """Our verdict for one path, as a verdict rather than an exception."""

    from tactus.runtime.workspace import WorkspaceScopeError

    try:
        return "accept" if workspace.covers(path) else "reject"
    except WorkspaceScopeError:
        return "reject"


def _theirs(evidence: Any, path: str, scope: tuple[str, ...]) -> str:
    return "reject" if evidence.out_of_scope([path], scope) else "accept"


@pytest.fixture()
def scope_workspace(tmp_path: Path) -> Any:
    from tactus.runtime.workspace import Workspace

    return Workspace(root=tmp_path, allowed_scope=("src",))


def test_property_1_both_implementations_agree_where_they_see_the_same_thing(
    scope_workspace: Any, seam: dict[str, Any]
) -> None:
    evidence = seam["evidence"]
    disagreements = [
        (path, _ours(scope_workspace, path), _theirs(evidence, path, ("src",)))
        for path in AGREEMENT_CORPUS
        if _ours(scope_workspace, path) != _theirs(evidence, path, ("src",))
    ]
    assert not disagreements, (
        "our scope rule and Ictus's disagree on paths neither treats as an "
        f"intentional difference: {disagreements}"
    )


@pytest.mark.parametrize("path", sorted(INTENTIONAL_DIFFERENCES))
def test_property_2_each_intentional_difference_is_named_and_explained(
    path: str, scope_workspace: Any, seam: dict[str, Any]
) -> None:
    evidence = seam["evidence"]
    ours = _ours(scope_workspace, path)
    theirs = _theirs(evidence, path, ("src",))
    assert ours != theirs, (
        f"{path} now agrees ({ours}); its entry in INTENTIONAL_DIFFERENCES is stale "
        f"and must be deleted. Recorded reason: {INTENTIONAL_DIFFERENCES[path]}"
    )
    # Not just "they differ" — the direction is the part that matters.
    assert (ours, theirs) == ("accept", "reject"), (
        f"{path}: ours={ours} theirs={theirs}. The recorded difference is that we "
        f"are *more permissive* here; a stricter Ictus would be a different fact. "
        f"Recorded reason: {INTENTIONAL_DIFFERENCES[path]}"
    )


def test_property_3_tactus_never_reports_a_path_ictus_prohibits(
    scope_workspace: Any, seam: dict[str, Any], backend_module: Any
) -> None:
    """The safety invariant, stated over what actually crosses the seam.

    Ictus never sees the string an agent typed: it sees the ``changed_paths``
    our supervisor reported. So the invariant is not "the two normalisers
    agree" — they do not, and property 2 records where. It is: *anything we
    accept, in the form we report it, is inside Ictus's scope.*

    That holds because the supervisor normalises before it reports, and this
    test pins both halves of that chain rather than assuming it: the translation
    passes the supervisor's paths through unchanged, and the supervisor's own
    entry point normalises the one path where we are more permissive than Ictus.
    """

    from tactus.runtime.workspace import (
        WorkspaceScopeError,
        normalize_changed_path,
        validate_changed_paths,
    )

    evidence = seam["evidence"]

    # Half one: the translation must not reshape what the supervisor reported.
    reported = ["src/a.py", "src/nested/b.py"]
    summary = _summary(artifacts={**_summary()["artifacts"], "changed_paths": reported})
    document = backend_module._evidence(summary, descriptor_path="/pin.json")
    assert document["changed_paths"] == reported

    # Half two: every path we accept is reported in a form Ictus accepts.
    violations = []
    for path in (*AGREEMENT_CORPUS, *INTENTIONAL_DIFFERENCES):
        if _ours(scope_workspace, path) != "accept":
            continue
        try:
            normalized = normalize_changed_path(path)
        except WorkspaceScopeError:  # pragma: no cover - we only accept valid paths
            continue
        # The supervisor's own entry point is what turns an accepted path into a
        # reported one; assert it agrees with the bare normaliser here.
        accepted = validate_changed_paths([path], workspace=scope_workspace, max_paths=20)
        assert accepted == (normalized,)
        if evidence.out_of_scope([accepted[0]], ("src",)):
            violations.append((path, accepted[0]))
    assert not violations, (
        "Tactus accepted a path and reported a form the Ictus scope contract "
        f"prohibits: {violations}"
    )
