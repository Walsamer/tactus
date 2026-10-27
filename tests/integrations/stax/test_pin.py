"""Pin-record integrity tests for the Stax adapter.

The record is review evidence, so the Python constant and the declarative JSON
record must agree exactly. No Stax installation or network is required.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from tactus.integrations.stax import (
    PINNED_STAX,
    STAX_EXECUTABLE_SHA256,
    STAX_RELEASE_ASSET,
    STAX_RELEASE_ASSET_SHA256,
    STAX_RELEASE_TAG,
    STAX_SUPPORTED_PLATFORM,
    STAX_UPSTREAM,
    STAX_VERSION,
    StaxPin,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PIN = REPOSITORY_ROOT / "config" / "stax-pin.json"


def test_pin_records_exact_reviewed_values() -> None:
    assert PINNED_STAX.upstream == "cesarferreira/stax"
    assert PINNED_STAX.version == "0.113.1"
    assert PINNED_STAX.release_tag == "v0.113.1"
    assert PINNED_STAX.supported_platform == "aarch64-apple-darwin"
    assert PINNED_STAX.release_asset == "stax-aarch64-apple-darwin.tar.gz"
    assert PINNED_STAX.release_asset_sha256 == (
        "865f69488b3cc6daf2a259c842bd0c6a6c0dc5b69b70b4fd3d5aae12213293a2"
    )
    assert PINNED_STAX.executable_sha256 == (
        "41c69735dcffd06749fcc10d945f08dab66bd7372cc800b1b8e3a607b180eee2"
    )
    assert PINNED_STAX.executable_name == "stax"


def test_module_constants_mirror_the_pin() -> None:
    assert STAX_UPSTREAM == PINNED_STAX.upstream
    assert STAX_VERSION == PINNED_STAX.version
    assert STAX_RELEASE_TAG == PINNED_STAX.release_tag
    assert STAX_SUPPORTED_PLATFORM == PINNED_STAX.supported_platform
    assert STAX_RELEASE_ASSET == PINNED_STAX.release_asset
    assert STAX_RELEASE_ASSET_SHA256 == PINNED_STAX.release_asset_sha256
    assert STAX_EXECUTABLE_SHA256 == PINNED_STAX.executable_sha256


def test_declarative_config_matches_the_pin() -> None:
    recorded = json.loads(CONFIG_PIN.read_text(encoding="utf-8"))
    assert recorded == {
        "schema_version": 1,
        "adapter": "tactus.integrations.stax",
        "upstream": PINNED_STAX.upstream,
        "version": PINNED_STAX.version,
        "release_tag": PINNED_STAX.release_tag,
        "supported_platform": PINNED_STAX.supported_platform,
        "release_asset": PINNED_STAX.release_asset,
        "release_asset_sha256": PINNED_STAX.release_asset_sha256,
        "executable": PINNED_STAX.executable_name,
        "executable_sha256": PINNED_STAX.executable_sha256,
    }


def test_pin_is_immutable() -> None:
    with pytest.raises(FrozenInstanceError):
        PINNED_STAX.version = "9.9.9"  # type: ignore[misc]


def test_supported_platform_is_exact_not_a_prefix() -> None:
    assert PINNED_STAX.supports_platform("aarch64-apple-darwin")
    assert not PINNED_STAX.supports_platform("aarch64-apple-darwin-extra")
    assert not PINNED_STAX.supports_platform("x86_64-apple-darwin")
    assert not PINNED_STAX.supports_platform("linux")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("upstream", "stax"),
        ("version", "v0.113.1"),
        ("version", "latest"),
        ("release_tag", ""),
        ("supported_platform", ""),
        ("release_asset", ""),
        ("release_asset_sha256", "not-a-digest"),
        ("executable_sha256", "ABC"),
        ("executable_name", "bin/stax"),
    ],
)
def test_pin_rejects_malformed_or_floating_values(field: str, value: str) -> None:
    with pytest.raises(ValueError):
        replace(PINNED_STAX, **{field: value})


def test_pin_can_be_derived_for_an_isolated_test_binary() -> None:
    derived = replace(PINNED_STAX, executable_sha256="0" * 64)
    assert isinstance(derived, StaxPin)
    assert derived is not PINNED_STAX
    assert derived.executable_sha256 == "0" * 64
