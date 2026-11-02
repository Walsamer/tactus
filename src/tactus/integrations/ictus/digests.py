"""Pure canonical hashing and version-fenced ledger replay comparisons.

Pinned contract
---------------
* Contract source: Tactus execution-cycle ledger / Ictus submission boundary
* Supported ledger digest versions: bare ``sha256:v0`` and prefixed ``sha256:v1``

Ledger canonicalization is not Ictus's proposal-binding serialization. These
helpers must not be used to claim verification of ``validation.proposal_digest``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

LEDGER_DIGEST_VERSION = "sha256:v1"
LEGACY_LEDGER_DIGEST_VERSION = "sha256:v0"


class DigestContractError(ValueError):
    """Base class for invalid or incompatible ledger digests."""


class UnknownDigestVersionError(DigestContractError):
    """A prefixed digest declares an unsupported serialization version."""


class MalformedDigestError(DigestContractError):
    """A digest is not a SHA-256 hexadecimal string in a supported shape."""


class CrossVersionDigestConflict(DigestContractError):
    """Replay comparison cannot establish equality across digest versions."""


def canonical_digest(payload: Mapping[str, Any]) -> str:
    """Return the stable digest used by the Ictus durable submission boundary."""

    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def versioned_digest(payload: Mapping[str, Any]) -> str:
    """Return a canonical digest explicitly bound to its ledger version."""

    return f"{LEDGER_DIGEST_VERSION}:{canonical_digest(payload)}"


def digest_version(digest: str) -> str:
    """Validate a digest and return its version, including legacy bare hashes."""

    if not isinstance(digest, str):
        raise MalformedDigestError("digest must be a string")
    if re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        return LEGACY_LEDGER_DIGEST_VERSION
    if ":" not in digest:
        raise MalformedDigestError("digest must contain 64 hexadecimal characters")
    prefix, _, value = digest.rpartition(":")
    if prefix != LEDGER_DIGEST_VERSION:
        raise UnknownDigestVersionError(f"unknown ledger digest version: {prefix!r}")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise MalformedDigestError("digest must contain 64 hexadecimal characters")
    return prefix


def is_replay(recorded: str, candidate: str) -> bool:
    """Compare same-version digests; never treat a version conflict as new work."""

    recorded_version = digest_version(recorded)
    candidate_version = digest_version(candidate)
    if recorded_version != candidate_version:
        raise CrossVersionDigestConflict(
            f"cannot compare ledger digests {recorded_version} and {candidate_version}"
        )
    return recorded == candidate


__all__ = [
    "LEDGER_DIGEST_VERSION",
    "LEGACY_LEDGER_DIGEST_VERSION",
    "CrossVersionDigestConflict",
    "DigestContractError",
    "MalformedDigestError",
    "UnknownDigestVersionError",
    "canonical_digest",
    "digest_version",
    "is_replay",
    "versioned_digest",
]
