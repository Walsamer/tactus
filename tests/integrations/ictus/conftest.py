"""Pin the real Ictus conformance tests to the exact reviewed revision.

The harness in :mod:`integrations.ictus.conformance` deliberately fails closed
when it is pointed at a checkout whose ``HEAD`` is not the reviewed pin. That is
the correct behaviour for a shared local checkout, which may legitimately
advance past the pin (for example with a docs-only commit from another
WorkOrder).

Rather than weakening the pin check, this session fixture materialises the exact
pinned commit in a disposable local clone and points the harness at it. CI
already checks out the pinned revision, in which case the existing checkout is
used unchanged. No canonical checkout is mutated and the clone is removed at the
end of the session.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterator

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HARNESS_PATH = _REPO_ROOT / "integrations" / "ictus" / "conformance.py"
_SPEC = importlib.util.spec_from_file_location("ictus_conformance_support", _HARNESS_PATH)
assert _SPEC is not None and _SPEC.loader is not None
conformance = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = conformance
_SPEC.loader.exec_module(conformance)


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=60.0,
    )


def _pinned_checkout(root: Path, base: Path) -> tuple[Path, Path | None]:
    """Return ``(checkout, disposable_clone)`` at the reviewed pin."""

    head = _git("rev-parse", "HEAD", cwd=root)
    if head.returncode != 0:
        raise conformance.IctusBoundaryUnavailable(
            f"'{root}' is not a git checkout: {head.stderr.strip()}"
        )
    if head.stdout.strip() == conformance.PINNED_ICTUS_COMMIT:
        return root, None

    commit = conformance.PINNED_ICTUS_COMMIT
    exists = _git("cat-file", "-e", f"{commit}^{{commit}}", cwd=root)
    if exists.returncode != 0:
        raise conformance.IctusPinMismatch(
            f"Ictus checkout '{root}' is at {head.stdout.strip()!r} and does not "
            f"contain the pinned commit {commit!r}"
        )

    checkout = base / "checkout"
    cloned = _git(
        "clone", "--shared", "--no-checkout", "--quiet", str(root), str(checkout),
        cwd=base,
    )
    if cloned.returncode != 0:
        raise conformance.IctusBoundaryUnavailable(
            f"cannot create a disposable clone at {commit!r} from "
            f"'{root}': {cloned.stderr.strip()}"
        )
    checked_out = _git("checkout", "--quiet", "--detach", commit, cwd=checkout)
    if checked_out.returncode != 0:
        raise conformance.IctusBoundaryUnavailable(
            f"cannot check out the pinned commit {commit!r} in "
            f"'{checkout}': {checked_out.stderr.strip()}"
        )
    pinned = _git("rev-parse", "HEAD", cwd=checkout)
    if pinned.stdout.strip() != commit:
        raise conformance.IctusPinMismatch(
            f"disposable checkout '{checkout}' is at {pinned.stdout.strip()!r}, "
            f"expected the pinned {commit!r}"
        )
    return checkout, checkout


@pytest.fixture(scope="session", autouse=True)
def pinned_ictus_checkout(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Resolve the real boundary against the exact reviewed revision."""

    root = conformance.resolve_ictus_root()
    checkout, disposable = _pinned_checkout(
        root, tmp_path_factory.mktemp("ictus-pinned-clone")
    )

    previous = os.environ.get(conformance.ICTUS_ROOT_ENV)
    os.environ[conformance.ICTUS_ROOT_ENV] = str(checkout)
    try:
        yield checkout
    finally:
        if previous is None:
            os.environ.pop(conformance.ICTUS_ROOT_ENV, None)
        else:
            os.environ[conformance.ICTUS_ROOT_ENV] = previous
        if disposable is not None:
            shutil.rmtree(disposable, ignore_errors=True)
