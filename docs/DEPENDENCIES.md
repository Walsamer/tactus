# Tactus Dependencies

Initial dependency landscape. Versions are intentionally not pinned yet.

| Component | Ownership | Role | Importance |
|---|---|---|---|
| Tactus | First-party | Control plane / master repository | Core |
| Ictus | First-party external | Decision/policy layer | Core |
| Dagster | Third-party | Durable execution | Core |
| Metaxy | Third-party | Dagster-integrated lineage/provenance | Relevant / workload-dependent |
| OpenShell | Third-party | Sandbox backend | Default candidate / replaceable |
| Pi | Third-party | Agent runtime | Initial default |
| SoL-Pi | Third-party | Pi optimization | Optional |
| Git | External | Source-control substrate | Core |
| Stax | Third-party | Higher-level Git/worktree/change workflow | Important |
| Pixi | Third-party | Reproducible environment/bootstrap | Important tooling |
| Arcventory | First-party private | Private optional future integration | Not current dependency |

## Known upstream locations

| Component | Upstream / source |
|---|---|
| Tactus | This repository (`Walsamer/tactus`) |
| Ictus | `Walsamer/ictus` |
| Dagster | `dagster-io/dagster` |
| Metaxy | Third-party provenance/metadata system |
| OpenShell | Third-party sandbox backend |
| Pi | Third-party agent runtime |
| SoL-Pi | Third-party Pi optimization/extension layer |
| Git | `git/git` (source-control substrate) |
| Stax | Third-party Git/worktree/change workflow tool |
| Pixi | `prefix-dev/pixi` |

Private or organization-internal systems are deliberately not documented here.
Arcventory is reserved (`integrations/private/arcventory/`) but is **not** part
of the current architecture and adds no dependency.
