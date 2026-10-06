# Migration from Fleet and shared implementation intent

Normative part of [Architecture Baseline v1](SYSTEM_ARCHITECTURE.md).

## Issue and WorkOrder semantics

GitHub Issue = canonical shared implementation intent and acceptance criteria.
Fleet WorkOrder = bounded executable unit derived from a specific Issue revision.
One issue may yield several WorkOrders. Every child retains repository, issue
number/URL and source revision; completing a child does not close the Issue.
Humans and Fleet use the same backlog and meet at reviewed PRs into main.

```text
                         GitHub Issue
                       /                              human branch       Fleet WorkOrder(s)
                       |              branch(es)
                       +------ PRs ------+
                               review → main
```

No private Fleet queue replaces the public roadmap. Existing approved WorkOrders
remain execution history/authorization; reconcile them to Issues before new
successor-system work. A completed Fleet run is not evidence that its integration
commit is merged. Do not requeue blocked work as part of this documentation task.

## Intake contract

New intake MUST require an open Issue explicitly labeled `fleet:ready`, with no
`fleet:blocked`/`fleet:human` conflict, completed dependencies, bounded scope,
objective acceptance/verification, matching project/repository and operator
authorization. The label is deliberate opt-in, not a bypass of WorkOrder guards,
approvals, project pauses, budgets or scope. An Issue's existence is not permission.

Persist on every generated WorkOrder:

| Field | Meaning |
|---|---|
| repository, repository ID | Canonical owner/name plus stable identity. |
| issue number, URL, title | Human-visible source identity. |
| source updated_at / revision | Observed source revision. |
| imported_at | UTC import time. |
| source body hash | SHA-256 over the exact UTF-8 body; retain immutable body snapshot. |
| specification digest | Hash of canonical title, body and relevant execution metadata. |
| source labels/dependencies snapshot | Authorization/readiness evidence. |
| import/decomposition key | Idempotent link and parent/child provenance. |

Fetch source again before claim and immediately before dispatch/publication.
Compare the body/specification digest and authorization metadata; `updated_at`
alone may change for unrelated metadata. If intent changed, readiness was removed,
or a source cannot be checked, hold new execution and require explicit refreshed
WorkOrder approval. Never overwrite the authorization of a running attempt. An
edit during execution creates a review hold on result/publication; preserve the
original revision and effects. No polling can promise a race-free GitHub lease:
execution is bound to the checked immutable revision, and later changes must be
reconciled. A WorkSource adapter has no direct permission to mutate Fleet SQLite.

Repeated imports of one revision are idempotent. Intent changes create a reviewed
revision/supersession, not silently updated execution instructions. Status comments
and branch/PR correlation refer to the source Issue. Approval removal and closed
Issues stop new execution; an already-running process follows explicit cancel
policy. A failed publication is tracked separately from implementation completion.

## Current adapter reconciliation

Current Fleet already implements `fleet_v3/work_sources/{github,config,markdown,
service}.py`, `github_issue_links`, and ingest/status/sync commands. Retain this
work. Its default/configured label is `automation:ready`; it does not persist the
required revision/body digest and existing links skip source edits. Tactus intake
is enabled under the old label; Ictus intake is disabled. Local project identity
for Ictus is still `agentic-control`, so config `ictus` must be reconciled before
enablement. These are inspected deployment facts, not recommended settings.

Change this adapter through a separately reviewed, high-priority Issue targeting
the current Fleet repository. Establish strict `fleet:ready`, immutable provenance,
claim/dispatch freshness checks and project mapping before automated intake.
Do not enable both legacy and new readiness labels as interchangeable permissions.
The initial baseline release has no fleet:ready issues until these gates and
the baseline PR reviews are complete. Humans may take explicitly assigned
`fleet:human` work in parallel. Readiness labels are changed deliberately after
dependency completion; milestone membership does not grant readiness.

## Label and milestone vocabulary

Reuse existing `area:control-plane`, `area:ictus`, `area:dagster`, `area:runtime`
as component labels; add `area:stax` and `area:cross-system`. Add only necessary
types (`architecture`, `contract`, `implementation`, `integration`, `test`,
`migration`), priorities p0/p1/p2 and fleet:ready/blocked/human. Existing historical
status labels are not authorization. Active issues use exactly one Fleet status.
Dependencies are explicit full GitHub Issue URLs in the template; keep a DAG.

M0 Architecture Convergence; M1 Control / Decision Boundary; M2 Minimal Vertical
Slice; M3 Runtime + Development Workflow; M4 Fleet Migration; M5 Production
Hardening. These replace the older session/milestone numbering. See Issues for
the live plan, not a second checklist in a WorkOrder directory.

## Incremental and reversible transfer

1. Current Fleet may build Tactus/Ictus/Dagster through approved Issue-derived
   WorkOrders, preserving current controller publication and operator gates.
2. Reuse reviewed domain/contract work; merge via PR after verifying provenance.
   Resolve integration-only work against current main without discarding it.
3. Prove M2 in isolation with synthetic work; no authority transfer to production.
4. Shadow a narrow bounded capability and compare results without duplicate side
   effects. Explicitly assign exactly one authoritative executor per work unit.
5. Transfer one capability/project with operator approval, fenced ownership,
   in-flight drain, backup/checkpoint and a tested rollback switch.
6. Expand only after evidence. Retire a legacy responsibility after its replacement
   and rollback have been verified; eventually retire legacy Fleet if all duties
   have transferred. Never run two authoritative controllers for the same work.

Rollback stops new successor submissions, reconciles/drains existing execution
and returns ownership through an audited handoff. It does not replay active work
or rewrite history. This is a migration of responsibilities, not a big-bang rewrite.
