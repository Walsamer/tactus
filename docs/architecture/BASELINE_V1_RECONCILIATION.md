# Baseline v1 reconciliation

Inspection: 2026-10-06. Classifications distinguish implemented main, integration
candidates and target behavior. Runtime was inspected read-only; no Fleet recovery,
state mutation, deployment or integration promotion is part of this change.

## Evidence and coverage

Reviewed both READMEs, architecture/roadmap/status/contracts, complete source/test
layouts and relevant implementation, contribution/CI gates, issue templates (none
previously), labels/milestones, all Tactus Issues #1–12, PRs #13–18 and Ictus's empty
Issue/PR history. Tactus had no open PRs; #13–15 and #18 merged; #16–17 closed
unmerged. There were no hidden open Ictus implementation issues to preserve.

| Repository/ref | Commit | Interpretation |
|---|---|---|
| Tactus GitHub main | `96d964958dbaca43cfbb1aed3732d9e249e0ffb0` | Baseline branch starts here. |
| Tactus local main | `978bda4ea96e596aeac1b830a2dff69244bb3938` | Different history, same committed tree `c140d52a11a54e8236cc7cc49bcb355e4c21cacd`. |
| Tactus Fleet integration | `7cbd91d9ebd5835212d654cd5fa336388bbc773b` | FIX-007 decision application candidate, not merged main. |
| Ictus GitHub/local main | `833175d88adeed59f8e62c14c5e5577580cfdf33` | Generic foundation, tree `0e3dd632ddca6b043a1b1e357f4c55d29189f08c`. |
| Ictus Fleet integration | `5ee42ea5602703b8aafd8cab9b88e3feb8347087` | FIX-006 generic vocabulary candidate, not merged main. |

The local audit `audits/tactus-ictus-dagster/06-original-intent-and-system-design.md`,
`07-audit-reconciliation.md`, and `08-setup-options-and-decisions.md` and original
whiteboard were inspected. They are source evidence, not additional canonical
specifications. Baseline v1 resolves their open questions: ACTIVE includes accepted
pending dispatch/queue time; M2 uses one run per semantic attempt; local main tracks
accepted work; Stax is M3, not a prerequisite of the simple M2 proof. Tiny ambiguous
handwritten timings are not adopted as requirements.

## Significant implementation and documentation items

| Existing behavior / assumption | Classification / conflict | Correct owner | Recommended action and affected area | Migration risk |
|---|---|---|---|---|
| Five lifecycle states, OPEN readiness, guarded transitions, acyclic dependency graph | KEEP; matches baseline | Tactus | Preserve `domain/work_order.py`, `dependency.py` and tests; #1/#2 remain completed. | Low; preserve identity/history. |
| `ExecutionAdmission` uses dictionaries and requires run ID at accept | KEEP + MODIFY; not restart-safe, cannot safely bridge lost acknowledgement | Tactus | #4 durable claim/fencing, attempts and outbox/inbox; accept stable intent before attaching receipt. | High; duplicate live execution. |
| `ExecutionRequest` construction is documented as proof of authorization | SUPERSEDE trust assumption | Ictus validation / Tactus applicability | #6 require trusted validated envelope and subject/source binding; remove misleading docstring in that implementation PR. | High; forged/stale authorization. |
| Backend registry/health and provider quota facts exist | KEEP + MODIFY; facts are correct, initial descriptor contract incomplete for Ictus | Tactus facts | #5 retained/re-scoped to versioned observed facts, expiry and full descriptors; #6 consumes only selected route. | Medium; unknown health mistaken for usable. |
| `BackendRegistry.compatible()` performs mechanical constraint filtering | MOVE RESPONSIBILITY for authoritative compatibility | Ictus | Routing issue owns decision; retain helper temporarily as nonauthoritative compatibility utility, no preferred ordering; retire callers incrementally. | Medium; rejecting candidates before policy sees facts. |
| Snapshot requires previous observation and emits different fact keys than Ictus reads | KEEP + MODIFY | Tactus adapter / Ictus profile | #7 plus Ictus context contract: initial phase, explicit semantic budget/count; no fabricated observation. | High; exhausted 0/0 defaults, no initial execution. |
| Inbound observation parser is strict and execution-only | KEEP | Tactus adapter / Ictus result contract | Retain FIX-004 in #7/#12 fixtures; add correlation/freshness at composition layer. | Low; avoid broad domain-to-UNKNOWN mapping. |
| Integration FIX-007 parses raw proposal, drops subject/provider/evidence, applies mutable in-memory effects | KEEP + MODIFY; not proof of policy validation or durable idempotency | Ictus produces / Tactus applies | #8 reuse candidate only after bound envelope and transactional application; do not auto-merge integration. | High; wrong-target/replayed effects. |
| Attempt closure and recovery reopening are separate operations | KEEP + MODIFY | Tactus | #4/#8 coordinate closure and effect in one transaction. | High; reopened work still has active attempt. |
| InMemoryChildCreation and parent-retire-last ordering | KEEP + MODIFY; no durable transaction | Tactus | #10 materialize plan, graph and parent effect atomically. | High; partial child activation. |
| Typed general BlockReason absent despite FIX-007 prerequisite | NEW GAP | Tactus | #3 implement typed blockers; completed worker status does not waive missing acceptance. | Medium; unblocking unrelated conditions. |
| Ictus generic contracts/core/ports and three domains | KEEP | Ictus | Preserve generic design; integrate through profile, not WorkOrder classes in core. | Low if fixtures retain other domains. |
| FIX-006 adds generic kinds and Proposal v2, route fields but no full selector | KEEP + MODIFY | Ictus | Review/promote vocabulary within context/decision contract issue; implement compatibility/routing separately. | Medium; supported enum mistaken for behavior. |
| Ictus rules/evaluator read retry.attempt/budget, default missing to 0; SUCCESS produces ABORT | KEEP + MODIFY | Ictus semantic policy; Tactus success application | Semantic recovery issue aligns profile; #8/#12 complete success directly. | High; incorrect escalation/retirement. |
| Python hand validators cover only part of wire semantics | KEEP + MODIFY | Ictus contracts/bridge | Shared conformance fixtures include bool-as-integer rejection, schema versions, authorization absence and unknown kinds. | Medium; cross-language disagreement. |
| Bridge uses persistent DAGSTER_HOME when set but execute_in_process | KEEP + MODIFY; history exists, independent queued launch not proved | Dagster integration in Ictus | Durable submission issue adds receipt/query/deduplication; keep demos. | High; duplicate dispatch on restart. |
| Result mapping uses coarse process-crash classification | KEEP + MODIFY | Ictus execution bridge / runtime adapter | Preserve raw evidence, map known timeout/cancel/failure facts accurately without semantic recovery decisions. | Medium; policy fed misleading facts. |
| Older docs repeat ownership and session plans; README omits merged adapters | SUPERSEDE | Canonical architecture + GitHub Issues | Replaced with seven canonical specs, reconciliations and compatibility links; historic versions remain in Git history. | Low; update entry links. |
| Whiteboard READY/BLOCKED/FAIL states and Tactus capacity scheduler | SUPERSEDE | Tactus orthogonal readiness / Dagster queue | Keep source image as history. No failed lifecycle state or Tactus slot scheduler. | Medium if old issues remain active. |
| Runtime placeholders select OpenShell/Pi by default; Stax required before first probe | SUPERSEDE as M2 prerequisites | Runtime/Stax later | Simple local process M2; explicit agent/Stax selection M3. Metaxy remains optional lineage. | Low; no implementation removed. |
| GitHub→Fleet adapter exists with automation:ready, no source digest/freshness | KEEP + MODIFY | Current Fleet work-source boundary | High-priority intake issue; use fleet:ready exclusively, revision checks, correct Ictus project mapping. | High; stale or unintended execution. |
| Fleet FIX-008 blocked; FIX-009 waiting on dependency | KEEP evidence; not architectural impossibility | Current Fleet operator | Runtime stall was recorded for FIX-008; new Dagster/slice Issues own intent. Explicit reconciliation before any retry/resume. | High if duplicate work is launched. |

