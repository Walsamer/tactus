# Live publication: one authorized review pull request

Status: implemented. `tactus publish` can open a real review pull request for a
verified prepared change, authorized by a durable operator grant, using a
fine-grained token that never enters a coding agent's environment. The mocked
port remains hard-locked, review→main remains out of scope, and nothing in this
path merges, force-pushes or touches the trunk.

Normative neighbours:

- [Publication grants and credential interface](PUBLICATION_GRANTS.md)
- [Mocked publication](MOCKED_PUBLICATION.md)
- [Stax adapter boundary](STAX_ADAPTER.md)
- [Ownership boundaries](OWNERSHIP_BOUNDARIES.md)

## The credential finding, stated as a finding

This is the most important thing on this page, so it comes first.

**The publication credential is not isolated from the coding agent, and this
Work Order does not pretend otherwise.** The supervised child gets a names-only
environment (`PATH`, `TACTUS_ATTEMPT_ID`, `TACTUS_INTENT_ID` —
`src/tactus/runtime/supervision.py`), and that function's own docstring says why
that is not enough: *"hygiene, not an isolation boundary: the child can still read
whatever the OS account can, including ambient network access and any credential
on disk."* The agent runs as the same OS account as the controller, so any
credential in a file is readable by it, and any credential in the controller's
memory is one same-user process read away.

What was true on the development machine before this change, verified:
`gh auth status` reports a **user OAuth token** (`gho_…`) with scopes
`gist, read:org, repo` and `permissions.admin: true` on `Walsamer/tactus`; Stax
resolves that same token (`stax auth status` → active source `gh auth token`). A
token like that writes every branch of every repository the account can push to
and can **edit or disable the "Protect main" ruleset itself**, which is the very
control the rest of this document leans on. That is the exposure the previous
state already had, before any publication code existed.

What this change does about it is narrow the token, not isolate it:

- the publication token is read from **one file at an absolute path outside the
  repository** (`FileTokenCredentialProvider`), never from `gh auth token`, never
  from the child environment, never from an ambient login;
- a **fine-grained token** is required. A classic token is refused because it
  reports OAuth scopes, which are repository-wide by definition;
- a token with **admin** permission is refused, and a read-only token is refused;
- the material never appears in argv, evidence, a log line or an exception
  message — it lives in an opaque `PublicationCredential` until a transport puts
  it in the environment of one subprocess.

The achievable win is a credential that can only open review branches and pull
requests in one repository. **Agent isolation is not claimed**, is not delivered
here, and needs an OS-level boundary (a separate account, a container or a
remote execution host) that this repository does not have.

## Where the credential is used

| Rule | Why |
|---|---|
| `GH_TOKEN` in the environment of one `gh` subprocess | `gh` prefers it over stored authentication, so an operator's `~/.config/gh` login is never used. |
| `git -c credential.helper= -c credential.helper=…` before `push` | The empty value resets the helper list first, so an ambient keychain or credential manager cannot supply a different credential. The token is read from `GH_TOKEN` inside the helper. |
| Never in argv | argv is world-readable in `ps`. |
| The token file must live outside the repository | It is never committed and never inside a workspace handed to an agent. |
| The token file's scope is checked before any publication | A provider that would publish with too broad a token is never constructible. |

## Two ports, one state machine

| Port | Accepts | Reads |
|---|---|---|
| `GitHubPublicationPort` (mocked) | a **non-live, non-network** transport, enforced in the constructor | before resolving a credential: base movement, stale branch, duplicate/foreign pull requests are all detected with no credential in hand |
| `GitHubLivePublicationPort` (live) | a transport that is **live and networked**, enforced in the constructor | resolves and binds the one credential **before** reading, because a live transport cannot read a private repository without it |

This is the only behavioural difference. Both share one implementation, so the
create/reuse/reconcile rules cannot drift; `_requires_live_transport` is a class
attribute, **never** a setting, flag or environment value. There is no
configuration that turns the mock into a live transport: a live publication is a
different class, visible in review.

`tests/publication/test_live_port_lock.py` pins exactly that: the mocked port
raises on a live transport, the live port raises on the mock, and each resolves
its credential on the side of the reads its contract states.

## What the transport refuses

`tactus.integrations.github.gh.GhGitHubTransport` implements the shared
`GitHubTransport` contract over `git push` and `gh api`. `git` and `gh` are the
only programs it runs, always as an argv list, never through a shell.

