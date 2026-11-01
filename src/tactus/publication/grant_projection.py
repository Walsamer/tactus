"""Pure projection from a durable human grant to a publication grant.

Ownership boundary (normative)
------------------------------
Tactus records the operator's authorization and never asserts that it was
*sufficient*; Ictus validates sufficiency. This module therefore does exactly
one thing: it restates an already-recorded human grant as the bounded
publication binding the guard already consumes. It performs no I/O, reads no
clock, issues no decision and manufactures no ``ALLOW``.

The projection refuses facts that cannot describe a review-branch publication at
all:

* a human action other than :attr:`~tactus.domain.HumanAction.PUBLISH`;
* a grant recorded for another Work Order or another source revision (the human
  authorized a different specification);
* a protected head branch (``main``/``master``): publication always targets a
  review branch, never the trunk;
* an empty scope, which would authorize nothing.

Freshness is deliberately *not* refused here. Revocation and expiry are facts the
guard already rejects fail-closed **and records as attempt evidence**; throwing
them here instead would lose that evidence, which is why a revoked grant reaches
the publisher and comes back as ``REFUSED`` with the ``revoked`` reason code.

See ``docs/architecture/PUBLICATION_GRANTS.md`` for the binding contract.
"""

from __future__ import annotations

from tactus.domain import SourceRevision
from tactus.domain.human_intervention import HumanAction, HumanGrant
from tactus.integrations.stax.source_control import PreparedChange

from .models import PublicationEffect, PublicationGrant, normalize_scope

__all__ = [
    "PROTECTED_HEAD_BRANCHES",
    "EmptyGrantScopeError",
    "GrantProjectionError",
    "ProtectedHeadBranchError",
    "PublicationActionRequiredError",
    "StaleGrantBindingError",
    "publication_grant_from_human_grant",
]


#: Branches a publication may never use as its head. A review branch is the
#: only publishable head, so the trunk is refused rather than rebound.
PROTECTED_HEAD_BRANCHES: frozenset[str] = frozenset({"main", "master"})


class GrantProjectionError(ValueError):
    """A durable human grant cannot be restated as a publication grant."""


class PublicationActionRequiredError(GrantProjectionError):
    """The recorded human action does not authorize publication."""


class StaleGrantBindingError(GrantProjectionError):
    """The grant was recorded for a different Work Order or source revision."""


class ProtectedHeadBranchError(GrantProjectionError):
    """The head branch is the trunk, which is never a publication head."""


class EmptyGrantScopeError(GrantProjectionError):
    """The grant carries no scope, so it authorizes no path."""


def publication_grant_from_human_grant(
    grant: HumanGrant,
    change: PreparedChange,
    *,
    repository: str,
    destination: str,
    authority: str = "operator",
    effect: PublicationEffect = PublicationEffect.OPEN_PULL_REQUEST,
) -> PublicationGrant:
    """Restate one durable human grant as a publication grant.

    Every binding field comes from a durable record or from the verified
    prepared change; nothing is derived from the caller's intent. ``authority``
    defaults to ``operator``, the only issuing authority the guard trusts today.
    """

    if not isinstance(grant, HumanGrant):
        raise GrantProjectionError("grant must be a HumanGrant")
    if not isinstance(change, PreparedChange):
        raise GrantProjectionError("change must be a PreparedChange")

    if grant.action is not HumanAction.PUBLISH:
        raise PublicationActionRequiredError(
            f"human action {grant.action.value} does not authorize publication; "
            f"recorded action must be {HumanAction.PUBLISH.value}"
        )
    if str(grant.work_order_id) != change.work_order_id:
        raise StaleGrantBindingError(
            f"grant was recorded for WorkOrder {grant.work_order_id}, "
            f"not {change.work_order_id}"
        )
    if grant.source_revision.value != change.source_revision:
        raise StaleGrantBindingError(
            f"grant was recorded for source revision {grant.source_revision}, "
            f"not {change.source_revision}"
        )

    branch = change.review_branch
    if branch.strip().casefold() in PROTECTED_HEAD_BRANCHES:
        raise ProtectedHeadBranchError(
            f"review branch {branch!r} is the trunk; publication head must be a "
            "review branch"
        )

    scope = normalize_scope(grant.scope)
    if not scope:
        raise EmptyGrantScopeError("grant scope is empty, so it authorizes no path")

    return PublicationGrant(
        grant_id=grant.grant_id,
        authority=authority,
        work_order_id=grant.work_order_id,
        repository=repository,
        branch=branch,
        base_revision=change.base_revision,
        destination=destination,
        scope=scope,
        source_revision=SourceRevision(change.source_revision),
        issued_at=grant.issued_at,
        expires_at=grant.expires_at,
        revoked_at=grant.revoked_at,
        effect=effect,
    )
