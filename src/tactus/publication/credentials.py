"""Fine-grained publication token provider with a fail-closed scope preflight.

Why this exists at all: the publication transport needs a credential, and the
wrong credential hands whoever holds it the ability to rewrite history in every
repository the operator can write to. This provider is deliberately boring and
deliberately narrow:

* the token comes from **one file at an absolute path outside the repository**,
  so it is never committed, never inside a workspace handed to an agent and
  never read from an ambient ``gh`` login;
* its scope is **checked before any publication**: a token that reports admin
  permission, or any classic OAuth scope at all, is refused. Classic tokens
  (``gho_``/``ghp_``) are repository-wide by construction - they carry ``repo``
  and therefore write access to every repository the account can push to - so
  none of them is ever accepted here;
* the material never leaves an opaque
  :class:`~tactus.publication.guard.PublicationCredential`. It is not logged, not
  copied into evidence and not included in an exception message.

What this does **not** claim: it does not isolate the credential from the agent.
The supervised agent runs as the same OS account, so a token file is readable by
it (``tactus.runtime.supervision`` says so in its own docstring). Narrowing the
token is the achievable win; file permissions and OS-level isolation are an
operator concern, and this module does not pretend otherwise.

See ``docs/architecture/LIVE_PUBLICATION.md``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .guard import PublicationCredential
from .models import PublicationGrant, PublicationRequest

__all__ = [
    "CredentialPreflightError",
    "FileTokenCredentialProvider",
    "TokenScopeFacts",
    "probe_github_token",
]


class CredentialPreflightError(RuntimeError):
    """The configured publication credential is missing, unusable or too broad."""


@dataclass(frozen=True, slots=True)
class TokenScopeFacts:
    """What a scope probe learned about one token, never including the token.

    ``permissions`` mirrors GitHub's ``permissions`` object for the repository
    (``admin``/``maintain``/``push``/``triage``/``pull``). ``oauth_scopes`` is the
    ``X-OAuth-Scopes`` header, which is present for classic tokens and absent for
    fine-grained ones.
    """

    permissions: Mapping[str, object]
    oauth_scopes: tuple[str, ...] = ()

    @property
    def is_classic(self) -> bool:
        """A classic token reports OAuth scopes; fine-grained ones report none."""

        return bool(self.oauth_scopes)


ScopeProbe = Callable[[str, str], TokenScopeFacts]


def probe_github_token(token: str, repository: str) -> TokenScopeFacts:
    """Read one repository's permission facts for *token*, over one request.

    This is the only network call this module makes, and it is made once, before
    any publication. It is injectable so tests never touch the network.
    """

    request = urllib.request.Request(  # noqa: S310 - fixed https endpoint
        f"https://api.github.com/repos/{repository}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15.0) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8") or "{}")
            scopes = response.headers.get("X-OAuth-Scopes", "")
    except urllib.error.HTTPError as exc:
        raise CredentialPreflightError(
            f"the publication token cannot read {repository} (HTTP {exc.code})"
        ) from None
    except (urllib.error.URLError, OSError, ValueError):
        raise CredentialPreflightError(
            f"cannot verify the publication token against {repository}"
        ) from None
    permissions = payload.get("permissions") if isinstance(payload, Mapping) else None
    if not isinstance(permissions, Mapping):
        raise CredentialPreflightError(
            f"GitHub returned no permission facts for {repository}"
        )
    return TokenScopeFacts(
        permissions=dict(permissions),
        oauth_scopes=tuple(
            part.strip() for part in scopes.split(",") if part.strip()
        ),
    )


class FileTokenCredentialProvider:
    """Resolve one narrow publication token from a file outside the repository.

    The scope preflight runs in the constructor: a provider that would publish
    with too broad a token is never usable, which is safer than discovering it
    at the moment of a push.
    """

    def __init__(
        self,
        token_path: str | os.PathLike[str],
        *,
        repository: str,
        repository_root: str | os.PathLike[str],
        probe: ScopeProbe | None = None,
        provider_id: str = "operator-token",
    ) -> None:
        if not isinstance(repository, str) or "/" not in repository.strip():
            raise CredentialPreflightError("repository must be a canonical owner/name")
        self._repository = repository.strip()
        self._provider_id = provider_id
        self._path = self._resolve_path(token_path, repository_root)
        self._probe = probe if probe is not None else probe_github_token
        self._token = self._read_token()
        self._preflight(self._probe(self._token, self._repository))

    # -- construction -----------------------------------------------------

    @staticmethod
    def _resolve_path(
        token_path: str | os.PathLike[str], repository_root: str | os.PathLike[str]
    ) -> Path:
        path = Path(os.fspath(token_path)).expanduser()
        if not path.is_absolute():
            raise CredentialPreflightError(
                "the publication token path must be absolute"
            )
        resolved = path.resolve()
        root = Path(os.fspath(repository_root)).expanduser()
        root = root.resolve() if root.exists() else root
        if resolved == root or root in resolved.parents:
            raise CredentialPreflightError(
                "the publication token file must live outside the repository"
            )
        if not resolved.is_file():
            raise CredentialPreflightError(
                f"the publication token file does not exist: {resolved}"
            )
        return resolved

    def _read_token(self) -> str:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            raise CredentialPreflightError(
                f"cannot read the publication token file: {self._path}"
            ) from None
        token = raw.strip()
        if not token:
            raise CredentialPreflightError(
                f"the publication token file is empty: {self._path}"
            )
        if any(character.isspace() for character in token):
            raise CredentialPreflightError(
                "the publication token file must hold exactly one token and no "
                "other text"
            )
        return token

    @staticmethod
    def _preflight(facts: TokenScopeFacts) -> None:
        if facts.is_classic:
            raise CredentialPreflightError(
                "the publication token is a classic token "
                f"(scopes: {', '.join(facts.oauth_scopes)}), which is "
                "repository-wide by definition; use a fine-grained token scoped "
                "to this repository with contents and pull-request write access"
            )
        if bool(facts.permissions.get("admin")):
            raise CredentialPreflightError(
                "the publication token carries admin permission; publication "
                "requires only contents and pull-request write access"
            )
        if not bool(facts.permissions.get("push")):
            raise CredentialPreflightError(
                "the publication token cannot write this repository; it needs "
                "contents and pull-request write access"
            )

    # -- the credential-provider protocol ---------------------------------

    @property
    def repository(self) -> str:
        return self._repository

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def resolve(
        self,
        grant: PublicationGrant,
        request: PublicationRequest,
        *,
        at: datetime,
    ) -> PublicationCredential:
        """Return the opaque handle for an already-authorized publication."""

        if not isinstance(grant, PublicationGrant):
            raise CredentialPreflightError("grant must be a PublicationGrant")
        if not isinstance(request, PublicationRequest):
            raise CredentialPreflightError("request must be a PublicationRequest")
        if request.repository != self._repository:
            raise CredentialPreflightError(
                f"the publication token is scoped to {self._repository!r}, "
                f"not {request.repository!r}"
            )
        return PublicationCredential(self._token, provider_id=self._provider_id)

    def __repr__(self) -> str:
        return (
            f"FileTokenCredentialProvider(repository={self._repository!r}, "
            f"provider_id={self._provider_id!r}, token=<redacted>)"
        )
