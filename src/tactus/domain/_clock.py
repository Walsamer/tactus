"""Internal clock helper.

Kept trivial and injectable at the call site so the domain stays deterministic
in tests.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""

    return datetime.now(timezone.utc)
