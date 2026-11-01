# `.ultimate/` — project setup for Ultimate

Committed with the repository, so everyone who starts `ultimate` in this folder
gets the same setup. It applies only after Ultimate's **Use this project's
setup?** choice is answered with *Use it*.

## `settings.json`

- `isolates.worktree.setup` — every coordinator, isolated subagent and the
  lander works in a fresh `git worktree`, which git creates without the
  ignored files the source checkout has. `worktree-setup.sh` gives it
  dependencies and the Ictus checkout the conformance tests need, before the
  first tool call.
- `lander.checks` — the registered verifier `./scripts/verify-goal.sh` runs on
  every landing. It is the same gate CI runs: whitespace, required docs, agent
  consistency (Ictus pin), secret material, then `uv run pytest`.
  Nothing is pushed: publication stays a pull request, per `AGENTS.md`.

## `worktree-setup.sh`

`uv sync` for dependencies, then a symlink to the Ictus checkout beside the
source checkout (`../agentic-control`), because
`integrations/ictus/conformance.py` resolves the pinned checkout from a sibling
directory named `agentic-control`/`ictus` under an ancestor of the working
directory — and a worktree under `~/.ultimate/worktrees` has none. The link is
kept out of `git status` through the clone's `.git/info/exclude`
(`/agentic-control`).

Verified in a scratch worktree: setup a few seconds, then `verify-goal.sh`
passing with 853 tests and a clean status.
