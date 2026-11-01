"""Publication authorization grants, guard and mock-only credential interface.

This package implements the Tactus-owned boundary between a successfully
implemented change and an authorized branch/PR publication. The guard consumes
already-validated grants and resolves an opaque credential only after all
authorization checks pass. The mocked publication port adds durable
create/reuse/reconcile behaviour over a fake GitHub transport; it has no live
network, push or credential-store path.

See ``docs/architecture/PUBLICATION_GRANTS.md`` and
``docs/architecture/MOCKED_PUBLICATION.md`` for the ownership, ordering and
idempotency contracts.
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
from .publisher import (
    PUBLICATION_CONTRACT_ID,
    PUBLICATION_VERSION,
    GitHubPublicationPort,
    ImplementationStatus,
    PublicationBusyError,
    PublicationContract,
    PublicationErrorCode,
    PublicationPort,
    PublicationResult,
    PublicationStateError,
    PublicationStatus,
)

__all__ = [
    "DEFAULT_TRUSTED_AUTHORITIES",
    "PUBLICATION_CONTRACT_ID",
    "PUBLICATION_VERSION",
    "GitHubPublicationPort",
    "ImplementationStatus",
    "PublicationAuthorization",
    "PublicationAuthorityError",
    "PublicationBindingError",
    "PublicationBusyError",
    "PublicationContract",
    "PublicationCredential",
    "PublicationCredentialError",
    "PublicationCredentialProvider",
    "PublicationDenialCode",
    "PublicationDeniedError",
    "PublicationEffect",
    "PublicationError",
    "PublicationErrorCode",
    "PublicationEvidence",
    "PublicationExpiredError",
    "PublicationGrant",
    "PublicationGuard",
    "PublicationIdentityConflictError",
    "PublicationNotYetValidError",
    "PublicationPort",
    "PublicationReceipt",
    "PublicationRequest",
    "PublicationResult",
    "PublicationRevokedError",
    "PublicationScopeError",
    "PublicationStateError",
    "PublicationStatus",
    "PublicationTransport",
    "PublicationTransportError",
    "normalize_scope",
    "scope_covers",
    "validate_publication_grant",
]
