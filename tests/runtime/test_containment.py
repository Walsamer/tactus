"""Containment: the rendered boundary, and the denials proven with real children.

Two kinds of test live here, split by what they actually need:

* **Policy data** — the rendered profile, its exact write allowlist, the
  recorded allowances, the policy hash, the launcher pin and every refusal path
  that needs no child. These run on every host, because a renderer or a refusal
  is not a platform feature.
* **Real children** — the denial matrix, which spawns processes through
  ``sandbox-exec``, and the marker sweep, which reads the macOS process table.
  These carry ``seatbelt`` / ``macos_process_table`` and run only where that
  boundary exists (``tests/conftest.py`` states the rule once).

The second group is the reason the boundary is a claim with evidence rather than
an assertion: a policy that does not apply, or a launcher that ignores its
profile, fails one of these checks — which is why they are verified on macOS in
CI rather than excluded from it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from tactus.runtime.containment import (
    ContainmentError,
    ContainmentPolicy,
    ContainmentUnavailable,
    DeniedRoot,
    EgressPolicy,
    Launcher,
    digest_tree,
    residual_processes,
    run_self_test,
    which_launcher,
)

SANDBOX = shutil.which("sandbox-exec")


def _policy(launcher: Launcher, **kwargs: object) -> ContainmentPolicy:
    return ContainmentPolicy(launcher=launcher, **kwargs)  # type: ignore[arg-type]


# -- egress ----------------------------------------------------------------


@pytest.mark.parametrize("mode", ["deny", "localhost", "open"])
def test_egress_modes_are_the_only_modes(mode: str) -> None:
    EgressPolicy(mode=mode, port=11434 if mode == "localhost" else None)


def test_egress_rejects_an_unknown_mode() -> None:
    with pytest.raises(ContainmentError, match="egress mode"):
        EgressPolicy(mode="allow-some")


def test_egress_localhost_requires_a_port() -> None:
    with pytest.raises(ContainmentError, match="port"):
        EgressPolicy(mode="localhost")
    with pytest.raises(ContainmentError, match="port"):
        EgressPolicy(mode="localhost", port=70000)


def test_egress_modes_other_than_localhost_refuse_a_port() -> None:
    with pytest.raises(ContainmentError, match="must not declare a port"):
        EgressPolicy(mode="deny", port=80)


def test_egress_sbpl_is_explicit_per_mode() -> None:
    assert EgressPolicy().sbpl() == "(deny network*)"
    assert (
        EgressPolicy(mode="localhost", port=11434).sbpl()
        == '(allow network-outbound (remote ip "localhost:11434"))'
    )
    assert EgressPolicy(mode="open").sbpl() == "(allow network*)"


# -- launcher --------------------------------------------------------------


@pytest.mark.seatbelt
def test_launcher_verify_returns_the_observed_digest(seatbelt_launcher: Launcher) -> None:
    digest = seatbelt_launcher.verify()
    assert len(digest) == 64


def test_a_launcher_pinned_by_path_alone_is_refused(pinned_launcher: Launcher) -> None:
    """A path-only pin can be replaced in place, which is the swap we must stop."""
    unpinned = Launcher(
        path=pinned_launcher.path, argv_prefix=pinned_launcher.argv_prefix
    )
    with pytest.raises(ContainmentUnavailable, match="no pinned digest"):
        unpinned.verify()
    with pytest.raises(ContainmentError, match="pinned by digest"):
        ContainmentPolicy(launcher=unpinned)


def test_launcher_digest_mismatch_fails_closed(pinned_launcher: Launcher) -> None:
    drifted = Launcher(
        path=pinned_launcher.path,
        argv_prefix=pinned_launcher.argv_prefix,
        sha256="0" * 64,
    )
    with pytest.raises(ContainmentUnavailable, match="digest mismatch"):
        drifted.verify()


def test_launcher_requires_an_absolute_path() -> None:
    with pytest.raises(ContainmentError, match="absolute"):
        Launcher(path=Path("sandbox-exec"), argv_prefix=("sandbox-exec",))


def test_launcher_prefix_must_name_the_launcher_itself(pinned_launcher: Launcher) -> None:
    """A descriptor may not verify one launcher and run another."""
    launcher = Launcher(path=pinned_launcher.path, argv_prefix=("/bin/sh",))
    with pytest.raises(ContainmentError, match="not the pinned launcher"):
        launcher.verify()


def test_launcher_missing_fails_closed(tmp_path: Path) -> None:
    launcher = Launcher(path=tmp_path / "absent", argv_prefix=(str(tmp_path / "absent"),))
    with pytest.raises(ContainmentUnavailable, match="not found"):
        launcher.verify()


# -- denied roots and workspace validation ---------------------------------


def test_denied_root_covers_itself_and_children(tmp_path: Path) -> None:
    root = DeniedRoot(path=tmp_path / "home", reason="operator home")
    assert root.covers(tmp_path / "home")
    assert root.covers(tmp_path / "home" / "projects" / "x")
    assert not root.covers(tmp_path / "elsewhere")


def test_denied_root_requires_a_reason(tmp_path: Path) -> None:
    with pytest.raises(ContainmentError, match="why it is denied"):
        DeniedRoot(path=tmp_path, reason="  ")


def test_workspace_inside_a_denied_root_is_refused(
    tmp_path: Path, pinned_launcher: Launcher
) -> None:
    home = tmp_path / "home"
    workspace = home / ".ultimate" / "worktrees" / "x"
    workspace.mkdir(parents=True)
    policy = _policy(
        pinned_launcher, denied_roots=(DeniedRoot(path=home, reason="operator home"),)
    )
    with pytest.raises(ContainmentError, match="inside the denied root"):
        policy.validate_workspace(workspace)


def test_runtime_inside_a_denied_root_is_refused(
    tmp_path: Path, pinned_launcher: Launcher
) -> None:
    home = tmp_path / "home"
    runtime = home / ".codex" / "bin" / "codex"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("#!/bin/sh\n", encoding="utf-8")
    policy = _policy(
        pinned_launcher, denied_roots=(DeniedRoot(path=home, reason="operator home"),)
    )
    with pytest.raises(ContainmentError, match="copy it outside"):
        policy.validate_readable(runtime, what="runtime")


# -- rendered profile ------------------------------------------------------


def test_profile_is_deny_by_default_with_an_exact_write_allowlist(
    tmp_path: Path, pinned_launcher: Launcher
) -> None:
    workspace = tmp_path / "ws"
    attempt = tmp_path / "state" / "attempt"
    workspace.mkdir(parents=True)
    attempt.mkdir(parents=True)
    denied = tmp_path / "home"
    denied.mkdir()
    policy = _policy(
        pinned_launcher, denied_roots=(DeniedRoot(path=denied, reason="home"),)
    )
    text = policy.profile_text(workspace=workspace, attempt_dir=attempt)
    assert "(deny default)" in text
    assert "(allow process-exec* process-fork)" in text
    assert f'(deny file-read* (subpath "{denied}"))' in text
    assert f'(subpath "{workspace}")' in text
    assert f'(subpath "{attempt}")' in text
    assert "(deny network*)" in text
    # No blanket write permission anywhere.
    assert "(allow file-write*)" not in text
    # Deterministic: the same inputs render the same bytes.
    assert text == policy.profile_text(workspace=workspace, attempt_dir=attempt)


def test_profile_records_declared_allowances_verbatim(
    tmp_path: Path, pinned_launcher: Launcher
) -> None:
    workspace = tmp_path / "ws"
    attempt = tmp_path / "attempt"
    workspace.mkdir()
    attempt.mkdir()
    policy = _policy(pinned_launcher, allowances=("ipc-posix-shm",))
    text = policy.profile_text(workspace=workspace, attempt_dir=attempt)
    assert "(allow ipc-posix-shm)" in text


def test_profile_hash_changes_when_the_policy_changes(
    tmp_path: Path, pinned_launcher: Launcher
) -> None:
    workspace = tmp_path / "ws"
    attempt = tmp_path / "attempt"
    workspace.mkdir()
    attempt.mkdir()
    denied = _policy(pinned_launcher, egress=EgressPolicy())
    localhost = _policy(
        pinned_launcher, egress=EgressPolicy(mode="localhost", port=11434)
    )
    assert denied.profile_sha256(
        workspace=workspace, attempt_dir=attempt
    ) != localhost.profile_sha256(workspace=workspace, attempt_dir=attempt)


# -- artifact tree digest --------------------------------------------------


def test_tree_digest_is_order_independent_and_change_sensitive(tmp_path: Path) -> None:
    tree = tmp_path / "runtime"
    (tree / "bin").mkdir(parents=True)
    (tree / "bin" / "a").write_text("a\n", encoding="utf-8")
    (tree / "b").write_text("b\n", encoding="utf-8")
    first = digest_tree(tree)
    (tree / "bin" / "a").write_text("changed\n", encoding="utf-8")
    second = digest_tree(tree)
    assert first[1] == second[1] == 2
    assert first[0] != second[0]


def test_tree_digest_ignores_enumeration_order(tmp_path: Path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    for tree in (one, two):
        tree.mkdir()
    (one / "a").write_text("1\n", encoding="utf-8")
    (one / "z").write_text("2\n", encoding="utf-8")
    (two / "z").write_text("2\n", encoding="utf-8")
    (two / "a").write_text("1\n", encoding="utf-8")
    assert digest_tree(one) == digest_tree(two)


def test_tree_digest_requires_a_directory(tmp_path: Path) -> None:
    with pytest.raises(ContainmentError, match="not a directory"):
        digest_tree(tmp_path / "absent")


# -- residual process sweep ------------------------------------------------


@pytest.mark.macos_process_table
def test_residual_processes_finds_a_detached_child_then_nothing() -> None:
    marker = f"containment-probe-{os.getpid()}-{int(time.time() * 1000)}"
    env = dict(os.environ)
    env["TACTUS_ATTEMPT_DIR"] = marker
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=env,
        start_new_session=True,
    )
    try:  # wait for the child to appear in the process table
        deadline = time.time() + 5
        found = ()
        while time.time() < deadline and not found:
            found = residual_processes(marker)
            if not found:
                time.sleep(0.05)
        assert found, "the sweep must see a live child carrying the attempt marker"
        assert found[0].pid == proc.pid
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    deadline = time.time() + 5
    while time.time() < deadline and residual_processes(marker):
        time.sleep(0.05)
    assert residual_processes(marker) == ()


def test_residual_processes_requires_a_marker() -> None:
    with pytest.raises(ContainmentError, match="attempt marker"):
        residual_processes("   ")


# -- the denial matrix, run for real --------------------------------------


@pytest.fixture()
def containment_paths(tmp_path: Path) -> tuple[Path, Path, DeniedRoot]:
    workspace = tmp_path / "ws"
    attempt = tmp_path / "state" / "attempts" / "a1"
    denied = tmp_path / "home"
    for directory in (workspace, attempt, denied):
        directory.mkdir(parents=True)
    (denied / ".gitconfig").write_text("[user]\n", encoding="utf-8")
    return workspace, attempt, DeniedRoot(path=denied, reason="operator home")


@pytest.mark.seatbelt
def test_self_test_proves_every_denial(
    containment_paths: tuple[Path, Path, DeniedRoot], seatbelt_launcher: Launcher
) -> None:
    workspace, attempt, denied = containment_paths
    result = run_self_test(
        _policy(seatbelt_launcher, denied_roots=(denied,)),
        workspace=workspace,
        attempt_dir=attempt,
    )
    names = {check.name: check for check in result.checks}
    assert result.ok, result.as_dict()
    assert {"write_allowed_in_workspace", "write_denied_outside", "read_denied_root",
            "connect_denied", "nested_widen_denied", "foreign_signal_denied"} <= set(names)
    for name in names:
        assert names[name].ok, names[name].as_dict()
    assert result.profile_sha256 and len(result.profile_sha256) == 64
    # The probe writes are cleaned up: the workspace is not left dirty.
    assert not (workspace / ".containment-write-probe").exists()


@pytest.mark.seatbelt
def test_self_test_catches_a_launcher_that_does_not_apply_the_profile(
    containment_paths: tuple[Path, Path, DeniedRoot], tmp_path: Path
) -> None:
    """A launcher that ignores its profile must fail the matrix, not pass it."""
    workspace, attempt, denied = containment_paths
    fake = tmp_path / "fake-launcher.sh"
    fake.write_text("#!/bin/sh\nexec \"$@\"\n", encoding="utf-8")
    fake.chmod(0o755)
    policy = ContainmentPolicy(
        launcher=Launcher.for_path(fake, (str(fake),)),
        denied_roots=(denied,),
    )
    result = run_self_test(policy, workspace=workspace, attempt_dir=attempt / "fake")
    by_name = {check.name: check for check in result.checks}
    assert not result.ok
    assert not by_name["write_denied_outside"].ok
    assert not by_name["read_denied_root"].ok


@pytest.mark.seatbelt
def test_self_test_records_the_localhost_allowance_as_two_checks(
    containment_paths: tuple[Path, Path, DeniedRoot], seatbelt_launcher: Launcher
) -> None:
    workspace, attempt, denied = containment_paths
    result = run_self_test(
        _policy(
            seatbelt_launcher,
            denied_roots=(denied,),
            egress=EgressPolicy(mode="localhost", port=11434),
        ),
        workspace=workspace,
        attempt_dir=attempt,
    )
    by_name = {check.name: check for check in result.checks}
    assert result.ok, result.as_dict()
    assert by_name["connect_allowed_localhost_port"].ok
    assert by_name["connect_denied_other_port"].ok


@pytest.mark.seatbelt
def test_self_test_runs_the_runtime_probe_through_the_launcher(
    containment_paths: tuple[Path, Path, DeniedRoot], seatbelt_launcher: Launcher
) -> None:
    workspace, attempt, denied = containment_paths
    result = run_self_test(
        _policy(seatbelt_launcher, denied_roots=(denied,)),
        workspace=workspace,
        attempt_dir=attempt,
        runtime_probe_argv=("/bin/echo", "probe-ok"),
    )
    by_name = {check.name: check for check in result.checks}
    assert by_name["runtime_probe_through_launcher"].ok
    assert by_name["runtime_probe_through_launcher"].detail.strip() == "probe-ok"


@pytest.mark.seatbelt
def test_self_test_fails_when_the_probe_cannot_run_through_the_launcher(
    containment_paths: tuple[Path, Path, DeniedRoot], seatbelt_launcher: Launcher
) -> None:
    workspace, attempt, denied = containment_paths
    result = run_self_test(
        _policy(seatbelt_launcher, denied_roots=(denied,)),
        workspace=workspace,
        attempt_dir=attempt,
        runtime_probe_argv=("/bin/false",),
    )
    by_name = {check.name: check for check in result.checks}
    assert not by_name["runtime_probe_through_launcher"].ok
    assert not result.ok


def test_which_launcher_reports_the_host_tool_when_present() -> None:
    launcher = which_launcher()
    if SANDBOX is None:
        assert launcher is None
    else:
        assert launcher is not None
        assert launcher.argv_prefix[0] == SANDBOX
