# Publication grants and credential interface

Status: Tactus-owned authorization guard implemented; **mock-only**. This Work
Order does not push a branch, open a pull request, call GitHub, read a real
credential or wire a live Stax publication command. It defines the boundary a
later Stax publication adapter must use.

Local source-control preparation is implemented separately in
`tactus.integrations.stax.source_control`. A prepared change records
`ImplementationOutcome.VERIFIED`/`REUSED` and always reports
`PublicationEligibility.NOT_ELIGIBLE`; it has no credential or transport path
and creates only local refs. A later publisher must still pass this guard.

Normative ownership context:

- [Ownership boundaries](OWNERSHIP_BOUNDARIES.md)
- [Local first runtime](LOCAL_FIRST_RUNTIME.md)
- [Stax adapter boundary](STAX_ADAPTER.md)
- [Work Order state model](WORK_ORDER_STATE_MODEL.md)

## Why this is separate from implementation

A successful edit run does not grant publication authority. The runtime adapter
does not start a coding agent with publication credentials, and the publication
guard never delegates its decision or credential to a coding-agent process.
Implementation completion and publication are separate effects with separate
authorization.

Tactus owns this authorization interface. Ictus remains the source of policy
decisions; the guard consumes an already-validated grant and does not
manufacture an `ALLOW` envelope. Git/GitHub remain authoritative for real refs,
pull requests and merge status; Stax remains the eventual branch/PR transport.

## Interface

`tactus.publication.PublicationRequest` is a stable publication intent. It binds:

| Field | Meaning |
|---|---|
| `intent_id` | Caller's semantic intent identity. |
| `work_order_id` | Tactus Work Order being published. |
| `repository` | Canonical `owner/name` destination repository. |
| `branch` | Review/head branch being published. |
| `base_revision` | Exact immutable 40/64-character commit, never a branch name. |
| `destination` | Publication destination/base ref (for example the PR base). |
| `scope` | Authorized repository-relative path/effect prefixes. |
| `source_revision` | Tactus source revision the intent was produced from. |
| `effect` | `OPEN_PULL_REQUEST` (default) or `PUBLISH_REVIEW_BRANCH`. |

`tactus.publication.PublicationGrant` is the consumed, already-validated grant.
It repeats the complete binding and adds:

| Field | Meaning |
|---|---|
| `grant_id` | Durable grant identity. |
| `authority` | Authenticated issuing authority. |
| `issued_at` / `expires_at` | Bounded freshness window. |
| `revoked_at` | Explicit revocation fact; any recorded revocation fails closed. |

The guard is `tactus.publication.PublicationGuard`. Its `evaluate()` method is a
pure verdict. `authorize()` raises a typed denial. `publish()` performs the
ordered effect below:

```text
1. validate trusted authority
2. validate exact WorkOrder/repository/branch/base revision/destination/
   source revision/effect binding
3. validate requested scope is covered by the grant
4. reject a recorded revocation
5. reject a not-yet-valid or expired grant
6. if the same content-addressed publication already completed, return its
   original evidence
7. resolve exactly one opaque credential through the injected provider
8. invoke the injected publication transport exactly once
9. return fully redacted evidence
```

A denial at any of steps 1-5 never calls the credential provider and never calls
the transport. The unit tests use fake providers/transports and assert both call
counts remain zero for every denial class.

## Stable publication identity

`PublicationRequest.publication_id` is content-addressed over the complete
intent (`intent_id`, Work Order, repository, branch, exact base revision,
destination, normalized scope, source revision and effect). Repeating the same
intent, including in a fresh process, produces the same identity. The guard's
in-process ledger also returns the original evidence for a completed
identity-shaped request without another credential access or publication;
concurrent duplicates of one intent are serialized so only one can resolve a
credential or publish. Reusing one `intent_id` with different content raises
`PublicationIdentityConflictError` rather than silently creating a second
publication.

## Credentials and redaction

`PublicationCredential` is an opaque handle. Its material is private and
excluded from `repr`, `str`, equality and pickling. It is passed only to the
injected transport. `PublicationEvidence` has no credential field; `as_dict()`
contains identifiers, binding facts, timestamps and an external reference only,
plus `"redacted": true`.

Credential-provider and transport failures are wrapped in typed errors without
chaining the original exception, so a provider message cannot leak secret text
through the guard. This module performs no logging.

## Tests

`tests/publication/` covers:

- positive authorization and exact one-shot credential/transport ordering;
- repeated-intent identity stability and one-effect replay;
- intent-identity conflicts;
- authority, WorkOrder, repository, branch, base-revision, destination, source
  revision, effect and scope mismatches;
- expiry, future-issued and revoked grants;
- no credential or transport access on any denial;
- provider/transport failure handling; and
- evidence/credential redaction, including serialization and pickle refusal.

## Non-goals

- No real credential store and no token value in source, tests or evidence.
- No `git push`, GitHub API, `stax submit`, `stax pr`, PR/merge operation or
  direct write to `main`.
- No Ictus policy/approval-requirement change and no fabricated decision.
- No publication privilege delegated to a coding-agent process.
- No claim that a future live transport is safe merely because this guard
  approved the intent; the live adapter must still enforce protected
  destinations, exact pins and GitHub's authoritative result.
