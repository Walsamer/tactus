"""Immutable pin record for the Stax CLI used by Tactus.

Stax is a third-party tool (``cesarferreira/stax``). Tactus never updates it,
never substitutes a Git fallback and never accepts a binary that does not match
the exact version and executable digest recorded here. The pin is deliberately
reproducible review evidence: changing it is an explicit, reviewed change to
this module (mirrored by ``config/stax-pin.json``).

The release-asset digest identifies the published archive. The executable
digest identifies the single executable extracted from that archive. Both are
SHA-256 values in lowercase hexadecimal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)

#: Release tag reviewed for this adapter.
STAX_RELEASE_TAG = "v0.113.1"

#: Exact Stax version accepted by this adapter.
STAX_VERSION = "0.113.1"

#: Upstream project (owner/repository).
STAX_UPSTREAM = "cesarferreira/stax"

#: Rust target triple of the reviewed macOS release asset.
STAX_SUPPORTED_PLATFORM = "aarch64-apple-darwin"

#: Release asset reviewed for the supported platform.
STAX_RELEASE_ASSET = "stax-aarch64-apple-darwin.tar.gz"

#: SHA-256 of :data:`STAX_RELEASE_ASSET` as published by the upstream release.
STAX_RELEASE_ASSET_SHA256 = (
    "865f69488b3cc6daf2a259c842bd0c6a6c0dc5b69b70b4fd3d5aae12213293a2"
)

#: SHA-256 of the ``stax`` executable extracted from the release asset.
STAX_EXECUTABLE_SHA256 = (
    "41c69735dcffd06749fcc10d945f08dab66bd7372cc800b1b8e3a607b180eee2"
)


def _require_sha256(name: str, value: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class StaxPin:
    """Immutable, exact Stax version and integrity pin.

    ``StaxPin`` is intentionally a value object. A caller may derive a test pin
    with :func:`dataclasses.replace`, but production preflight must use
    :data:`PINNED_STAX`.
    """

    upstream: str
    version: str
    release_tag: str
    supported_platform: str
    release_asset: str
    release_asset_sha256: str
    executable_sha256: str
    executable_name: str = "stax"

    def __post_init__(self) -> None:
        if not self.upstream or "/" not in self.upstream:
            raise ValueError("upstream must be an owner/repository slug")
        if not _VERSION_RE.fullmatch(self.version):
            raise ValueError("version must be an exact semantic version")
        if not self.release_tag:
            raise ValueError("release_tag must not be empty")
        if not self.supported_platform:
            raise ValueError("supported_platform must not be empty")
        if not self.release_asset:
            raise ValueError("release_asset must not be empty")
        if not self.executable_name or "/" in self.executable_name:
            raise ValueError("executable_name must be a file name")
        _require_sha256("release_asset_sha256", self.release_asset_sha256)
        _require_sha256("executable_sha256", self.executable_sha256)

    @property
    def release_url(self) -> str:
        """Canonical upstream release URL for review and provisioning."""

        return (
            f"https://github.com/{self.upstream}/releases/tag/"
            f"{self.release_tag}"
        )

    @property
    def release_asset_url(self) -> str:
        """Canonical upstream URL of the pinned release asset."""

        return (
            f"https://github.com/{self.upstream}/releases/download/"
            f"{self.release_tag}/{self.release_asset}"
        )

    def supports_platform(self, platform: str) -> bool:
        """Return whether *platform* is the exact reviewed target triple."""

        return platform == self.supported_platform


#: The only pin accepted by the production preflight adapter.
PINNED_STAX = StaxPin(
    upstream=STAX_UPSTREAM,
    version=STAX_VERSION,
    release_tag=STAX_RELEASE_TAG,
    supported_platform=STAX_SUPPORTED_PLATFORM,
    release_asset=STAX_RELEASE_ASSET,
    release_asset_sha256=STAX_RELEASE_ASSET_SHA256,
    executable_sha256=STAX_EXECUTABLE_SHA256,
)

__all__ = [
    "PINNED_STAX",
    "STAX_EXECUTABLE_SHA256",
    "STAX_RELEASE_ASSET",
    "STAX_RELEASE_ASSET_SHA256",
    "STAX_RELEASE_TAG",
    "STAX_SUPPORTED_PLATFORM",
    "STAX_UPSTREAM",
    "STAX_VERSION",
    "StaxPin",
]
