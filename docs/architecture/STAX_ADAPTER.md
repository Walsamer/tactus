# Stax adapter boundary

Status: M3 preflight and Git-isolated workspace lifecycle. Tactus owns the
pin, verification, and deterministic `create`/`path`/`list`/`remove` operations
for one attempt. It does not publish branches/PRs, launch agents or drive an
execution lane.

Stax is a third-party CLI (`cesarferreira/stax`) for stacked Git branches and
pull requests. Tactus treats it as an external, pinned capability behind an
anti-corruption boundary. Fleet's Stax installation, workspace pool and
controller rules are not accessible to this adapter.

## Exact pin

The reviewable machine-readable record is
[`config/stax-pin.json`](../../config/stax-pin.json). The immutable runtime
record is `tactus.integrations.stax.pin.PINNED_STAX`; a test fails if the two
drift, and changing either is a reviewed change.

| Fact | Value |
|---|---|
| Upstream | `cesarferreira/stax` |
| Version / release tag | `0.113.1` / `v0.113.1` |
| Supported platform | `aarch64-apple-darwin` (macOS arm64) |
| Release asset | `stax-aarch64-apple-darwin.tar.gz` |
| Release asset SHA-256 | `865f69488b3cc6daf2a259c842bd0c6a6c0dc5b69b70b4fd3d5aae12213293a2` |
| Executable SHA-256 | `41c69735dcffd06749fcc10d945f08dab66bd7372cc800b1b8e3a607b180eee2` |

The release-asset digest identifies the published archive. The executable digest
identifies the extracted `stax` file. Both are lowercase SHA-256 values. The
supported platform is a Rust target triple and is matched exactly; no
"compatible enough" host or architecture is accepted.

Advancing any value is a reviewed code/config change. It must re-check the
upstream release metadata, archive digest and extracted executable digest.

## Provisioning (operator, explicit)

1. Download the pinned release asset from the URL recorded by the pin.
2. Verify the archive's SHA-256 against `release_asset_sha256` **before**
   extracting it.
3. Extract the `stax` executable to an absolute installation path.
4. Verify the extracted executable's SHA-256 against `executable_sha256`.
5. Run the Tactus preflight against that absolute path.

Do **not** run `stax update`, `stax skills update`, `stax lane`, `stax submit`,
`stax sync`, `stax init`, or any other mutating command as part of provisioning
or verification. The preflight never shells out to Git. Workspace verification
adds one bounded, read-only `git rev-parse --verify HEAD^{commit}` probe for
revision evidence; it is never a fallback for a Stax worktree operation.

## Preflight sequence

`StaxPreflight.check()` / `.preflight()` is fail-closed and ordered:

1. The executable path must be absolute.
2. The final path component must not be a symlink (symlink drift is rejected).
3. The target must exist, be a regular file and be executable by this process.
4. The host/configured platform must exactly match the pin.
5. The executable file is hashed and compared to the pin **before** execution.
6. `stax --version` is invoked (no shell, bounded timeout) and its output must be
   exactly one recognizable version line matching the pinned version.
7. If a repository is supplied, read-only `stax doctor` is invoked with the
   repository as its working directory. Tactus does not pass `--fix`.

Preflight invokes only `--version` and `doctor`. After preflight passes, the
workspace adapter may invoke only these Stax subcommands:

| Operation | Stax command |
|---|---|
| create | `stax worktree create <name> --from <commit> --no-verify` |
| resolve path | `stax worktree path <name>` |
| list | `stax worktree list --json` |
| remove | `stax worktree remove <name>` |

The fake-runner tests assert that no `submit`, `lane`, `update`, `init`, `sync`,
`restack`, `prune`, `cleanup`, `--force` or `--delete-branch` is substituted.

`CommandRunner`, `platform` and the digest function are constructor-injected.
The default test suite uses a fake executable and fake runner, so it is
deterministic and runs on machines without Stax and without network access.
Production callers use the default `PINNED_STAX` and do not inject overrides.

## Workspace lifecycle

`StaxWorkspacePort` owns one Git worktree per semantic attempt. **This is Git
isolation, not an OS or security sandbox**: the agent still runs as the same
user with the same filesystem and network reach, and a same-user process can
read the canonical checkout or ambient credentials.