| Refusal | Error | Test |
|---|---|---|
| A push that is not a fast-forward | `GitHubNonFastForwardPushError` (a `GitHubBranchConflictError`) | `test_a_rejected_non_fast_forward_push_is_refused_never_forced` |
| A push onto the trunk or the authorized base | `GitHubProtectedDestinationError` | `test_publishing_onto_the_trunk_is_refused_before_any_command`, `test_publishing_onto_the_authorized_base_branch_is_refused` |
| A pull request against any base but the authorized one, or at any base revision but the authorized one | `GitHubProtectedDestinationError` | `test_a_pull_request_against_another_base_is_refused_before_any_command`, `test_a_pull_request_for_another_base_revision_is_refused` |
| A pull request with the trunk as its head | `GitHubProtectedDestinationError` | `test_a_pull_request_with_the_trunk_as_head_is_refused` |
| Publishing with no credential bound | `GitHubTransportError` | `test_an_unbound_credential_refuses_before_any_command` |

`--force` is never passed. A rejected non-fast-forward push is **the boundary
working**, not an obstacle: it surfaces as a typed refusal, the verified revision
stays where it was, and nothing retries with a stronger flag. That is why the
refusal has its own test with its own name — `--force` is a flag someone
eventually adds, and the test is the thing that stops them.

The port maps a protected-destination refusal to `REFUSED` /
`protected_destination`, not `FAILED`: the publication was refused by policy, not
broken by a transport fault.

## Binding a grant to a branch at a revision

The grant is the *recorded human authorization*, not a policy verdict. It is
restated by one pure function,
`tactus.publication.publication_grant_from_human_grant`, which refuses a human
action other than `PUBLISH`, a grant recorded for another Work Order or another
source revision, an empty scope, and the trunk as a head branch.

The binding the port enforces:

| Field | Source |
|---|---|
| `branch`, `base_revision`, `branch_revision` | the verified `PreparedChange` |
| `source_revision` | the prepared change, which must equal the grant's source revision |
| `repository` | canonical `owner/name` from the recorded provenance |
| `destination` | the operator's explicit base, never inferred |
| `scope` | the recorded grant's scope, normalized and refused when empty |
| `grant_id`, `authority`, `issued_at`, `expires_at`, `revoked_at` | the durable `HumanGrant` |

A grant that has moved is refused: a different base revision is
`CHANGE_BINDING_MISMATCH`, a moved remote base is `BASE_MOVED`, a branch at
another revision is `STALE_BRANCH`.

## Replay, idempotency and what a record proves

The publication identity is content-addressed over the intent, and the intent id
is derived only from durable facts (`publish:<grant_id>:<prepared_change_id>`) —
no clock, no randomness. Repeating the same publication therefore returns the
original branch and pull request with `REUSED`, without a second credential
resolution or remote mutation, even in a fresh process.

The durable success record names the facts that matter for audit, including the
grant:

```text
publication_id, intent_id, intent_digest, work_order_id, repository, branch,
base_revision, destination, source_revision, scope, effect,
prepared_change_id, patch_digest, branch_revision,
pull_request_number, pull_request_url, publication_status, published_at,
grant_id, authority, grant_issued_at, grant_expires_at
```

`grant_id` and `authority` are what turn "a pull request exists" into "this grant
authorized that pull request". The record never contains credential material.

Revocation and expiry are refused **before** the reuse path, so a late retry with
a revoked grant cannot re-publish. It also cannot read the URL back through
`publish()`; the recovery path is `port.evidence(publication_id)`, which needs no
grant. Every attempt — published, reused, reconciled, refused, failed — writes
attempt evidence, so a refusal never erases implementation evidence.

## Why `gh`, and not Stax, for publication

Stax stays the workspace and change-preparation adapter. It is not the publisher:

- `stax stack submit --dry-run --json` refuses Tactus-prepared branches outright
  (`Error: No metadata for branch …`); giving it metadata needs
  `stax branch track`, a mutating command the Stax adapter deliberately does not
  allow, and the allowlist would have to grow;
- it resolves the same ambient `gh auth token`, so it offers no credential gain;
- the pinned CLI also exposes `stax merge`, so adopting it as the publisher would
  widen the reachable command surface to the exact effect the rules forbid;
- its push semantics cannot be verified from the pinned binary without pushing.

`git push` + `gh api repos/…/pulls` is also what the current controller does, so
this is a proven shape rather than a new one. See
[Stax adapter boundary](STAX_ADAPTER.md).

