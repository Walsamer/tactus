"""Bounded real-boundary conformance harness for the Tactus Ictus snapshots.

This harness runs the deterministic Tactus ``StateSnapshot`` fixtures through
the **actual** pinned Ictus binary (``ictus decide``) instead of a Python mirror
of Ictus policy. It is the cross-repository half of the Ictus adapter evidence:

* it locates the reviewed Ictus checkout (``TACTUS_ICTUS_ROOT``, then known
  local checkout names) and verifies its exact ``HEAD`` revision;
* it builds the ``ictus`` binary from that revision into a disposable target
  directory (``cargo build --offline --locked``), so no canonical checkout is
  mutated and no vendored source is needed;
* it feeds a snapshot payload to ``ictus decide`` over stdin/stdout and returns
  the real ``{proposal, policy_decision, execution_intent}`` envelope.

The pinned Ictus policy reads the legacy ``retry.attempt``/``retry.budget``
keys. :func:`tactus.integrations.ictus.to_ictus_legacy_boundary_payload` is the
explicit compatibility projection that maps the canonical semantic count/limit
onto those keys; this harness never reimplements ``count < limit`` itself.

Everything here is read-only with respect to the Ictus checkout and bounded in
time via process timeouts. It is **not** part of the Tactus runtime: it exists
only for conformance evidence. When the checkout is missing or off-pin the
harness raises :class:`IctusBoundaryUnavailable` / :class:`IctusPinMismatch`
instead of skipping, so drift is a visible failure.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

#: Exact reviewed Ictus revision this harness validates against.
PINNED_ICTUS_COMMIT = "e823000c9255b959c2c9516ed4ee85eb13150df2"

#: Repository that owns the pinned revision (documentation only).
PINNED_ICTUS_REPOSITORY = "Walsamer/ictus"

#: Environment variable that names the Ictus checkout explicitly.
ICTUS_ROOT_ENV = "TACTUS_ICTUS_ROOT"

#: Environment variable that pins a reusable build target directory.
ICTUS_TARGET_ENV = "TACTUS_ICTUS_TARGET_DIR"

#: Local checkout directory names tried when no root is named explicitly.
_DEFAULT_CHECKOUT_NAMES = ("agentic-control", "ictus")

#: Files that must exist for a directory to look like the Ictus checkout.
_ICTUS_MARKERS = (
    "Cargo.toml",
    "crates/bridge/src/main.rs",
    "contracts/state-snapshot.schema.json",
)

#: ``ictus decide`` exit codes that still carry a valid JSON envelope: ``0``
#: success/executable, ``3`` a non-executable policy decision.
_VALID_DECIDE_RETURNCODES = frozenset({0, 3})


class IctusBoundaryUnavailable(RuntimeError):
    """The pinned Ictus checkout or a tool needed to run it is unavailable."""


class IctusPinMismatch(IctusBoundaryUnavailable):
    """The located Ictus checkout is not at the reviewed pinned revision."""


@dataclass(frozen=True, slots=True)
class IctusBoundary:
    """A verified, built Ictus boundary ready to decide."""

    root: Path
    commit: str
    binary: Path

    def decide(
        self,
        payload: Mapping[str, Any],
        *,
        args: Sequence[str] = ("decide",),
        timeout: float = 60.0,
    ) -> "DecideOutcome":
        return run_decide(self.binary, payload, args=args, timeout=timeout)


@dataclass(frozen=True, slots=True)
class DecideOutcome:
    """The real Ictus decision envelope plus its process return code."""

    returncode: int
    proposal: Mapping[str, Any]
    policy_decision: Mapping[str, Any]
    execution_intent: Mapping[str, Any] | None
    raw: Mapping[str, Any]

    @property
    def proposal_decision(self) -> str:
        return str(self.proposal.get("decision"))

    @property
    def policy_decision_kind(self) -> str:
        return str(self.policy_decision.get("decision"))

    @property
    def executable(self) -> bool:
        return self.execution_intent is not None

    @property
    def reason(self) -> str:
        return str(self.proposal.get("reason") or "")

    @property
    def subject_id(self) -> str:
        raw_subject = self.proposal.get("subject")
        if isinstance(raw_subject, Mapping):
            return str(raw_subject.get("id"))
        return ""

    @property
    def subject_type(self) -> str:
        raw_subject = self.proposal.get("subject")
        if isinstance(raw_subject, Mapping):
            return str(raw_subject.get("type"))
        return ""


def _looks_like_ictus_checkout(root: Path) -> bool:
    return all((root / marker).is_file() for marker in _ICTUS_MARKERS)


def _candidate_roots() -> list[Path]:
    """Ancestor directories that may contain a sibling Ictus checkout.

    The reviewed checkout lives beside the Tactus project (the Fleet records it
    as ``AGENTIC_CONTROL_REPO``), so walking the ancestors of this file and of
    the working directory finds it without a machine-specific absolute path.
    """

    candidates: list[Path] = []
    starts = {Path(__file__).resolve().parent, Path.cwd().resolve()}
    for start in starts:
        for base in (start, *start.parents):
            if base not in candidates:
                candidates.append(base)
    return candidates


def resolve_ictus_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Locate the pinned Ictus checkout or raise ``IctusBoundaryUnavailable``.

    Resolution order: the ``explicit`` argument, then ``TACTUS_ICTUS_ROOT``,
    then a sibling checkout named ``agentic-control``/``ictus`` under an
    ancestor of this repository.
    """

    named = explicit if explicit is not None else os.environ.get(ICTUS_ROOT_ENV)
    if named:
        root = Path(named).expanduser().resolve()
        if not _looks_like_ictus_checkout(root):
            raise IctusBoundaryUnavailable(
                f"'{root}' is not an Ictus checkout (missing one of "
                f"{', '.join(_ICTUS_MARKERS)})"
            )
        return root

    tried: list[str] = []
    for base in _candidate_roots():
        for name in _DEFAULT_CHECKOUT_NAMES:
            candidate = base / name
            tried.append(str(candidate))
            if _looks_like_ictus_checkout(candidate):
                return candidate

    raise IctusBoundaryUnavailable(
        "cannot locate the pinned Ictus checkout. Set "
        f"{ICTUS_ROOT_ENV} to the checkout at {PINNED_ICTUS_COMMIT} "
        f"({PINNED_ICTUS_REPOSITORY}). Tried: " + ", ".join(tried[:12])
    )


