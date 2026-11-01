#!/bin/sh
# Prepare a fresh git worktree so the repository's own gate runs in it.
#
# Run by Ultimate before the first tool call of every coordinator, isolated
# subagent and the lander, with ULTIMATE_SOURCE set to the source checkout.
# Must be safe to repeat.
set -e

# Dependencies: the lockfile is authoritative, the cache keeps this quick.
uv sync --quiet

# tests/integrations/ictus/ resolves the pinned Ictus checkout from a sibling
# directory named agentic-control or ictus under an ancestor of the working
# directory (integrations/ictus/conformance.py resolve_ictus_root). A worktree
# lives far from any such sibling, so link the one beside the source checkout
# and keep the link out of `git status` through the clone's local exclude file.
ictus="$(dirname "$ULTIMATE_SOURCE")/agentic-control"
if [ -d "$ictus" ] && [ ! -e agentic-control ]; then
  ln -s "$ictus" agentic-control
  exclude="$(git rev-parse --git-path info/exclude)"
  grep -qx '/agentic-control' "$exclude" 2>/dev/null || printf '/agentic-control\n' >>"$exclude"
fi