## Review → main is deliberately out of scope

Tactus opens a review pull request and **observes** what happens to it. It does
not merge, and there is no merge code path here.

The repository's `main` is governed by an active ruleset, verified with
`gh api /repos/Walsamer/tactus/rulesets`:

| Rule | Setting |
|---|---|
| Deletion | refused |
| Force-push (`non_fast_forward`) | refused |
| Linear history | required (`squash` and `rebase` only) |
| Pull request required | yes, **1 approving review**, stale reviews dismissed on push, review thread resolution required |
| Status check | `python (pytest + verifier)`, **strict** (the branch must be up to date with `main`) |
| Bypass | one actor: `RepositoryRole` id 5 (admin), `bypass_mode: always` |

Two honest caveats:

1. **The approval requirement was zero until this change.** While
   `required_approving_review_count` was `0`, every pull request merged into
   `main` — including every Tactus pull request before this one — was merged with
   no required review at all. Green CI plus push access was the entire gate. The
   rule is now 1, so the gauntlet is real for anyone who is not an admin.
2. **Admin can bypass it.** The ruleset lists the admin repository role with
   `bypass_mode: always`, and this repository has a single identity, so the
   operator can still merge their own pull request. The rule binds contributors
   and automation, not the owner. Removing that bypass is an operator decision,
   and with one identity it would block that identity from repairing `main`.

What must be true before Tactus merges anything itself: publication proceeds
through an Ictus-validated grant rather than an operator-recorded one; a human
review requirement stays in place so Tactus cannot approve itself; `stax merge`
and any admin merge stay unrepresentable in this adapter; and an operator
explicitly transfers that capability with a tested rollback (see
[Migration from Fleet](MIGRATION_FROM_FLEET.md), issue #24).

## Operator runbook

```bash
tactus publish \
  --grant <durable-grant-id> \
  --work-order <work-order-id> \
  --prepared-change <recorded-change-id> \
  --database <human-intervention.sqlite> \
  --state-root <absolute-state-root> \
  --repository Walsamer/tactus \
  --repository-root <absolute git checkout to push from> \
  --token-file <absolute path outside the repository> \
  --base main
```

The command prints redacted publication evidence as JSON and exits:

| Exit | Meaning |
|---|---|
| `0` | Published, reused or reconciled — a review pull request exists |
| `1` | A publication attempt happened and was refused or failed; the result says which, and implementation evidence is intact |
| `2` | The operator facts were wrong: unknown grant, unknown prepared change, a refused credential scope, or a bad path |

### Failure modes and what to do

| Result | Meaning | Action |
|---|---|---|
| `REFUSED` / `REVOKED`, `EXPIRED`, `DENIED` | the grant no longer authorizes | record a new grant; do not retry the old one |
| `REFUSED` / `digest_conflict`, `intent_conflict` | the durable record is bound to different content | investigate; never republish over it |
| `REFUSED` / `protected_destination` | the transport was asked to write the trunk or an unauthorized base | a defect: the request should never have been built |
| `FAILED` / `base_moved` | `main` advanced past the authorized revision | re-prepare against the new base; do not re-force |
| `FAILED` / `stale_branch` | the remote review branch is at another revision | inspect the branch; a human decides, Tactus never force-updates |
| `FAILED` / `permission_denied` | the token cannot write, or the branch is protected | check the token's scope and the destination |

## Non-goals

- No merge, no `stax merge`, no deploy, no release.
- No direct write to `main`, no force-push, no branch deletion.
- No automatic retry of agent editing to repair a publication failure.
- No Ictus policy change, no fabricated `ALLOW`, no grant-sufficiency verdict.
- No OS-level credential isolation, and no claim of it.
- No GitHub App token minting (that would add the project's first runtime
  dependency); the token is an operator-managed fine-grained PAT.

## Tests

| Area | File |
|---|---|
| Grant projection | `tests/publication/test_grant_projection.py` |
| Live transport, force and destination refusals | `tests/integrations/github/test_gh_transport.py` |
| Port lock and credential-before-reads | `tests/publication/test_live_port_lock.py` |
| Token provider scope preflight | `tests/publication/test_credentials.py` |
| Composition, replay, revoked grant | `tests/application/test_publish_change.py` |
| CLI wiring and refusals | `tests/test_cli_publish.py` |

None of them touches the network, runs `git push` or holds a real token.