def verify_pinned_ictus(
    root: str | os.PathLike[str],
    expected: str = PINNED_ICTUS_COMMIT,
) -> str:
    """Return the checkout ``HEAD`` after verifying it equals ``expected``."""

    checkout = Path(root)
    try:
        proc = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30.0,
        )
    except FileNotFoundError as error:  # pragma: no cover - environment guard
        raise IctusBoundaryUnavailable("git is required to pin the Ictus revision") from error
    except subprocess.TimeoutExpired as error:  # pragma: no cover - environment guard
        raise IctusBoundaryUnavailable("git rev-parse timed out") from error

    if proc.returncode != 0:
        raise IctusBoundaryUnavailable(
            f"'{checkout}' is not a git checkout: {proc.stderr.strip()}"
        )
    head = proc.stdout.strip()
    if head != expected:
        raise IctusPinMismatch(
            f"Ictus checkout '{checkout}' is at {head!r}, expected the pinned "
            f"{expected!r} ({PINNED_ICTUS_REPOSITORY})"
        )
    return head


def _cargo_home_guesses(cargo_bin: Path) -> tuple[Path | None, Path | None]:
    """Best-effort ``(CARGO_HOME, RUSTUP_HOME)`` for a rustup shim layout."""

    cargo_home = cargo_bin.parent.parent
    rustup_candidates = (
        cargo_home.parent / ".rustup",
        cargo_home.parent / "rustup",
        Path.home() / ".rustup",
    )
    rustup_home = next(
        (candidate for candidate in rustup_candidates if (candidate / "toolchains").is_dir()),
        None,
    )
    if not (cargo_home / "registry").is_dir() and not (cargo_home / "bin").is_dir():
        return None, rustup_home
    return cargo_home, rustup_home


def cargo_environment(env: Mapping[str, str] | None = None) -> tuple[str, dict[str, str]]:
    """Return ``(cargo_executable, environment)`` with rustup homes resolved."""

    resolved_env = dict(os.environ if env is None else env)
    cargo = shutil.which("cargo", path=resolved_env.get("PATH"))
    if cargo is None:
        raise IctusBoundaryUnavailable(
            "cargo is required to build the pinned ictus binary; install the "
            "stable Rust toolchain or set PATH so 'cargo' resolves"
        )
    cargo_bin = Path(cargo).resolve()
    if not resolved_env.get("CARGO_HOME") or not resolved_env.get("RUSTUP_HOME"):
        cargo_home, rustup_home = _cargo_home_guesses(cargo_bin)
        if cargo_home is not None and not resolved_env.get("CARGO_HOME"):
            resolved_env["CARGO_HOME"] = str(cargo_home)
        if rustup_home is not None and not resolved_env.get("RUSTUP_HOME"):
            resolved_env["RUSTUP_HOME"] = str(rustup_home)
    cargo_dir = str(cargo_bin.parent)
    path_parts = resolved_env.get("PATH", "").split(os.pathsep)
    if cargo_dir not in path_parts:
        resolved_env["PATH"] = os.pathsep.join([cargo_dir, *path_parts])
    return cargo, resolved_env


