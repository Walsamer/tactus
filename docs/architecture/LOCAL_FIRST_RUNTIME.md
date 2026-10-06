# Local first runtime

Normative part of [Architecture Baseline v1](SYSTEM_ARCHITECTURE.md).

## Initial placement

Run locally with local repository workspaces and artifacts. Tactus provides the
composition/application entry point; Ictus remains a separate package/binary;
the generic Dagster bridge stays in Ictus. Tactus owns its software-work code
location/capabilities. Logical modules need not be Git submodules or services.

```text
local host
  Tactus application + transactional domain store
  Ictus decision invocation (local typed JSON boundary)
  persistent Dagster instance + daemon + code location
  Dagster run workers → local runtime adapter → bounded subprocess/agent
  isolated workspace + artifact root
  Stax source-control adapter (M3)
```

The M2 target domain persistence adapter is SQLite with transactions and unique
constraints, consistent with a single local host. Keep a persistence port and
migrations so a later server adapter is possible. Do not share Tactus tables with
Dagster internal storage or make Ictus a database service. Choose durable local
Dagster storage/configuration under an explicit runtime directory; its ephemeral
fallback is for tests/examples only. Production submission must reject an
ephemeral configuration.

The runtime adapter starts/supervises the requested local process, enforces
workspace/time/resource restrictions, forwards cancellation and returns typed
evidence. A Dagster Resource may inject this adapter; it is not itself a compute
allocator. Actual isolation and available resources come from the runtime/host.

## Development workflow

The latest operator instruction settles the earlier local-main ambiguity:

```text
Architecture → GitHub Issue → human branch OR Fleet WorkOrder + branch
             → PR → review → main
```

Local main follows accepted upstream work. Pending changes use isolated branches
and, where needed, Stax stacks. Stax publishes authorized review branches/PRs; it
does not authorize direct pushes to main, choose recovery, or schedule agents.
The execution agent's editing permission does not imply publication authority.
GitHub is authoritative for the PR's actual review/merge status. A publication
failure must not erase successful implementation evidence or rerun editing
without a new decision. Current Fleet retains its own controller-only publication
rules until explicitly migrated.

## Component staging and pins

| Stage | Required components | Deferred |
|---|---|---|
| M2 | Tactus, Ictus, persistent Dagster, deterministic local subprocess | Real agent, Stax, Metaxy, richer isolation/infrastructure. |
| M3 | One agent adapter and Stax workspace/publication adapter | Extra agents and remote runtime targets. |
| Later, when justified | Metaxy for concrete artifact lineage; server placement | No automatic adoption of a sandbox/vendor from the old diagram. |

Pi, SoL-Pi, Codex, OpenShell, containers and Kubernetes are runtime candidates,
not selected prerequisites. The current Fleet external-integration charter still
governs its integrations; this baseline does not adopt OpenShell into Fleet.

Pin each adopted dependency to exact version/commit, record upstream/license and
adapter compatibility, and fail closed when the required runtime is unavailable.
Ictus's inspected lockfile pins Dagster 1.13.25; preserve the lock until a reviewed
upgrade. Existing directory placeholders do not establish that external
integrations are installed. Exact Stax distribution/version and agent/runtime
pins are M3 implementation decisions with explicit review before adoption.

For future server operation keep roots, endpoints, storage and launcher choice
configurable. Remote credentials, transport authentication, shared artifact store
and stronger isolation are M5 requirements; do not distribute the system merely
to establish the first slice. No credentials belong in contracts, docs or issues.
