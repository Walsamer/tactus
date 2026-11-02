"""OS-level containment for one supervised agent attempt.

A coding agent is only bounded if the operating system — not the agent, and not
its own sandbox setting — denies what the boundary claims to deny. This module
owns that boundary on macOS: it renders a deny-default seatbelt profile from
data, pins the launcher that applies it, proves the denials with real child
processes before anything is spawned, and finds processes that outlive their
group.

What is enforced (each proven by a check in :func:`run_self_test`):

* writes only inside the workspace and the attempt directory (an allowlist);
* reads of the configured denied roots (``$HOME``, other volumes, credential
  stores) refused;
* Mach IPC denied by default, because ``(deny default)`` denies it, which is
  what closes the Keychain — a path-level denial alone does not;
* network egress denied, or limited to ``localhost:<port>``, or open — never
  silently anything else;
* a child cannot widen its own sandbox (``sandbox_apply`` fails inside one);
* a child cannot signal a process outside its sandbox.

What is *not* claimed: reads are a deny-list (everything minus the denied
roots), so any sensitive path outside the denied roots is readable. Callers must
pass every sensitive root they know about. ``sandbox-exec`` is deprecated by
Apple; an OS that drops it fails closed here rather than running uncontained.

Self-test children use system binaries (``/bin/sh``, ``/usr/bin/python3``,
``/usr/bin/sandbox-exec``) and never the repository, so the same checks hold
when the checkout lives inside a denied root.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = [
    "ContainmentError",
    "ContainmentUnavailable",
    "ContainmentPolicy",
    "DeniedRoot",
    "EgressPolicy",
    "FILE_READ_DENIED_MESSAGE",
    "Launcher",
    "ProcessRef",
    "SelfTestCheck",
    "SelfTestResult",
    "describe_host",
    "digest_tree",
    "host_build_label",
    "redact_process_command",
    "residual_processes",
    "run_self_test",
    "workspace_holders",
    "sweep_is_visible",
    "which_launcher",
]

EGRESS_MODES = ("deny", "localhost", "open")

#: A profile construct this module emits that a host may reject. Kept in one
#: place so the rendered profile is one string, testable on its own.
PROFILE_TEMPLATE_VERSION = 1

#: Marked in the child environment; the sweep uses it to find survivors. The
#: value is the *per-run* attempt directory name, not the logical attempt id: two
#: runs of the same intent must not share a marker, or a stale process from an
#: earlier run would block an unrelated one forever.
ATTEMPT_MARKER_VARIABLE = "TACTUS_ATTEMPT_DIR"

#: Files that exist on a default macOS install, in order of preference, used to
#: prove that a denied root is really unreadable. The first one that exists is
#: probed; if none exists the check is recorded as skipped, never as passed.
DENIED_READ_CANDIDATES = (
    ".ssh/id_ed25519",
    ".codex/auth.json",
    ".claude.json",
    ".gitconfig",
    ".netrc",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PLACEHOLDER_RE = re.compile(r"\{[a-z_]+\}")

FILE_READ_DENIED_MESSAGE = "Operation not permitted"


class ContainmentError(RuntimeError):
    """The requested containment cannot be built or is inconsistent."""


class ContainmentUnavailable(ContainmentError):
    """This host cannot provide the requested boundary (fail closed)."""


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    """Outbound network policy for the child.

    ``deny`` blocks everything; ``localhost`` allows exactly one local port (the
    shape a locally served model needs); ``open`` allows all egress and exists
    only for a bounded, operator-approved run — it is recorded as such.
    """

    mode: str = "deny"
    port: int | None = None

    def __post_init__(self) -> None:
        if self.mode not in EGRESS_MODES:
            raise ContainmentError(
                f"egress mode must be one of {list(EGRESS_MODES)}, not {self.mode!r}"
            )
        if self.mode == "localhost":
            if not isinstance(self.port, int) or not 1 <= self.port <= 65535:
                raise ContainmentError("localhost egress requires a port between 1 and 65535")
        elif self.port is not None:
            raise ContainmentError(
                f"egress mode {self.mode!r} must not declare a port"
            )

    @property
    def reaches_network(self) -> bool:
        return self.mode != "deny"

    def sbpl(self) -> str:
        if self.mode == "open":
            return "(allow network*)"
        if self.mode == "localhost":
            return f'(allow network-outbound (remote ip "localhost:{self.port}"))'
        return "(deny network*)"

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "port": self.port}


@dataclass(frozen=True, slots=True)
class DeniedRoot:
    """One path the child must not read, and why (the reason is evidence)."""

    path: Path
    reason: str

    def __post_init__(self) -> None:
        resolved = Path(self.path).expanduser().resolve()
        object.__setattr__(self, "path", resolved)
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ContainmentError("a denied root must record why it is denied")

    def covers(self, candidate: Path) -> bool:
        candidate = Path(candidate)
        return candidate == self.path or self.path in candidate.parents

    def as_dict(self) -> dict[str, str]:
        return {"path": str(self.path), "reason": self.reason}


@dataclass(frozen=True, slots=True)
class Launcher:
    """The pinned binary that applies the profile, and its fixed arguments.

    ``argv_prefix`` may contain the ``{profile}`` and ``{workspace}``
    placeholders; its first element must resolve to ``path``, so a descriptor
    cannot verify one launcher and run another. ``recorded_on`` names the host the
    digest was recorded on, so a mismatch names both sides of the comparison.
    """

    path: Path
    argv_prefix: tuple[str, ...]
    sha256: str = ""
    #: The host the digest was recorded on, e.g. ``macOS 27.2 (26B123)``. A
    #: preflight mismatch names both sides, so the next OS update explains itself.
    recorded_on: str = ""

    def __post_init__(self) -> None:
        path = Path(self.path).expanduser()
        if not path.is_absolute():
            raise ContainmentError("launcher path must be absolute")
        object.__setattr__(self, "path", path)
        prefix = tuple(self.argv_prefix)
        if not prefix:
            raise ContainmentError("launcher argv_prefix must not be empty")
        object.__setattr__(self, "argv_prefix", prefix)
        if self.sha256 and not _SHA256_RE.fullmatch(self.sha256):
            raise ContainmentError("launcher sha256 must be a lowercase SHA-256 digest")
        recorded_on = self.recorded_on.strip()
        if self.recorded_on and not recorded_on:
            raise ContainmentError("launcher recorded_on must name a host when it is set")
        object.__setattr__(self, "recorded_on", recorded_on)

    @classmethod
    def for_path(cls, path: str | Path, argv_prefix: Sequence[str] | None = None) -> "Launcher":
        """Record a launcher and its digest together (the only safe way to pin one)."""
        resolved = Path(path).expanduser()
        prefix = tuple(argv_prefix) if argv_prefix is not None else (str(resolved), "-f", "{profile}")
        return cls(
            path=resolved,
            argv_prefix=prefix,
            sha256=hashlib.sha256(Path(resolved).read_bytes()).hexdigest(),
        )

    def verify(self, *, require_digest: bool = True) -> str:
        """Return the launcher's digest, or raise if it is unusable or drifted.

        ``require_digest`` defaults to true: a launcher pinned by path alone can be
        replaced in place, which is exactly the swap the pin exists to stop.
        """
        path = Path(self.path)
        if not path.is_file():
            raise ContainmentUnavailable(f"containment launcher not found: {path}")
        if not os.access(path, os.X_OK):
            raise ContainmentUnavailable(f"containment launcher is not executable: {path}")
        resolved_prefix = Path(self.argv_prefix[0]).expanduser()
        if resolved_prefix.resolve() != path.resolve():
            raise ContainmentError(
                f"launcher argv_prefix[0] ({self.argv_prefix[0]}) is not the pinned "
                f"launcher ({path})"
            )
        if require_digest and not self.sha256:
            raise ContainmentUnavailable(
                f"containment launcher {path} has no pinned digest; a launcher pinned "
                "by path alone can be replaced in place"
            )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if self.sha256 and digest != self.sha256:
            raise ContainmentUnavailable(
                f"containment launcher digest mismatch: pinned {self.sha256} "
                f"recorded on {self.recorded_on or 'an unspecified host'}, observed "
                f"{digest} ({path}); this host is {host_build_label()}; re-record the "
                "launcher pin for this host only after confirming the Apple signature "
                "chain and the new OS build"
            )
        return digest

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "path": str(self.path),
            "argv_prefix": list(self.argv_prefix),
            "sha256": self.sha256,
        }
        if self.recorded_on:
            data["recorded_on"] = self.recorded_on
        return data


@dataclass(frozen=True, slots=True)
class ContainmentPolicy:
    """One rendered boundary: launcher, denied roots, egress, extra allowances."""

    launcher: Launcher
    denied_roots: tuple[DeniedRoot, ...] = ()
    egress: EgressPolicy = EgressPolicy()
    #: Mach/shm names the pinned runtime demonstrably needs. Recorded so an
    #: allowance can never grow silently.
    allowances: tuple[str, ...] = ()
    workspace: Path | None = None
    attempt_dir: Path | None = None

    def __post_init__(self) -> None:
        roots = tuple(self.denied_roots)
        for root in roots:
            if not isinstance(root, DeniedRoot):
                raise ContainmentError("denied roots must be DeniedRoot instances")
        object.__setattr__(self, "denied_roots", roots)
        for allowance in self.allowances:
            if not isinstance(allowance, str) or not allowance.strip():
                raise ContainmentError("allowances must be non-empty strings")
        if not self.launcher.sha256:
            raise ContainmentError(
                "a containment policy requires a launcher pinned by digest "
                "(Launcher.for_path records it); a path-only pin can be swapped in place"
            )

    # -- validation ------------------------------------------------------

    def validate_workspace(self, workspace_root: Path) -> None:
        """Refuse a workspace inside a denied root (deny wins over allow)."""
        workspace = Path(workspace_root).expanduser().resolve()
        for root in self.denied_roots:
            if root.covers(workspace):
                raise ContainmentError(
                    f"workspace {workspace} is inside the denied root {root.path} "
                    f"({root.reason}); the profile cannot allow it — put the "
                    "workspace outside every denied root"
                )

    def validate_readable(self, path: Path, *, what: str) -> None:
        """Refuse a path the profile would deny (the runtime must stay readable)."""
        candidate = Path(path).expanduser().resolve()
        for root in self.denied_roots:
            if root.covers(candidate):
                raise ContainmentError(
                    f"{what} {candidate} is inside the denied root {root.path} "
                    f"({root.reason}); copy it outside every denied root"
                )

    # -- rendering -------------------------------------------------------

    def profile_text(self, *, workspace: Path, attempt_dir: Path) -> str:
        """Render the seatbelt profile for one attempt (deterministic)."""
        workspace = Path(workspace).expanduser().resolve()
        attempt_dir = Path(attempt_dir).expanduser().resolve()
        self.validate_workspace(workspace)
        for path, what in ((workspace, "workspace"), (attempt_dir, "attempt directory")):
            self.validate_readable(path, what=what)

        lines = [
            f"; Tactus containment profile v{PROFILE_TEMPLATE_VERSION}",
            f"; host: {describe_host()}",
            f"; egress: {self.egress.mode}"
            + (f" (localhost:{self.egress.port})" if self.egress.port else ""),
            f"; denied roots: {len(self.denied_roots)}",
            "(version 1)",
            "(deny default)",
            "(allow process-exec* process-fork)",
            # A child may manage its own children, never anything outside this
            # sandbox: the supervisor and the operator's processes stay out of reach.
            "(allow signal (target same-sandbox))",
            "(allow sysctl-read)",
            # Reads are a deny-list: everything, then the denied roots. Stated as
            # such in the docs, because it is weaker than an allowlist.
            '(allow file-read* (subpath "/"))',
        ]
        for root in self.denied_roots:
            lines.append(f'(deny file-read* (subpath "{root.path}"))')
        writes = [f'(subpath "{workspace}")', f'(subpath "{attempt_dir}")']
        writes += ['(literal "/dev/null")', '(literal "/dev/tty")']
        lines.append("(allow file-write* " + " ".join(writes) + ")")
        for allowance in self.allowances:
            lines.append(f"(allow {allowance})")
        lines.append(self.egress.sbpl())
        return "\n".join(lines) + "\n"

    def write_profile(self, path: Path, *, workspace: Path, attempt_dir: Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = self.profile_text(workspace=workspace, attempt_dir=attempt_dir)
        target.write_text(text, encoding="utf-8")
        return target

    def profile_sha256(self, *, workspace: Path, attempt_dir: Path) -> str:
        text = self.profile_text(workspace=workspace, attempt_dir=attempt_dir)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_template_version": PROFILE_TEMPLATE_VERSION,
            "launcher": self.launcher.as_dict(),
            "denied_roots": [root.as_dict() for root in self.denied_roots],
            "egress": self.egress.as_dict(),
            "allowances": list(self.allowances),
        }


_SECRET_TOKEN_RE = re.compile(
    r"\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*)=\S*"
)


def redact_process_command(command: str) -> str:
    """Keep a survivor's argv, drop the environment ``ps -E`` appends to it.

    ``ps -E`` prints the child's whole environment after its argv, so a survivor
    would otherwise copy its credentials into attempt evidence. The argv is what
    identifies a process; the environment is not evidence and must not become it.
    """
    text = str(command or "")
    for marker in (" TACTUS_ATTEMPT_DIR=", " TACTUS_ATTEMPT_ID=", " PATH="):
        index = text.find(marker)
        if index > 0:
            text = text[:index]
            break
    return _SECRET_TOKEN_RE.sub(lambda match: match.group(1) + "=<redacted>", text).strip()


@dataclass(frozen=True, slots=True)
class ProcessRef:
    """One live process the sweep found, with its environment material removed."""

    pid: int
    argv: str
    matched_by: str = "attempt-marker"

    @property
    def command(self) -> str:
        return self.argv

    def as_dict(self) -> dict[str, Any]:
        return {"pid": self.pid, "command": self.argv, "matched_by": self.matched_by}


def sweep_is_visible(*, timeout: float = 10.0) -> tuple[bool, str]:
    """Prove this host's sweep can see a marker before trusting an empty result.

    ``ps -E`` does not expose the environment of every binary on macOS (Apple
    platform binaries hide it), so "no process carries the marker" is only
    meaningful if a marker we planted ourselves is visible. Returns
    ``(visible, detail)``.
    """

    token = f"containment-sweep-control-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    env = dict(os.environ)
    env[ATTEMPT_MARKER_VARIABLE] = token
    try:
        control = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        return (False, f"control process could not start: {exc}")
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(item.pid == control.pid for item in residual_processes(token)):
                return (True, "marker visible")
            if control.poll() is not None:
                break
            time.sleep(0.02)
        return (False, "a planted marker was not visible to the sweep")
    finally:
        with contextlib.suppress(OSError):
            control.terminate()
        with contextlib.suppress(Exception):
            control.wait(timeout=5)


LSOF_PATH = "/usr/sbin/lsof"


def workspace_holders(
    paths: Sequence[Path], *, timeout: float = 20.0
) -> tuple[ProcessRef, ...] | None:
    """Processes holding a path open or using it as their working directory.

    This is the marker-independent half of the survivor check: an agent's leftover
    helper usually still sits in the workspace (as cwd or with a file open), and
    that is observable even where ``ps -E`` hides the environment. ``None`` means
    the check could not be performed, which must never be read as "nothing there".
    """

    if not os.access(LSOF_PATH, os.X_OK):
        return None
    here = os.getpid()
    found: dict[int, ProcessRef] = {}
    for candidate in paths:
        target = Path(candidate)
        if not target.exists():
            continue
        try:
            completed = subprocess.run(
                [LSOF_PATH, "-w", "-n", "-F", "pc", "+D", str(target)],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ContainmentUnavailable(f"holder check unavailable: {exc}") from exc
        if completed.returncode not in (0, 1):
            return None
        pid: int | None = None
        for line in completed.stdout.splitlines():
            if line.startswith("p") and line[1:].isdigit():
                pid = int(line[1:])
            elif line.startswith("c") and pid is not None:
                if pid == here:
                    continue
                found.setdefault(
                    pid,
                    ProcessRef(pid=pid, argv=line[1:], matched_by="workspace-holder"),
                )
    return tuple(found.values())


def residual_processes(marker: str, *, timeout: float = 5.0) -> tuple[ProcessRef, ...]:
    """Find processes still carrying ``TACTUS_ATTEMPT_DIR=<marker>``.

    A process group can be gone while a detached helper still runs; the marker is
    set once for the attempt and inherited by every descendant, so this is the
    only trustworthy "nothing of this attempt is alive" check beyond the group.
    """

    if not isinstance(marker, str) or not marker.strip():
        raise ContainmentError("a non-empty attempt marker is required")
    needle = f"{ATTEMPT_MARKER_VARIABLE}={marker}"
    try:
        completed = subprocess.run(
            ["ps", "-wwE", "-A", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Unverifiable identity must never be read as "nothing is alive".
        raise ContainmentUnavailable(f"process sweep unavailable: {exc}") from exc
    found: list[ProcessRef] = []
    for line in completed.stdout.splitlines():
        if needle not in line:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        found.append(ProcessRef(pid=pid, argv=redact_process_command(command)))
    return tuple(found)


@dataclass(frozen=True, slots=True)
class SelfTestCheck:
    """One denial or permission, as actually observed."""

    name: str
    ok: bool
    detail: str
    required: bool = True
    exit_code: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "required": self.required,
            "detail": self.detail,
            "exit_code": self.exit_code,
        }


@dataclass(frozen=True, slots=True)
class SelfTestResult:
    """Outcome of the denial matrix: every check, not just a verdict."""

    checks: tuple[SelfTestCheck, ...]
    profile_sha256: str = ""

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks if check.required)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "profile_sha256": self.profile_sha256,
            "checks": [check.as_dict() for check in self.checks],
        }


def describe_host() -> str:
    """A short, stable host description recorded in evidence."""
    try:
        version = platform.mac_ver()[0] or platform.release()
    except Exception:  # pragma: no cover - defensive
        version = "unknown"
    return f"macos-{version}-{platform.machine()}"


def host_build_label() -> str:
    """The macOS version and build this host runs, e.g. ``macOS 27.2 (26B123)``.

    Recorded beside a launcher digest so a mismatch can name the host the pin
    belongs to and the host that refused it — an OS update then produces a message
    that explains itself instead of a bare digest diff. Never guessed: an
    unreadable host says so.
    """
    try:
        version = platform.mac_ver()[0]
    except Exception:  # pragma: no cover - defensive
        version = ""
    build = ""
    try:
        completed = subprocess.run(
            ["sw_vers", "-buildVersion"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if completed.returncode == 0:
            build = completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - non-macOS hosts
        build = ""
    if not version:
        return "unknown"
    return f"macOS {version} ({build})" if build else f"macOS {version}"


#: Interpreters used for the connect probe, in order of preference. The
#: ``/usr/bin`` entry is a shim on macOS that prints unrelated noise, so the
#: Command Line Tools interpreter is preferred; if neither runs, the connect
#: check is recorded as *unknown*, never as passed.
CONNECT_PROBE_PYTHONS = (
    "/Library/Developer/CommandLineTools/usr/bin/python3",
    "/usr/bin/python3",
)

_CONNECT_PROBE = """\
import errno, socket, sys
sock = socket.socket()
sock.settimeout(2.0)
try:
    sock.connect((sys.argv[1], int(sys.argv[2])))
    print("CONNECTED")
