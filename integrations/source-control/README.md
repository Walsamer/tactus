# Source control

Git is the underlying source-control truth. Stax is the higher-level
source-control/workspace/change-management capability and is important to
Tactus's future push/pull workflow.

Expected responsibility:

- worktrees, isolated change lanes, branches
- stacked changes, stacked PRs, restacking
- synchronization, publishing changes, safe recovery

Tactus should eventually expose a `SourceControlPort` with Stax as the primary
implementation. Raw Git/Stax-specific semantics must not be scattered
throughout the Tactus core.