def default_target_dir(commit: str = PINNED_ICTUS_COMMIT) -> Path:
    """A disposable, commit-keyed build target directory under the temp root."""

    configured = os.environ.get(ICTUS_TARGET_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path(tempfile.gettempdir()) / "tactus-ictus-conformance" / commit[:12]


def build_ictus(
    root: str | os.PathLike[str],
    *,
    target_dir: str | os.PathLike[str] | None = None,
    force: bool = False,
    timeout: float = 1200.0,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Build (or reuse) the pinned ``ictus`` binary in a disposable target dir."""

    checkout = Path(root)
    destination = Path(target_dir) if target_dir is not None else default_target_dir()
    binary = destination / "debug" / "ictus"
    if binary.is_file() and not force:
        return binary

    cargo, cargo_env = cargo_environment(env)
    cargo_env["CARGO_TARGET_DIR"] = str(destination)
    command = [
        cargo,
        "build",
        "--offline",
        "--locked",
        "--bin",
        "ictus",
        "--manifest-path",
        str(checkout / "Cargo.toml"),
    ]
    try:
        proc = subprocess.run(
            command,
            cwd=str(checkout),
            env=cargo_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise IctusBoundaryUnavailable(
            f"building the pinned ictus binary timed out after {timeout}s"
        ) from error

    if proc.returncode != 0 or not binary.is_file():
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-12:]
        raise IctusBoundaryUnavailable(
            "failed to build the pinned ictus binary from "
            f"'{checkout}' (cargo exit {proc.returncode}):\n" + "\n".join(tail)
        )
    return binary


def load_boundary(
    root: str | os.PathLike[str] | None = None,
    *,
    target_dir: str | os.PathLike[str] | None = None,
    force_build: bool = False,
    timeout: float = 1200.0,
) -> IctusBoundary:
    """Resolve, pin-verify and build the real Ictus boundary."""

    checkout = resolve_ictus_root(root)
    commit = verify_pinned_ictus(checkout)
    binary = build_ictus(
        checkout, target_dir=target_dir, force=force_build, timeout=timeout
    )
    return IctusBoundary(root=checkout, commit=commit, binary=binary)


def run_decide(
    binary: str | os.PathLike[str],
    payload: Mapping[str, Any],
    *,
    args: Sequence[str] = ("decide",),
    timeout: float = 60.0,
    env: Mapping[str, str] | None = None,
) -> DecideOutcome:
    """Run ``ictus decide`` on ``payload`` and return the real envelope."""

    command = [str(binary), *args]
    try:
        proc = subprocess.run(
            command,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(os.environ if env is None else env),
        )
    except subprocess.TimeoutExpired as error:
        raise IctusBoundaryUnavailable(
            f"ictus {' '.join(args)} timed out after {timeout}s"
        ) from error

    if proc.returncode not in _VALID_DECIDE_RETURNCODES:
        raise IctusBoundaryUnavailable(
            f"ictus {' '.join(args)} failed with exit {proc.returncode}: "
            + proc.stderr.strip()[:500]
        )
    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError as error:
        raise IctusBoundaryUnavailable(
            f"ictus {' '.join(args)} did not emit a JSON envelope: {error}"
        ) from error
    if not isinstance(raw, Mapping):
        raise IctusBoundaryUnavailable("ictus decide emitted a non-object envelope")

    proposal = raw.get("proposal")
    policy_decision = raw.get("policy_decision")
    if not isinstance(proposal, Mapping) or not isinstance(policy_decision, Mapping):
        raise IctusBoundaryUnavailable(
            "ictus decide envelope is missing proposal/policy_decision"
        )
    execution_intent = raw.get("execution_intent")
    if execution_intent is not None and not isinstance(execution_intent, Mapping):
        raise IctusBoundaryUnavailable("execution_intent must be an object or null")

    return DecideOutcome(
        returncode=proc.returncode,
        proposal=proposal,
        policy_decision=policy_decision,
        execution_intent=execution_intent,
        raw=raw,
    )


def _report_snapshot(boundary: IctusBoundary, path: Path) -> dict[str, Any]:
    from tactus.integrations.ictus import (
        to_ictus_legacy_boundary_payload,
        validate_snapshot_profile,
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_snapshot_profile(payload)
    projected = to_ictus_legacy_boundary_payload(payload)
    outcome = boundary.decide(projected)
    return {
        "snapshot": str(path),
        "snapshot_id": payload.get("snapshot_id"),
        "subject": payload.get("subject"),
        "proposal_decision": outcome.proposal_decision,
        "policy_decision": outcome.policy_decision_kind,
        "reason": outcome.reason,
        "executable": outcome.executable,
        "returncode": outcome.returncode,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: build the pinned boundary and report real decisions for snapshots."""

    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("snapshots", nargs="+", type=Path)
    parser.add_argument("--root", default=None, help=f"override {ICTUS_ROOT_ENV}")
    parser.add_argument("--target-dir", default=None, help="disposable cargo target dir")
    parser.add_argument("--force-build", action="store_true")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--keep-build", action="store_true", help="keep the target dir")
    args = parser.parse_args(argv)

    held_target = None
    target_dir = args.target_dir
    if target_dir is None and not args.keep_build:
        held_target = tempfile.mkdtemp(prefix="tactus-ictus-conformance-")
        target_dir = held_target

    try:
        boundary = load_boundary(
            args.root, target_dir=target_dir, force_build=args.force_build
        )
        print(
            f"pinned Ictus boundary: {boundary.commit} ({PINNED_ICTUS_REPOSITORY})\n"
            f"binary: {boundary.binary}"
        )
        reports = [_report_snapshot(boundary, path) for path in args.snapshots]
        print(json.dumps(reports, indent=2, sort_keys=True))
    finally:
        if held_target is not None:
            shutil.rmtree(held_target, ignore_errors=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - manual conformance command
    sys.exit(main())
