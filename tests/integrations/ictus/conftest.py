"""Pin the real Ictus conformance tests to the exact reviewed revision.

The harness in :mod:`integrations.ictus.conformance` deliberately fails closed
when it is pointed at a checkout whose ``HEAD`` is not the reviewed pin. That is
the correct behaviour for a shared local checkout, which may legitimately
advance past the pin (for example with a docs-only commit from another
WorkOrder).

Rather than weakening the pin check, :func:`seam_support.pinned_checkout`
materialises the exact pinned commit in a disposable local clone and this
session fixture points the harness at it. CI already checks out the pinned
revision, in which case the existing checkout is used unchanged. No canonical
checkout is mutated and the clone is removed at interpreter exit.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

import seam_support


@pytest.fixture(scope="session", autouse=True)
def pinned_ictus_checkout() -> Iterator[Path]:
    """Resolve the real boundary against the exact reviewed revision."""

    checkout = seam_support.pinned_checkout()
    previous = os.environ.get(seam_support.ICTUS_ROOT_ENV)
    os.environ[seam_support.ICTUS_ROOT_ENV] = str(checkout)
    try:
        yield checkout
    finally:
        if previous is None:
            os.environ.pop(seam_support.ICTUS_ROOT_ENV, None)
        else:
            os.environ[seam_support.ICTUS_ROOT_ENV] = previous
