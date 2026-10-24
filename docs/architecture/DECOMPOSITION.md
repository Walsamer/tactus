# Bounded Decomposition (issue #10)

How a validated `DECOMPOSE` decision becomes durable child Work Orders, atomically.

Ictus decides *that* a subject must be decomposed. Tactus owns the Work Order
graph, so Tactus owns the explicit, validated materialization of the children.
Neither side invents the other's nouns.

## Layers

```text
domain        ``tactus.domain.decomposition``     plan validation + graph arithmetic
application   ``tactus.application.decomposition`` idempotent orchestration + port
persistence   ``tactus.persistence.decomposition`` one durable SQLite transaction
```

Persistence is a port (`ChildMaterializationStore`), so the domain and
application layers stay I/O-free and the transaction can be exercised
hermetically.

## 1. Validation happens before any mutation

`ChildMaterializationPlan` refuses the whole plan on the first violation:

| Violation | Error |
| --- | --- |
| zero children, or more than `max_children` | `ChildCountError` |
| duplicate child id, or a child reusing the parent id | `DuplicateChildError` |
| child scope outside the parent grant | `ScopeExpansionError` |
| child with no scope, or an unbounded `**` grant | `ChildScopeViolationError` |
| self dependency, unknown sibling, or a cycle among siblings | `ChildDependencyError` |

Scope containment is **segment** prefix containment after normalization
(`src/**` → `src`), so `src` covers `src/api/pure.py` but never `src2/x.py`.

## 2. One transaction, or nothing

The store writes children, provenance, the dependency rewrite and the
materialization ledger entry inside a single `BEGIN IMMEDIATE`. Four failpoints
(`after_children`, `after_provenance`, `after_edges`, `before_parent_effect`)
let tests inject a failure at every stage; rollback leaves no child, no edge
change, no ledger row, and an `ACTIVE` parent.

Children are always created `DRAFT`. A partially written generation is
therefore never executable, and every child must pass independent admission and
readiness on its own merits.

The parent effect is deliberately last: `RETIRED` is terminal, so nothing that
can fail is allowed to run after it.

## 3. Deterministic identity

`materialization_key(decision_id)` = parent id + decision id + SHA-256 of the
canonical plan (children, scopes, intra-child edges). Re-applying the same
validated decision finds the ledger row and returns it with `replayed=True`
instead of creating a second generation. A ledger row that disagrees with the
plan raises `LedgerConflictError` rather than silently diverging.

## 4. Provenance and readiness

Children inherit the parent's Issue revision via
`WorkOrderSourceProvenance.as_child()`, recording `parent_import_key` and
`parent_work_order_id`, so a child's completion still refers to the same Issue
revision.

`prerequisites_satisfied` counts **only** `IMPLEMENTED` upstream Work Orders.
`RETIRED` is explicitly *not* satisfied: a retired prerequisite may have been
abandoned, superseded or decomposed, and treating it as complete would let
dependent work run against output that was never produced. Unknown upstream
state also fails closed.

## Dependency rewiring

`DependencyGraph.split_work_order()` replaces one node with several, preserving
topology: every former upstream feeds each child, and each child feeds every
former downstream. Removals and additions are validated against the complete
candidate topology before a single commit, so a rejected split leaves the graph
unchanged. The rewrite returns a `DependencyChangeRecord`.

`merge_work_orders` and `replace_node` remain explicit `NotImplementedError`
extension points.

## Non-goals

No decomposition policy, no child planning, no scheduling, no retry budget and
no Ictus vocabulary live here. Scope extension beyond the parent grant is
refused rather than escalated.
