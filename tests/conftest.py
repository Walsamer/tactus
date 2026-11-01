"""One rule for every test that needs the macOS containment boundary.

The boundary this repository enforces is macOS seatbelt, so what a test can
exercise depends on the host. The distinction is only ever *where the boundary
can exist*, never *what a test proves*:

* A test that spawns a real child through ``sandbox-exec`` is marked
  ``seatbelt``; one that reads the macOS process table (``ps -E``,
  ``/usr/sbin/lsof``) is marked ``macos_process_table``. Off Darwin it skips,
  naming the boundary it could not exercise. On Darwin it never skips: a missing
  launcher fails the test, because that absence is the regression worth catching.
* A test that only renders or validates policy *data* needs no launcher at all.
  It runs on every host, against the ``pinned_launcher`` stand-in, which is
  pinned by a real digest exactly as a launcher is.
"""

from __future__ import annotations

import platform
import shutil
from pathlib import Path

import pytest

from tactus.runtime.containment import Launcher

#: marker -> the part of the macOS boundary a test carrying it cannot exercise
#: anywhere else. The skip reason is built from this, so it always names it.
MACOS_ONLY: dict[str, str] = {
    "seatbelt": "macOS seatbelt (sandbox-exec)",
    "macos_process_table": "macOS process table (ps -E, /usr/sbin/lsof)",
}


def pytest_configure(config: pytest.Config) -> None:
    for marker, boundary in MACOS_ONLY.items():
        config.addinivalue_line(
            "markers",
            f"{marker}: needs {boundary}; skipped off Darwin, never skipped on Darwin",
        )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip a macOS-only test off Darwin — and only there.

    On Darwin nothing is skipped, so an absent launcher surfaces as the failure
    it is instead of a green check that proves nothing.
    """
    if platform.system() == "Darwin":
        return
    host = platform.system().lower()
    for item in items:
        for marker, boundary in MACOS_ONLY.items():
            if marker in item.keywords:
                item.add_marker(
                    pytest.mark.skip(reason=f"{boundary} unavailable on {host}")
                )


def _stand_in_launcher(directory: Path, name: str = "sandbox-exec-stand-in") -> Path:
    """An executable at a path a launcher pin can name, needing no seatbelt."""
    path = directory / name
    path.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture()
def pinned_launcher(tmp_path: Path) -> Launcher:
    """A launcher pinned by a real digest, for tests that never execute it.

    Rendering the profile, its exact write allowlist, the recorded allowances,
    the policy hash and every refusal path read launcher *data*; a marked test is
    what actually runs one.
    """
    return Launcher.for_path(_stand_in_launcher(tmp_path))


@pytest.fixture()
def seatbelt_launcher() -> Launcher:
    """This host's real seatbelt launcher, pinned by its digest.

    Only marked tests ask for this, so a missing launcher is a failure here: off
    Darwin the marker stopped the test before it got this far.
    """
    found = shutil.which("sandbox-exec")
    if found is None:
        pytest.fail(
            "macOS seatbelt (sandbox-exec) is absent: the boundary these tests prove "
            "is unavailable on this host"
        )
    return Launcher.for_path(found)
