"""Publication authorization grants, guard and mock-only credential interface.

This package implements the Tactus-owned boundary between a successfully
implemented change and an authorized branch/PR publication. It deliberately
contains no Git, GitHub, Stax or credential-store implementation: the guard
consumes already-validated grants, resolves an opaque credential only after all
authorization checks pass, and hands it to an injected mock transport.

See ``docs/architecture/PUBLICATION_GRANTS.md`` for the ownership and ordering
contract.
"""

from .guard import (
    PublicationCredential,
    PublicationCredentialProvider,
    PublicationEvidence,
    PublicationGuard,
    PublicationReceipt,
    PublicationTransport,
)
from .models import (
    DEFAULT_TRUSTED_AUTHORITIES,
    PublicationAuthorization,
    PublicationAuthorityError,
    PublicationBindingError,
    PublicationCredentialError,
    PublicationDenialCode,
    PublicationDeniedError,
    PublicationEffect,
    PublicationError,
    PublicationExpiredError,
    PublicationGrant,
    PublicationIdentityConflictError,
    PublicationNotYetValidError,
    PublicationRequest,
    PublicationRevokedError,
    PublicationScopeError,
    PublicationTransportError,
    normalize_scope,
    scope_covers,
    validate_publication_grant,
)

__all__ = [
    "DEFAULT_TRUSTED_AUTHORITIES",
    "PublicationAuthorization",
    "PublicationAuthorityError",
    "PublicationBindingError",
    "PublicationCredential",
    "PublicationCredentialError",
    "PublicationCredentialProvider",
    "PublicationDenialCode",
    "PublicationDeniedError",
    "PublicationEffect",
    "PublicationError",
    "PublicationEvidence",
    "PublicationExpiredError",
    "PublicationGrant",
    "PublicationGuard",
    "PublicationIdentityConflictError",
    "PublicationNotYetValidError",
    "PublicationReceipt",
    "PublicationRequest",
    "PublicationRevokedError",
    "PublicationScopeError",
    "PublicationTransport",
    "PublicationTransportError",
    "normalize_scope",
    "scope_covers",
    "validate_publication_grant",
]
