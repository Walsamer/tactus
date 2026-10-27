# Stax adapter boundary

Status: M3 preflight only. Tactus owns the pin and verification; it does not yet
create workspaces, publish branches/PRs or drive an execution lane.

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
or verification. Tactus never shells out to Git as a fallback.

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

Only `--version` and `doctor` are ever invoked by this boundary. The fake-runner
tests assert that no `git`, `update`, `lane`, `init`, `submit` or other command
is substituted.

`CommandRunner`, `platform` and the digest function are constructor-injected.
The default test suite uses a fake executable and fake runner, so it is
deterministic and runs on machines without Stax and without network access.
Production callers use the default `PINNED_STAX` and do not inject overrides.

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

## Non-goals in this Work Order

- No workspace creation or lane/agent launching.
- No branch creation, restack, submit, PR publication or merge.
- No automatic update or skill refresh.
- No Fleet workspace/pool access, Ictus policy or secrets.
- No writes to the canonical checkout.
