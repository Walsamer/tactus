"""Compose one operator grant, one verified change and one publication effect.

This is the seam the publication port was missing: everything exists to publish
a real review pull request except the piece that joins durable facts to an
authorized effect. It joins them and nothing more.

What it does:

* takes a durable :class:`~tactus.domain.human_intervention.HumanGrant`, the
  verbatim record that a human authorized publication;
* takes the :class:`~tactus.integrations.stax.PreparedChange` the source-control
  adapter durably recorded for that exact specification;
* restates the grant as a bounded publication grant (no policy, no sufficiency
  verdict, no fabricated ``ALLOW``);
* builds the content-addressed publication intent from those durable facts, so a
  retry of the same join produces the same publication identity;
* runs exactly one publication through the injected port and returns its result.

What it deliberately does not do: merge, retry, re-run an agent, widen scope,
choose a base branch, decide whether the human was allowed to grant, or turn a
grant into an authorization. A refused or failed publication returns its result
with the implementation evidence intact; there is no retry loop here.

See ``docs/architecture/LIVE_PUBLICATION.md``.
"""

from __future__ import annotations

from datetime import datetime

from tactus.domain.human_intervention import HumanGrant
from tactus.integrations.stax.source_control import PreparedChange
from tactus.publication.grant_projection import publication_grant_from_human_grant
from tactus.publication.models import (
    PublicationEffect,
    PublicationGrant,
    PublicationRequest,
)
from tactus.publication.publisher import PublicationPort, PublicationResult

__all__ = [
    "PublicationCompositionError",
    "publication_intent_id",
    "publication_request_for",
    "publish_change",
]


class PublicationCompositionError(ValueError):
    """The durable facts do not describe one publishable change."""


def publication_intent_id(grant: HumanGrant, change: PreparedChange) -> str:
    """Stable intent identity for one grant publishing one prepared change.

    Derived only from durable facts and therefore identical across processes,
    machines and retries. It contains no clock reading and no randomness, which
    is what makes a repeated publication a ``REUSED`` result rather than a
    second pull request.
    """

    if not isinstance(grant, HumanGrant):
        raise PublicationCompositionError("grant must be a HumanGrant")
    if not isinstance(change, PreparedChange):
        raise PublicationCompositionError("change must be a PreparedChange")
    return f"publish:{grant.grant_id}:{change.prepared_change_id}"


def publication_request_for(
    grant: HumanGrant,
    change: PreparedChange,
    *,
    repository: str,
    destination: str,
    effect: PublicationEffect = PublicationEffect.OPEN_PULL_REQUEST,
) -> tuple[PublicationRequest, PublicationGrant]:
    """Return the publication intent and its bounded grant for these facts."""

    publication_grant = publication_grant_from_human_grant(
        grant,
        change,
        repository=repository,
        destination=destination,
        effect=effect,
    )
    request = PublicationRequest(
        intent_id=publication_intent_id(grant, change),
        work_order_id=publication_grant.work_order_id,
        repository=publication_grant.repository,
        branch=publication_grant.branch,
        base_revision=publication_grant.base_revision,
        destination=publication_grant.destination,
        scope=publication_grant.scope,
        source_revision=publication_grant.source_revision,
        effect=publication_grant.effect,
    )
    return request, publication_grant


def publish_change(
    *,
    grant: HumanGrant,
    change: PreparedChange,
    repository: str,
    destination: str,
    port: PublicationPort,
    effect: PublicationEffect = PublicationEffect.OPEN_PULL_REQUEST,
    at: datetime | None = None,
) -> PublicationResult:
    """Publish one authorized change exactly once through *port*."""

    request, publication_grant = publication_request_for(
        grant, change, repository=repository, destination=destination, effect=effect
    )
    return port.publish(request, publication_grant, change, at=at)
