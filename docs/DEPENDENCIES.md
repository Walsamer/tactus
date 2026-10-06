# Dependency policy

See [local runtime and staging](architecture/LOCAL_FIRST_RUNTIME.md) for the
baseline's component choices. Ictus is independently versioned; its generic
Dagster bridge stays in that repository. New adopted integrations require an
exact pin, provenance, compatibility check and fail-closed preflight. Placeholder
directories are not installed dependencies.

M2 requires Tactus, Ictus, persistent Dagster and a deterministic local runtime.
Stax and one agent are M3 work. Metaxy, richer sandbox implementations, Pixi and
remote infrastructure are optional until a concrete acceptance case requires them.
No external source is vendored or absorbed by this documentation baseline.
