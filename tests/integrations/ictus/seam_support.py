"""Load the published seam and pinned artefacts without installing Dagster.

The pinned Ictus revision owns the published seam modules, the contract
artefacts and the identity-digest vectors, so a consumer asserts them from the
checkout at ``PINNED_ICTUS_COMMIT`` instead of mirroring them here. A moved
layout, a missing file or an off-pin checkout **raises**: a skip would hide
exactly the cross-repository drift this conformance exists to catch.
"""

from __future__ import annotations

import atexit
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from typing import Any

_HARNESS_PATH = Path(__file__).resolve().parents[3] / "integrations/ictus/conformance.py"
_SPEC = importlib.util.spec_from_file_location("seam_conformance_pin", _HARNESS_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_CONFORMANCE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _CONFORMANCE
_SPEC.loader.exec_module(_CONFORMANCE)

#: Environment variable that names the Ictus checkout at the pinned revision.
ICTUS_ROOT_ENV = _CONFORMANCE.ICTUS_ROOT_ENV

#: The resolved checkout at the pinned revision, materialised once per run.
_PINNED_CHECKOUT: Path | None = None


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=60.0,
    )


def pinned_checkout() -> Path:
    """Return the Ictus checkout at ``PINNED_ICTUS_COMMIT``, or raise.

    ``TACTUS_ICTUS_ROOT`` (what CI sets) names the checkout. A shared local
    checkout may legitimately advance past the pin, so the exact commit is
    materialised once in a disposable ``--shared`` clone: no canonical checkout
    is mutated, and the clone is removed at interpreter exit. Resolution,
    revision and clone failures all raise rather than skip.
    """

    global _PINNED_CHECKOUT
    if _PINNED_CHECKOUT is not None:
        return _PINNED_CHECKOUT

    commit = _CONFORMANCE.PINNED_ICTUS_COMMIT
    root = _CONFORMANCE.resolve_ictus_root()
    head = _git("rev-parse", "HEAD", cwd=root)
    if head.returncode != 0:
        raise _CONFORMANCE.IctusBoundaryUnavailable(
            f"'{root}' is not a git checkout: {head.stderr.strip()}"
        )
    if head.stdout.strip() == commit:
        _PINNED_CHECKOUT = root
        return root

    if _git("cat-file", "-e", f"{commit}^{{commit}}", cwd=root).returncode != 0:
        raise _CONFORMANCE.IctusPinMismatch(
            f"Ictus checkout '{root}' is at {head.stdout.strip()!r} and does not "
            f"contain the pinned commit {commit!r}"
        )

    base = Path(tempfile.mkdtemp(prefix="tactus-ictus-pinned-"))
    checkout = base / "checkout"
    cloned = _git(
        "clone", "--shared", "--no-checkout", "--quiet", str(root), str(checkout), cwd=base
    )
    if cloned.returncode != 0:
        shutil.rmtree(base, ignore_errors=True)
        raise _CONFORMANCE.IctusBoundaryUnavailable(
            f"cannot create a disposable clone at {commit!r} from '{root}': "
            f"{cloned.stderr.strip()}"
        )
    checked_out = _git("checkout", "--quiet", "--detach", commit, cwd=checkout)
    if checked_out.returncode != 0:
        shutil.rmtree(base, ignore_errors=True)
        raise _CONFORMANCE.IctusBoundaryUnavailable(
            f"cannot check out the pinned commit {commit!r} in '{checkout}': "
            f"{checked_out.stderr.strip()}"
        )
    pinned = _git("rev-parse", "HEAD", cwd=checkout)
    if pinned.stdout.strip() != commit:
        shutil.rmtree(base, ignore_errors=True)
        raise _CONFORMANCE.IctusPinMismatch(
            f"disposable checkout '{checkout}' is at {pinned.stdout.strip()!r}, "
            f"expected the pinned {commit!r}"
        )

    atexit.register(shutil.rmtree, base, ignore_errors=True)
    _PINNED_CHECKOUT = checkout
    return checkout


def load_pinned_artifact(relative_path: str) -> Any:
    """Load a published JSON contract artefact from the pinned revision.

    ``relative_path`` is the artefact's path in the Ictus repository
    (``contracts/digest-vectors.json``). The artefact is the contract: it is
    never vendored here and never shadowed by a hand-written expectation.
    """

    path = pinned_checkout() / relative_path
    if not path.is_file():
        raise FileNotFoundError(
            f"missing published {relative_path} at Ictus pin "
            f"{_CONFORMANCE.PINNED_ICTUS_COMMIT}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def load_seam(python_root: str | Path) -> dict[str, types.ModuleType]:
    # Bypass ictus_dagster/seam/__init__.py, which imports seam.failures and
    # therefore dagster; the published modules below are themselves Dagster-free.
    root = Path(python_root) / "ictus_dagster"
    paths = {
        name: root / "seam" / f"{name}.py"
        for name in ("process", "evidence", "control", "config", "port")
    }
    for path in paths.values():
        if not path.is_file():
            raise FileNotFoundError(
                f"missing published seam module {path} "
                f"at Ictus pin {_CONFORMANCE.PINNED_ICTUS_COMMIT}"
            )

    for name, path in (("ictus_dagster", root), ("ictus_dagster.seam", root / "seam")):
        package = types.ModuleType(name)
        package.__path__ = [str(path)]
        sys.modules[name] = package

    modules = {}
    for name, path in paths.items():
        spec = importlib.util.spec_from_file_location(f"ictus_dagster.seam.{name}", path)
        assert spec is not None and spec.loader is not None, (
            f"cannot load {path} at Ictus pin {_CONFORMANCE.PINNED_ICTUS_COMMIT}"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        modules[name] = module
    return modules