The deterministic workspace name depends on the attempt id. The persisted
identity binds `attempt_id`, the exact commit `base_revision`, the branch and
the resolved worktree path. Repeating an identical request reuses the same
worktree under an exclusive lease. Reusing the name with a different base
revision, branch, path or repository fails closed instead of silently creating
a second workspace.

State is written only under the configured absolute `workspace_root`:

```text
<workspace_root>/
  .tactus/
    locks/       # non-blocking flock per workspace, held for the lease
    workspaces/ # ownership manifests (attempt/revision/branch/path)
    refusals/   # JSON evidence for typed refusals
  tactus-wt-... # Stax worktrees
```

Safety rules:

1. Preflight runs before every Stax invocation, so an unknown, off-pin or
   wrong-version executable is rejected before a worktree command can mutate.
2. The workspace root must exist, must not be a filesystem root, and must be
   outside the source repository. Every Stax-reported worktree path must resolve
   strictly inside the workspace root and outside the source repository.
3. A non-blocking `flock` is held for the lifetime of `WorkspaceLease`. A
   concurrent attempt gets `WorkspaceInUseError` without touching the workspace.
4. Source and worktree revisions are checked with a read-only Git probe before
   create, reuse and remove. Drift raises `WorkspaceSourceDriftError`.
5. Removal requires an exact ownership manifest and a live, clean Stax record.
   Dirty worktrees are preserved and reported with `WorkspaceDirtyError`; the
   adapter never retries with `--force`, never deletes the branch and never runs
   `git reset --hard` or a global stash.
6. Refusal evidence is written outside the canonical checkout. Evidence failure
   never masks the typed refusal.
7. The adapter never publishes. There is no `stax lane`, `submit`, PR, merge,
   push or automatic update path.

The default test suite injects a stateful fake Stax runner. It exercises
create/reuse, identity conflicts, source drift, concurrent leases, containment
escapes, dirty preservation, off-pin refusal and removal without running a real
Stax binary, Git, network or canonical checkout.

## Typed failures

All failures derive from `StaxPreflightError`:

| Failure | Meaning |
|---|---|
| `StaxExecutableNotAbsoluteError` | path is relative |
| `StaxExecutableNotFoundError` | absent tool, or path is not a regular file |
| `StaxExecutableNotExecutableError` | file exists but is not executable |
| `StaxExecutableSymlinkError` | executable path is a symlink |
| `StaxExecutableReadError` | digest could not be computed |
| `StaxUnsupportedPlatformError` | host/configured platform is not pinned |
| `StaxExecutableDigestMismatchError` | off-pin executable (rejected before execution) |
| `StaxCommandExecutionError` | invocation failed or exited non-zero |
| `StaxMalformedVersionError` | version output absent, ambiguous or malformed |
| `StaxVersionMismatchError` | executable reported a different version |
| `StaxRepositoryNotFoundError` | repository path is not a directory |
| `StaxRepositoryNotInitializedError` | Stax reports the repository is not initialized |
| `StaxMalformedDoctorOutputError` | doctor output has no unambiguous status |

There is no boolean "ok" shortcut and no fallback path. A caller that cannot
satisfy preflight must stop or escalate; it must not invoke Git instead.

## Doctor and validate behavior

Both commands are read-only when used as described here, but they are not
equivalent:

- `stax doctor` reports configuration and repository health. It exits `0` even
  when it finds issues, so its exit code alone is not a health verdict. Tactus
  parses the explicit `✓ Repo initialized` marker and treats
  `✗ Repo not initialized` (or "Not in a Git repository") as a typed failure.
  Tactus never adds `--fix`.
- `stax validate` validates stack metadata and exits non-zero when it cannot
  validate, for example before a trunk exists. It is intentionally **not**
  invoked by this preflight: it depends on initialization and would conflate
  "uninitialized" with metadata failures. A later workspace adapter may run it
  read-only after preflight, but must not mutate a real checkout.

Tests never run either command against a real repository. The documented
behavior above is modeled by fake CLI output; no canonical checkout, real
workspace or Fleet pool is touched.

## Non-goals

- No lane/agent launching, real-agent execution or OS-sandbox claim.
- No stack restack, submit, PR publication or merge.
- No automatic Stax update or skill refresh.
- No Fleet workspace/pool reuse, Ictus policy or credentials.
- No writes to the canonical checkout and no forced cleanup.
