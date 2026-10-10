# WorkSource adapter (GitHub Issue → Tactus WorkOrder)

Normative companion to [Migration from Fleet](MIGRATION_FROM_FLEET.md) and the
`WorkSourceRevision` row in
[Cross-system contracts](CROSS_SYSTEM_CONTRACTS.md). It records the implemented
successor Tactus intake path for the first vertical slice. It does not transfer
authority and does not replace the current Fleet adapter or its database.

## Boundary

```text
mocked/local GitHub input            Tactus domain                 persistence
adapter (integrations/github)  →  WorkSourceRevision  →  WorkOrderSourceProvenance
                                     |  authorization gate        (SQLite)
                                     v
                              WorkOrder (DRAFT → OPEN)
```

* GitHub owns canonical Issue intent; the adapter only checks and normalizes it.
* Tactus owns source provenance, the content-derived import key, the
  authorization/freshness evidence and the WorkOrder binding.
* The application service (`application/import_issue.py`) orchestrates; the
  domain (`domain/work_source.py`) holds the pure invariants.

## Persisted source fields

Every generated/bound WorkOrder records the baseline contract fields:

| Field | Implementation |
|---|---|
| repository, repository ID | `RepositoryIdentity` (`owner`, `name`, `repository_id`, `url`). |
| issue number, URL, title | `WorkSourceRevision`. |
| source updated_at / revision | `updated_at` and `source_revision`. |
| imported_at | `WorkOrderSourceProvenance.imported_at` (UTC). |
| source body hash | `body_sha256` = SHA-256 over the exact UTF-8 body; the immutable `body` snapshot is retained. |
| specification digest | `specification_digest` = SHA-256 over canonical title, body and dependency metadata. |
| source labels/dependencies snapshot | `labels` and `dependencies` tuples. |
| import/decomposition key | `ImportKey` = `repo#issue@specification_digest`, plus parent import/WorkOrder references. |

`updated_at` is retained as observed evidence but is intentionally **not** part
of the change detector or the import key: GitHub changes it for unrelated
metadata.

## Authorization and idempotency

New intake fails closed unless the checked revision is an **open** Issue with
`fleet:ready` and no conflicting `fleet:blocked`/`fleet:human` label. An open
Issue or milestone alone is not permission.

The import key is content-derived, so:

* importing the same revision repeatedly is idempotent and returns the original
  deterministic WorkOrder;
* an edited title/body/dependency set yields a distinct WorkOrder identity and
  never mutates the existing binding;
* binding one explicit WorkOrder to a different revision raises
  `ImmutableSourceConflictError` and preserves the original authorization.

## Freshness at claim/dispatch

`recheck_source` re-fetches the canonical revision and returns typed
`SourceHold` evidence:

| Reason | Meaning |
|---|---|
| `SOURCE_CHANGED` | body/specification digest differs from the stored snapshot. |
| `SOURCE_UNAVAILABLE` | the source could not be checked (fail closed). |
| `AUTHORIZATION_REMOVED` | `fleet:ready` is no longer present/valid. |
| `ISSUE_CLOSED` | the Issue is no longer open. |

Holds convert to typed readiness blockers (`SOURCE_CHANGED` with a typed
`subreason`). Applied to an `OPEN` WorkOrder they block admission; they never
rewrite an accepted attempt's authorization. An `updated_at`-only change is not
reported.

## Children and Issue lifecycle

A child WorkOrder created from a parent's revision preserves the parent Issue
provenance (`parent_import_key`, `parent_work_order_id`, same revision) through
`WorkOrderSourceProvenance.as_child` / `SqliteWorkSource.bind_child`. Completing
one child therefore does not close or rewrite the Issue; all Issue acceptance
criteria and reviews remain authoritative.

## Explicit local import command

```bash
uv run python -m tactus.cli import-issue \
    --source tests/integrations/github/data/issue_21.json \
    --database /tmp/tactus-work-source.sqlite \
    --repository walsamer/tactus --issue 21 [--check]
```

`--check` rechecks authorization/freshness and reports typed holds. The command
uses the deterministic offline `MockGitHubIssueSource`; no network or Fleet
database is involved.
