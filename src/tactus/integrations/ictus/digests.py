"""Pure canonical hashing and version-fenced ledger replay comparisons.

Pinned contract
---------------
* Contract source: Tactus execution-cycle ledger / Ictus submission boundary;
  canonical form and number domain: Ictus identity-digest protocol v1
  (``docs/IDENTITY_DIGEST.md``, ``contracts/digest-vectors.json``
  ``schema_version`` 1, Walsamer/ictus PR #26)
* Supported ledger digest versions: bare ``sha256:v0`` and prefixed ``sha256:v1``
* Digested integer domain: ``-2^63 ..= 2^64-1``; floats finite; a document's
  number literals must denote the value they parse to (no rounding, no
  ``NaN``/``Infinity``/``-Infinity``)

The canonical form is CPython's
``json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)``,
which is what the protocol pins. Every value outside the number domain is
refused with :class:`DigestNumberDomainError` naming the rule and the JSON path,
never hashed as its nearest representable form: a digest produced where Ictus
produces a refusal would make an unverified proposal look validated.

Ledger canonicalization is now also Ictus's proposal-binding serialization: at
the pinned Ictus ``c2aa092`` ``validation.proposal_digest`` is computed over the
published canonical form, :func:`~tactus.integrations.ictus.envelope.verify_proposal_binding`
recomputes it, and ``tests/integrations/ictus/test_digest_vectors.py`` asserts the
shipped ``contracts/digest-vectors.json`` artefact from the pinned checkout.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from math import isfinite
from typing import Any

LEDGER_DIGEST_VERSION = "sha256:v1"
LEGACY_LEDGER_DIGEST_VERSION = "sha256:v0"

#: Smallest integer the protocol digests as an integer (``i64::MIN``).
DIGEST_INTEGER_MIN = -9_223_372_036_854_775_808

#: Largest integer the protocol digests as an integer (``u64::MAX``).
DIGEST_INTEGER_MAX = 18_446_744_073_709_551_615

#: Root of every reported JSON path (``$``, ``$.facts[0].value``).
DIGEST_ROOT_PATH = "$"


class DigestContractError(ValueError):
    """Base class for invalid or incompatible ledger digests."""


class UnknownDigestVersionError(DigestContractError):
    """A prefixed digest declares an unsupported serialization version."""


class MalformedDigestError(DigestContractError):
    """A digest is not a SHA-256 hexadecimal string in a supported shape."""


class CrossVersionDigestConflict(DigestContractError):
    """Replay comparison cannot establish equality across digest versions."""


class DigestNumberDomainError(DigestContractError):
    """A number outside the identity-digest protocol's exact number domain.

    Refusing is a normal outcome, not a crash: the value is never hashed as its
    nearest representable form, and the message names the rule and the JSON path
    of the offending number (``number-would-round at $.facts[0].value``).
    """

    #: Stable token naming the refusal, matching the vector file's ``reason``.
    reason: str = ""

    def __init__(self, path: str, literal: str, *, detail: str) -> None:
        super().__init__(f"{self.reason} at {path}: {detail}")
        self.path = path
        self.literal = literal


class NumberOutOfDomainError(DigestNumberDomainError):
    """An integer outside ``-2^63 ..= 2^64-1``: a parser may not hold it exactly."""

    reason = "number-out-of-domain"

    def __init__(self, path: str, literal: str) -> None:
        super().__init__(
            path,
            literal,
            detail=(
                f"integer {literal} is outside the digest domain "
                f"{DIGEST_INTEGER_MIN}..={DIGEST_INTEGER_MAX}"
            ),
        )


class NonFiniteNumberError(DigestNumberDomainError):
    """A non-finite number (``NaN``, ``Infinity``, a literal that overflows)."""

    reason = "non-finite"

    def __init__(self, path: str, literal: str) -> None:
        super().__init__(path, literal, detail=f"{literal} is not a finite number")


class NumberWouldRoundError(DigestNumberDomainError):
    """A number literal spelled with digits the double it parses to cannot hold."""

    reason = "number-would-round"

    def __init__(self, path: str, literal: str) -> None:
        super().__init__(
            path,
            literal,
            detail=f"number literal {literal} does not denote the value it parses to",
        )


@dataclass(frozen=True)
class _RefusedLiteral:
    """A refused number literal, kept as a marker so its JSON path survives.

    A parse hook sees the literal but not where it sits, so the refusal is
    recorded in the parsed structure and :func:`check_number_domain` raises it
    with the path it reached.
    """

    error: type[DigestNumberDomainError]
    literal: str


def check_number_domain(value: Any, path: str = DIGEST_ROOT_PATH) -> None:
    """Refuse any number in ``value`` the protocol cannot carry exactly.

    The digest writer and the text ingress both call this, so no caller can hash
    a value the protocol refuses. An integer outside ``-2^63 ..= 2^64-1`` and a
    non-finite float are judged on the parsed value; a refused literal reaches
    this walk as a marker and is reported with the path it sits at.
    """

    if isinstance(value, _RefusedLiteral):
        raise value.error(path, value.literal)
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        if not DIGEST_INTEGER_MIN <= value <= DIGEST_INTEGER_MAX:
            raise NumberOutOfDomainError(path, str(value))
        return
    if isinstance(value, float):
        if not isfinite(value):
            raise NonFiniteNumberError(path, repr(value))
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            check_number_domain(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            check_number_domain(item, f"{path}[{index}]")


def parse_digestable_document(document: str) -> Any:
    """Parse Ictus JSON text, refusing the numbers the protocol cannot carry.

    This is the ingress for an Ictus document read from JSON text (the
    conformance harness parsing the binary's stdout is the one that matters).
    Three things can only be judged on the text, before a value exists: the
    ``NaN``/``Infinity``/``-Infinity`` spellings a permissive parser accepts as
    numbers, and a literal that only rounds to the double it parses to. Each is
    refused with its rule and its JSON path; the parsed value is then handed to
    :func:`check_number_domain`, the same guard :func:`canonical_digest` uses.
    """

    payload = json.loads(
        document,
        parse_constant=_refused_constant,
        parse_float=_parsed_float,
    )
    check_number_domain(payload)
    return payload


def _refused_constant(literal: str) -> _RefusedLiteral:
    return _RefusedLiteral(NonFiniteNumberError, literal)


def _parsed_float(literal: str) -> Any:
    """The parsed double, or a marker when the literal is outside the domain.

    A literal is in the contract only if it is exactly the value it denotes: it
    must equal, as a number, the shortest round-trip form of the double it
    parses to. ``0.10``, ``1e-05``, ``1E5`` and ``-0.0`` are their values and
    pass; ``0.1000000000000000055511151`` only rounds and is refused. The
    comparison is against exact decimals, never double arithmetic.
    """

    value = float(literal)
    if not isfinite(value):
        return _RefusedLiteral(NonFiniteNumberError, literal)
    if Decimal(literal) != Decimal(repr(value)):
        return _RefusedLiteral(NumberWouldRoundError, literal)
    return value


def canonical_digest(payload: Mapping[str, Any]) -> str:
    """Return the stable digest used by the Ictus durable submission boundary.

    The protocol's number domain is enforced before hashing, so a payload it
    refuses is refused here too instead of being digested approximately.
    """

    check_number_domain(payload)
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
    "DIGEST_INTEGER_MAX",
    "DIGEST_INTEGER_MIN",
    "DIGEST_ROOT_PATH",
    "LEDGER_DIGEST_VERSION",
    "LEGACY_LEDGER_DIGEST_VERSION",
    "CrossVersionDigestConflict",
    "DigestContractError",
    "DigestNumberDomainError",
    "MalformedDigestError",
    "NonFiniteNumberError",
    "NumberOutOfDomainError",
    "NumberWouldRoundError",
    "UnknownDigestVersionError",
    "canonical_digest",
    "check_number_domain",
    "digest_version",
    "is_replay",
    "parse_digestable_document",
    "versioned_digest",
]
