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

## `agents/`

A review team for the one class of mistake this repository must not make.
The shipped `reviewer` starts every `reviewer-*` in parallel on the same
immutable range and merges their reports; the coordinator owns the verdict.

- `reviewer-boundaries.md` (`deepseek/deepseek-v4-pro`, high) — plane
  boundaries, the Ictus pin, weakened gates, direct publication.
- `reviewer-contracts.md` (`deepseek/deepseek-flash`, high) — the cross-system
  contracts against the real pinned Ictus artifact and the fixtures that stand
  in for the other side.

Both are read-only and resolve the Ictus checkout through the worktree's
`agentic-control` link, reading the pinned revision out of it with
`git show <commit>:<file>` rather than trusting its working tree.

## `extensions/issues/`

The right panel's **Issues** tab, because this project's open work lives as
GitHub issues (`docs/ROADMAP.md` is only a snapshot). It lists `Walsamer/tactus`'s
open issues grouped by milestone — `type:/area:/priority:/fleet:` labels,
how many acceptance criteria are ticked, when each last moved — and hands one
to @ultimate as a single prepared prompt: the issue, its criteria and the
project's own hard rules, read from `AGENTS.md` at that moment. The action
follows the intake gate (`fleet:ready` only, as
`src/tactus/domain/work_source.py` says): **start** on an authorized issue,
**assess** on a gated one, where the prompt asks what has to change first. The
listing is read on the server with `gh issue list --repo … --state open --json …`
(a minute's cache and an explicit refresh), so no token reaches the browser.

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
