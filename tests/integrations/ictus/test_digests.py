"""Core canonicalization and version-fenced replay behavior."""

import hashlib

import pytest

from tactus.integrations.ictus.digests import (
    LEDGER_DIGEST_VERSION,
    LEGACY_LEDGER_DIGEST_VERSION,
    CrossVersionDigestConflict,
    MalformedDigestError,
    UnknownDigestVersionError,
    canonical_digest,
    digest_version,
    is_replay,
    versioned_digest,
)


def test_versioned_digest_preserves_canonical_serialization():
    payload = {"z": "é", "a": 1}
    expected = hashlib.sha256(b'{"a":1,"z":"\\u00e9"}').hexdigest()
    assert canonical_digest(payload) == expected
    assert versioned_digest(payload) == f"sha256:v1:{expected}"
    assert digest_version(versioned_digest(payload)) == LEDGER_DIGEST_VERSION


@pytest.mark.parametrize("value", ["a" * 64, "A" * 64])
def test_bare_digest_is_legacy(value):
    assert digest_version(value) == LEGACY_LEDGER_DIGEST_VERSION


@pytest.mark.parametrize("prefix", ["sha256:v0", "sha256:v2", "sha512:v1"])
def test_unknown_prefix_fails_closed(prefix):
    with pytest.raises(UnknownDigestVersionError):
        digest_version(f"{prefix}:{'a' * 64}")


@pytest.mark.parametrize(
    "value",
    [None, True, "", "a" * 63, "g" * 64, "sha256:v1:", "sha256:v1:" + "g" * 64],
)
def test_malformed_digest(value):
    with pytest.raises(MalformedDigestError):
        digest_version(value)


@pytest.mark.parametrize("prefix", ["", "sha256:v1:"])
def test_replay_equal_and_different_same_version(prefix):
    assert is_replay(prefix + "a" * 64, prefix + "a" * 64) is True
    assert is_replay(prefix + "a" * 64, prefix + "b" * 64) is False


@pytest.mark.parametrize("recorded,candidate", [
    ("a" * 64, "sha256:v1:" + "b" * 64),
    ("sha256:v1:" + "a" * 64, "a" * 64),
])
def test_cross_version_comparison_is_a_typed_conflict(recorded, candidate):
    with pytest.raises(CrossVersionDigestConflict):
        is_replay(recorded, candidate)
