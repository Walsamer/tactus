# Ownership boundaries

Normative part of [Architecture Baseline v1](SYSTEM_ARCHITECTURE.md).

| Question / responsibility | Authority | Boundary |
|---|---|---|
| WorkOrder identity, lifecycle, dependencies and readiness | Tactus | One durable domain authority. |
| Whether work may execute | Ictus authorizes the capability/route; Tactus admits against current domain facts; Dagster queues accepted execution | All three gates must succeed. READY alone is not authorization. |
| Claim and lease coordination | Tactus | Domain uniqueness, revision checks and fencing; no execution slot scheduler. |
| Backend descriptors, observed health/capacity and operator disablement | Tactus factual registry, fed by probes/runtime observations | Timestamp, provenance and expiry are facts. Unknown is not healthy. Provider quota is not a worker slot. |
| Compatibility reasoning and backend/model/provider choice | Ictus | Consume facts and requirements; produce a validated route. No ranking in Tactus. |
| Capability authorization and approval requirements | Ictus | Decision/provider candidates are untrusted until policy and capability validation. |
| Human approval/intervention records and resolutions | Tactus | Record actor, grant scope, expiry, source revision and audit history. Ictus validates their sufficiency. |
| Semantic recovery | Ictus | Bounded typed re-execution, rerouting, decomposition, escalation or abort. |
| Apply a recovery decision to a WorkOrder | Tactus | Validate applicability and authorization binding; never re-derive routing/recovery policy. |
| Create split/replan children | Tactus | Atomically materialize validated specifications, graph changes and parent effect. |
| Durable runs/steps, execution queue/concurrency, run history | Dagster | Domain stores retain references and observations, not authoritative copies of run state. |
| Execution retries and re-execution mechanics | Dagster | Only approved repeatable effects; no change to route, model or scope. |
| Launch a Dagster run worker | Dagster run launcher | The execution worker is distinct from the coding agent. |
| Start/supervise an agent process or remote runtime | Runtime adapter inside a Dagster capability | Enforce supplied restrictions and cancellation; no policy fallback. |
| Perform coding/reasoning | Agent runtime | Bounded authorized workspace/capabilities; no global orchestration. |
| Workspace, changes/stacks and authorized branch/PR publication | Stax adapter | Git/GitHub remain authoritative for actual refs and PR/merge status. |
| Verify/apply domain completion | Tactus | A successful process alone does not establish acceptance criteria. |

## Hard boundaries

Tactus must not select a preferred backend, authorize a capability, decide
retry-vs-reroute-vs-decompose, implement Dagster retries or run its own execution
queue. Durable submission records and a ready-work query are legitimate domain
coordination. Observing Dagster status for reconciliation/UI is allowed.

Ictus must not schedule, sleep through retry loops, retain a durable WorkOrder
database, create children, mutate domain state, or become a second workflow
engine. Its generic core contains generic subjects/facts/capabilities; Tactus
WorkOrder classes and lifecycle transitions belong in the Tactus adapter.

Dagster and runtime adapters must not switch backend/model/provider, widen scope,
split work, retire work, or invent human-approval decisions. A workflow registry
may mechanically map an already authorized capability to a job. That mapping is
different from selecting a semantic route.

Tactus verifies current facts against the exact route selected by Ictus. If the
route is stale/unusable, it obtains a new decision; it does not choose a fallback.
It may reject revoked approvals and disabled backends without performing policy
ranking. Changes to policy are Ictus-owned; changes to grants are audited domain
commands. No component treats a nonempty intent ID as proof of authorization.
