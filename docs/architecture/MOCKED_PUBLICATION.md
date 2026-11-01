# Mocked publication: idempotent review pull requests

Status: implemented **against a fake GitHub transport only**. There is no
network call, push to a real remote, PR creation on GitHub, credential-store
access, merge or deployment in this Work Order. Tactus's publication port stays
mocked only: `GitHubPublicationPort` refuses any transport whose contract is not
explicitly non-live and non-network, no grant issuer is decided, and every live
credential path remains a later gated Work Order after explicit publication
authority is accepted.

The `software.promote` Dagster wiring this document once deferred to "a later
gated Work Order after ICT-WO-201" now exists upstream: ICT-WO-201 landed as
Ictus `c140af3` (Walsamer/ictus#21), which adds `software.implement` and
`software.promote` as explicit Dagster workflow routes. Both are **fail-closed
placeholders** — the jobs refuse the capability and never report success, so
that backend performs no implementation and no publication either. The route is
a refusal, not a live path.

Normative neighbours:

- [Publication grants and credential interface](PUBLICATION_GRANTS.md)
- [Stax adapter boundary](STAX_ADAPTER.md)
- [Ownership boundaries](OWNERSHIP_BOUNDARIES.md)
- [Work Order state model](WORK_ORDER_STATE_MODEL.md)

## Components

| Component | Location | Responsibility |
|---|---|---|
| Grant guard | `tactus.publication` (`guard.py`, `models.py`) | Authorization, binding, scope, freshness, revocation, opaque credential. |
| Publication port | `tactus.publication.publisher.GitHubPublicationPort` | Durable publication identity, ordered publication effect, separated statuses. |
| Fake GitHub transport | `tactus.integrations.github.fake.FakeGitHubTransport` | In-memory branch/PR model with deterministic failure injection. No network. |
| Prepared change | `tactus.integrations.stax.source_control.PreparedChange` | Verified implementation evidence consumed read-only by the publisher. |

The publication port consumes an already-verified `PreparedChange`, a
`PublicationRequest` and an already-validated `PublicationGrant`. It never
stages, edits or re-runs a change; it only publishes the exact verified branch
revision.

## Ordering

```text
1. validate the prepared implementation evidence and exact change binding
2. validate authority + WorkOrder/repository/branch/base/destination/
   source/effect binding + scope + freshness + revocation
3. under one durable publication lock:
   a. reuse a matching durable publication record (same digest and branch)
   b. refuse a changed digest or a conflicting intent identity
   c. check the destination base revision and existing PR/branch state
   d. resolve exactly one opaque credential
   e. ensure one deterministic review branch
   f. create, reuse or reconcile exactly one pull request
   g. write the branch + PR identity durably
4. return separate implementation and publication statuses
```

Any refusal before step 3d leaves the credential provider and transport
untouched. Base movement, stale branches, duplicate PRs and existing PRs with a
different publication identity are detected before credential resolution.

## Durable identity

State lives only under the configured absolute state root:

```text
<state_root>/
  .tactus/
    publications/
      records/    # one durable success record per publication_id
      intents/    # intent_id -> publication_id index
      attempts/   # JSON evidence for every attempt, including refusals/failures
      locks/      # non-blocking flock per publication_id
```

A success record binds the publication identity to the Work Order, repository,
branch, exact base/source revisions, effect, `prepared_change_id`, patch digest,
branch revision and fake PR number/URL. It contains no credential or token.

Repeating the same intent and source returns the original branch and PR URL with
`PublicationStatus.REUSED`, without a second credential resolution or remote
mutation. A fresh publisher instance over the same state root behaves
identically, proving durability across processes.

The request's content-addressed `publication_id` does not include the patch
digest. The durable record therefore also binds the digest and prepared-change
identity: reusing the same publication identity with a changed digest is
refused with `DIGEST_CONFLICT` and never republishes. Reusing one `intent_id`
with a different bound intent is refused with `INTENT_CONFLICT`.

## Fake GitHub transport

`FakeGitHubTransport` is intentionally in-memory:

- no HTTP client, socket, host, URL template to a live service or token field;
- branch and pull-request state is held in dictionaries;
- PR URLs use the non-resolving `https://github.invalid/` host;
- `network_calls` is always `0`;
- every simulated mutation is recorded in `operations` for boundary assertions;
- pull-request bodies carry the `publication_id` marker so a lost acknowledgment
  can be reconciled with one read instead of a blind retry.

Deterministic failure modes:

| Scenario | Transport fact | Publication result |
|---|---|---|
| Permission failure | `GitHubPermissionDeniedError` | `FAILED` / `permission_denied` |
| Base moved | base ref revision differs | `FAILED` / `base_moved` |
| Stale source | request source differs from prepared change | `REFUSED` / `stale_source` |
| Stale branch | remote branch revision differs | `FAILED` / `stale_branch` |
| Duplicate PR | two PRs for one head/base pair | `FAILED` / `duplicate_pull_request` |
| Foreign PR | one PR with another publication id | `FAILED` / `publication_conflict` |
| Lost acknowledgment | PR created, response lost | `RECONCILED` when exactly one marker matches |
| Unreconciled acknowledgment | PR created, no match found | `FAILED` / `acknowledgment_lost` |
| Changed digest | durable identity has another digest | `REFUSED` / `digest_conflict` |
| Revoked grant | recorded revocation | `REFUSED` / `REVOKED` |

## Separate statuses

`PublicationResult` carries two independent statuses and projections:

- `implementation_status` — `VERIFIED` or `NOT_VERIFIED`, derived only from the
  prepared change;
- `publication_status` — `PUBLISHED`, `REUSED`, `RECONCILED`, `REFUSED` or
  `FAILED`.

A publication failure returns `implementation_status == VERIFIED` unchanged.
The prepared change is never mutated, and no publication path can restart agent
editing, reopen the worktree, force-push, merge or deploy. The result evidence
also records the error code, reason, simulated operations and
`network_calls == 0`, with `redacted: true`.

## Tests

`tests/publication/test_mocked_publication.py` covers:

- first approved publication of one deterministic branch and one fake PR URL,
  durably recorded;
- repeated submission across publisher instances returning the same identity
  with no extra credential or remote mutation;
- changed digest refusal;
- stale source, revoked grant, permission failure, base moved, duplicate PR,
  lost acknowledgment and unreconciled acknowledgment;
- publication failure preserving verified implementation evidence;
- separate implementation/publication statuses and redacted evidence; and
- a socket-refusing test proving the fake transport has no network path.

## Non-goals

- No real GitHub API call, `git push`, remote branch creation, PR creation,
  credential read, merge or deployment.
- No Dagster wiring from Tactus, no production credentials and no live
  publication authority. The upstream `software.implement`/`software.promote`
  routes are Ictus's fail-closed placeholders (see Status), not a publication
  path this port can use.
- No direct `main` mutation, force-push, automatic agent re-run on publication
  failure, or Fleet workspace/pool reuse.
- No claim that the fake transport is a safe live adapter. A live adapter must be
  a separate reviewed implementation behind the same protocol, with GitHub as
  the authoritative result and protected destinations enforced again.