No functioning source is deleted in this PR. REMOVE is reserved for obsolete
authorization assumptions, duplicate active roadmap text and superseded issue
instructions; code removal needs its own reviewed migration acceptance evidence.

## Existing issue reconciliation

| Old issue | Classification | Owner | Action / replacement |
|---|---|---|---|
| #1 lifecycle architecture (closed) | KEEP | Tactus | Preserve completed history; baseline state model confirms it. |
| #2 domain implementation (closed) | KEEP | Tactus | Preserve merged core and tests. |
| #3 typed BlockReason | KEEP + MODIFY | Tactus | Keep number; add independent blocker resolution and freshness semantics. |
| #4 SchedulerPort/claim | KEEP + MODIFY | Tactus | Replace capacity scheduler objective with durable domain claim/admission/correlation. |
| #5 backend registry/health | KEEP + MODIFY | Tactus | Acknowledge merged facts; remaining scope is versioned descriptors/freshness, not rebuilding registry. |
| #6 scheduler backend selection | MOVE RESPONSIBILITY | Ictus selection; Tactus revalidation | Retain #6 for consuming/revalidating selected intents; Ictus routing issue owns actual selection. |
| #7 recovery observation | KEEP + MODIFY | Tactus adapter | Retain inbound parser; extend initial/recovery context and align semantic facts. |
| #8 RecoveryDecision | KEEP + MODIFY | Ictus produces; Tactus applies | Reuse integration-only applier after trusted envelope, correlation and transactions. |
| #9 bounded retry/reroute | SUPERSEDE / MOVE RESPONSIBILITY | Ictus | Close as not planned in Tactus; replacement is Ictus semantic recovery plus #8 domain application. |
| #10 split/replan | KEEP + MODIFY | Tactus | Validated plan only; atomic children/graph/parent, deferred to M3. |
| #11 human intervention | KEEP + MODIFY | Tactus records, Ictus requirements | Typed grants/resolutions, revocation and revalidation. |
| #12 vertical slice | KEEP + MODIFY | Cross-system | Five mandatory scenarios, simplest runtime, no Stax/agent prerequisite. |

Each changed issue receives a reconciliation comment preserving its prior body;
closed historical issues are not silently rewritten. Exact links and the live
dependency graph are in [the roadmap](../ROADMAP.md). #16/#17 PRs remain closed;
#18 already contains their corrected replacements. No redundant implementation
issue is opened for those merged slices.

## Open implementation choices

M1 fixes schema/profile version numbers and validated-envelope representation
without changing ownership. M3 selects exact Stax/agent pins and publication
capability details. M5 defines run-family re-execution, server storage/transport
and operational SLOs. M2 defaults are explicit: local SQLite domain storage,
persistent Dagster, one run/semantic attempt and deterministic local process.
These choices do not block baseline agreement or justify another scheduler.