except OSError as exc:
    print("ERRNO=%d NAME=%s" % (exc.errno, errno.errorcode.get(exc.errno, "?")))
finally:
    sock.close()
"""


def _connect_python() -> str | None:
    for candidate in CONNECT_PROBE_PYTHONS:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def _denied_read_target(policy: ContainmentPolicy) -> Path | None:
    """A readable file inside a denied root, used to prove the read is refused."""
    for root in policy.denied_roots:
        if not root.path.exists():
            continue
        for candidate in DENIED_READ_CANDIDATES:
            path = root.path / candidate
            if path.is_file():
                return path
        if root.path.is_file():
            return root.path
        if root.path.is_dir():
            examined = 0
            for child in root.path.rglob("*"):
                examined += 1
                if child.is_file() and not child.is_symlink():
                    return child
                if examined >= 200:
                    break
    return None


def _keychain_store_target(policy: ContainmentPolicy) -> Path | None:
    """A keychain *store* file inside a denied root, when the pin denies one.

    Mach access to securityd is a permission of its own (a hosted runtime needs it
    for TLS trust evaluation), and it does not make the operator's keychain files
    readable. That is the half of the keychain boundary the file system still
    carries, so it is the half this can check.
    """
    for root in policy.denied_roots:
        if root.path.parts[-2:] != ("Library", "Keychains") or not root.path.is_dir():
            continue
        for child in sorted(root.path.iterdir()):
            if child.is_file() and child.name.endswith((".keychain-db", ".keychain")):
                return child
    return None


def run_self_test(
    policy: ContainmentPolicy,
    *,
    workspace: Path,
    attempt_dir: Path,
    runtime_probe_argv: Sequence[str] = (),
    expected_version_token: str = "",
    timeout: float = 30.0,
) -> SelfTestResult:
    """Prove the boundary with real children before any agent process exists.

    Every check runs through the same launcher and the same rendered profile that
    the attempt will use, so a profile that silently fails to apply cannot pass.
    """

    workspace = Path(workspace).expanduser().resolve()
    attempt_dir = Path(attempt_dir).expanduser().resolve()
    attempt_dir.mkdir(parents=True, exist_ok=True)
    policy.launcher.verify()
    profile_path = attempt_dir / "containment.sb"
    policy.write_profile(profile_path, workspace=workspace, attempt_dir=attempt_dir)
    profile_sha = hashlib.sha256(profile_path.read_bytes()).hexdigest()

    home = attempt_dir / "home"
    temp = attempt_dir / "tmp"
    for directory in (home, temp):
        directory.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home),
        "TMPDIR": str(temp),
        "GIT_CONFIG_GLOBAL": str(home / "gitconfig"),
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "TACTUS_CONTAINMENT_SELFTEST": "1",
    }

    def launch(argv: Sequence[str]) -> tuple[int, str]:
        command = [
            str(item)
            .replace("{profile}", str(profile_path))
            .replace("{workspace}", str(workspace))
            for item in list(policy.launcher.argv_prefix) + [str(part) for part in argv]
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                cwd=str(workspace),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return (-1, f"launch failed: {exc}")
        return (completed.returncode, (completed.stdout or "") + (completed.stderr or ""))

    checks: list[SelfTestCheck] = []

    # 1. A write inside the workspace must succeed, so the boundary is not simply
    #    broken in a way that also denies the agent its own work.
    inside = workspace / ".containment-write-probe"
    inside.unlink(missing_ok=True)
    code, output = launch(["/bin/sh", "-c", f"printf ok > '{inside}'"])
    wrote = inside.is_file() and inside.read_text(encoding="utf-8") == "ok"
    checks.append(
        SelfTestCheck(
            name="write_allowed_in_workspace",
            ok=code == 0 and wrote,
            detail="workspace write permitted" if code == 0 and wrote else output.strip()[:200],
            exit_code=code,
        )
    )
    inside.unlink(missing_ok=True)

    # 2. A write outside it must fail, and must leave nothing behind.
    outside = attempt_dir.parent / f"escape-{attempt_dir.name}"
    outside.unlink(missing_ok=True)
    code, output = launch(["/bin/sh", "-c", f"printf no > '{outside}'"])
    escaped = outside.exists()
    if escaped:
        outside.unlink(missing_ok=True)
    checks.append(
        SelfTestCheck(
            name="write_denied_outside",
            ok=(not escaped) and code != 0,
            detail=(
                "write outside the workspace refused"
                if not escaped and code != 0
                else f"ESCAPED: {outside} was written"
            ),
            exit_code=code,
        )
    )

    # 3. A read of a denied root must fail.
    target = _denied_read_target(policy)
    if target is None:
        checks.append(
            SelfTestCheck(
                name="read_denied_root",
                ok=True,
                required=False,
                detail="no existing file under a denied root to probe; skipped",
            )
        )
    else:
        code, output = launch(["/bin/cat", str(target)])
        denied = code != 0 and FILE_READ_DENIED_MESSAGE in output
        checks.append(
            SelfTestCheck(
                name="read_denied_root",
                ok=denied,
                detail=f"read of {target} refused" if denied else output.strip()[:200],
                exit_code=code,
            )
        )

    # 3b. A keychain store must stay unreadable through the file system, whatever
    #     Mach services the runtime is allowed: securityd access does not open it.
    store = _keychain_store_target(policy)
    if store is None:
        checks.append(
            SelfTestCheck(
                name="read_denied_keychain_store",
                ok=True,
                required=False,
                detail=(
                    "the pin denies no keychain store (or this host has none); skipped"
                ),
            )
        )
    else:
        code, output = launch(["/bin/cat", str(store)])
        denied = code != 0 and FILE_READ_DENIED_MESSAGE in output
        checks.append(
            SelfTestCheck(
                name="read_denied_keychain_store",
                ok=denied,
                detail=(
                    f"read of {store} refused"
                    if denied
                    else output.strip()[:200]
                ),
                exit_code=code,
            )
        )

    # 4. Egress: denied when it must be, permitted exactly where it is allowed.
    #    ``nc`` distinguishes the two outcomes in its message: "Operation not
    #    permitted" is the sandbox refusing, "refused"/"succeeded" is a permitted
    #    connection attempt. No listener is required, so the check is deterministic.
    interpreter = _connect_python()
    if interpreter is not None:
        (attempt_dir / "connect_probe.py").write_text(_CONNECT_PROBE, encoding="utf-8")

    def attempt(port: int) -> tuple[str, int, str]:
        if interpreter is None:
            return ("unknown", -1, "no interpreter available for the connect probe")
        code, output = launch([interpreter, str(attempt_dir / "connect_probe.py"), "127.0.0.1", str(port)])
        lines = [line for line in output.splitlines() if line.startswith(("ERRNO=", "CONNECTED"))]
        verdict_line = lines[-1] if lines else "NO-OUTPUT"
        if verdict_line.startswith("ERRNO=1 "):
            return ("denied", code, verdict_line)
        if verdict_line.startswith(("ERRNO=", "CONNECTED")):
            return ("allowed", code, verdict_line)
        return ("unknown", code, verdict_line)

    if policy.egress.mode == "deny":
        verdict, code, text = attempt(1)
        checks.append(
            SelfTestCheck(
                name="connect_denied",
                ok=verdict == "denied",
                detail=text,
                exit_code=code,
            )
        )
    elif policy.egress.mode == "localhost":
        allowed_verdict, allowed_code, allowed_text = attempt(int(policy.egress.port))
        blocked_port = 1 if policy.egress.port != 1 else 2
        blocked_verdict, blocked_code, blocked_text = attempt(blocked_port)
        checks.append(
            SelfTestCheck(
                name="connect_allowed_localhost_port",
                ok=allowed_verdict == "allowed",
                detail=f"port {policy.egress.port}: {allowed_text}",
                exit_code=allowed_code,
            )
        )
        checks.append(
            SelfTestCheck(
                name="connect_denied_other_port",
                ok=blocked_verdict == "denied",
                detail=f"port {blocked_port}: {blocked_text}",
                exit_code=blocked_code,
            )
        )
    else:  # open
        verdict, code, text = attempt(1)
        checks.append(
            SelfTestCheck(
                name="connect_open",
                ok=verdict == "allowed",
                detail=f"egress declared open: {text}",
                exit_code=code,
            )
        )

    # 5. A child must not be able to widen its own sandbox.
    code, output = launch(
        ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/bin/echo", "widened"]
    )
    checks.append(
        SelfTestCheck(
            name="nested_widen_denied",
            ok=code != 0,
            detail="nested sandbox refused" if code != 0 else "ESCAPED: child widened its sandbox",
            exit_code=code,
        )
    )

    # 6. A child must not reach a process outside its sandbox (the supervisor).
    code, output = launch(["/bin/kill", "-0", str(os.getpid())])
    checks.append(
        SelfTestCheck(
            name="foreign_signal_denied",
            ok=code != 0,
            detail="signal to a foreign process refused" if code != 0 else "ESCAPED: signalled outside",
            exit_code=code,
        )
    )

    # 7. The pinned runtime must be runnable through this very launcher: what was
    #    verified is what will run.
    if runtime_probe_argv:
        code, output = launch(runtime_probe_argv)
        token_ok = (not expected_version_token) or expected_version_token in output
        checks.append(
            SelfTestCheck(
                name="runtime_probe_through_launcher",
                ok=code == 0 and token_ok,
                detail=(
                    output.strip()[:200] or f"exit {code}"
                )
                if token_ok
                else f"version token {expected_version_token!r} absent from {output.strip()[:160]!r}",
                exit_code=code,
            )
        )

    return SelfTestResult(checks=tuple(checks), profile_sha256=profile_sha)


def digest_tree(root: Path) -> tuple[str, int]:
    """Digest a tree deterministically: path-sorted ``sha256  relpath`` manifest.

    Ordering is declared (path-sorted, byte-wise) so the same tree always yields
    the same digest regardless of filesystem enumeration order.
    """

    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise ContainmentError(f"artifact tree is not a directory: {base}")
    entries: list[tuple[str, str]] = []
    for path in sorted(base.rglob("*"), key=lambda item: item.relative_to(base).as_posix()):
        if not path.is_file() or path.is_symlink():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append((path.relative_to(base).as_posix(), digest))
    manifest = "".join(f"{digest}  {name}\n" for name, digest in entries)
    return hashlib.sha256(manifest.encode("utf-8")).hexdigest(), len(entries)


def which_launcher(name: str = "sandbox-exec") -> Launcher | None:
    """Locate the default seatbelt launcher on this host, if present."""
    found = shutil.which(name)
    if not found:
        return None
    return Launcher.for_path(found)
