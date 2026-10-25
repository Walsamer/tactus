"""Workspace boundary and changed-path scope validation.

A workspace here is a **git worktree or directory the agent may edit**, plus the
relative paths it is allowed to touch. Two different things are deliberately
kept apart:

* **write scope** (this module): which paths inside the workspace an agent
  may change. Enforced on the *reported* changed paths and on the patch headers,
  after the process has finished.
* **OS isolation** (not implemented here): filesystem and network confinement.
  Nothing in this module restricts what a child process can read or reach; the
  child runs as the same OS account. Issue #23 owns the workspace boundary, and
  even there the claim is "Git worktree isolation", not an OS sandbox.

So: a path-scope violation is *detected and rejected*, never *prevented*.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Workspace",
    "WorkspaceScopeError",
    "normalize_changed_path",
    "patch_header_paths",
    "validate_changed_paths",
]


class WorkspaceScopeError(ValueError):
    """A path or workspace violates the declared write scope."""


def normalize_changed_path(raw: str) -> str:
    """Normalise one repository-relative path, rejecting anything unsafe."""
    if not isinstance(raw, str) or not raw.strip():
        raise WorkspaceScopeError("changed path must be a non-empty string")
    candidate = raw.strip().replace("\\", "/")
    if candidate.startswith("/"):
        raise WorkspaceScopeError(f"changed path must be repository-relative: {raw!r}")
    normalized = posixpath.normpath(candidate)
    if normalized in ("", "."):
        raise WorkspaceScopeError(f"changed path does not name a file: {raw!r}")
    if normalized == ".." or normalized.startswith("../"):
        raise WorkspaceScopeError(f"changed path escapes the workspace: {raw!r}")
    return normalized


def _scope_covers(scope_entry: str, path: str) -> bool:
    entry = scope_entry.rstrip("/")
    return path == entry or path.startswith(f"{entry}/")


@dataclass(frozen=True, slots=True)
class Workspace:
    """A directory an agent may edit, plus the relative paths it may change."""

    root: Path
    allowed_scope: tuple[str, ...]
    forbidden_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        root = Path(self.root)
        if not root.is_absolute():
            raise WorkspaceScopeError("workspace root must be an absolute path")
        resolved = root.resolve()
        if not resolved.is_dir():
            raise WorkspaceScopeError(f"workspace root is not a directory: {resolved}")
        scope = tuple(self.allowed_scope)
        if not scope:
            raise WorkspaceScopeError("workspace must declare a non-empty allowed scope")
        normalized_scope: list[str] = []
        for entry in scope:
            if not isinstance(entry, str) or not entry.strip():
                raise WorkspaceScopeError("allowed scope entries must be non-empty strings")
            normalized = normalize_changed_path(entry)
            if normalized not in normalized_scope:
                normalized_scope.append(normalized)
        forbidden = tuple(Path(item).resolve() for item in self.forbidden_roots)
        for blocked in forbidden:
            if resolved == blocked or blocked in resolved.parents:
                raise WorkspaceScopeError(
                    f"workspace {resolved} is inside a forbidden root {blocked}"
                )
        object.__setattr__(self, "root", resolved)
        object.__setattr__(self, "allowed_scope", tuple(normalized_scope))
        object.__setattr__(self, "forbidden_roots", forbidden)

    def covers(self, path: str) -> bool:
        normalized = normalize_changed_path(path)
        return any(_scope_covers(entry, normalized) for entry in self.allowed_scope)

    def as_dict(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "allowed_scope": list(self.allowed_scope),
            "forbidden_roots": [str(item) for item in self.forbidden_roots],
        }


def validate_changed_paths(
    paths: object, *, workspace: Workspace, max_paths: int
) -> tuple[str, ...]:
    """Validate the agent-reported changed paths against the workspace scope."""
    if not isinstance(paths, (list, tuple)):
        raise WorkspaceScopeError("changed_paths must be a list")
    if not paths:
        raise WorkspaceScopeError("a completed agent run must report changed paths")
    if len(paths) > max_paths:
        raise WorkspaceScopeError(
            f"agent reported {len(paths)} changed paths; limit is {max_paths}"
        )
    normalized: list[str] = []
    for raw in paths:
        path = normalize_changed_path(raw)  # non-str raises the same scope error
        if not workspace.covers(path):
            raise WorkspaceScopeError(
                f"changed path {path!r} is outside the allowed scope "
                f"{list(workspace.allowed_scope)}"
            )
        if path not in normalized:
            normalized.append(path)
    return tuple(normalized)


def patch_header_paths(patch: str) -> tuple[str, ...]:
    """Return the file paths named by unified-diff headers (``--- a/x`` / ``+++ b/x``)."""
    found: list[str] = []
    for line in patch.splitlines():
        for prefix in ("+++ b/", "--- a/"):
            if line.startswith(prefix):
                path = line[len(prefix) :].split("\t", 1)[0].strip()
                if path and path != "/dev/null" and path not in found:
                    found.append(path)
    return tuple(found)
