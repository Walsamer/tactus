"""Load the published seam without installing Dagster for conformance tests."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_HARNESS_PATH = Path(__file__).resolve().parents[3] / "integrations/ictus/conformance.py"
_SPEC = importlib.util.spec_from_file_location("seam_conformance_pin", _HARNESS_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_CONFORMANCE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _CONFORMANCE
_SPEC.loader.exec_module(_CONFORMANCE)


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
