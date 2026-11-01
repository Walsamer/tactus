The open GitHub issues of `Walsamer/tactus` in the right panel's Issues tab: what each one is, how far its acceptance criteria went, and one click that hands it to @ultimate with the rules to work by.

This project's open work lives as issues on GitHub; `docs/ROADMAP.md` is only a snapshot. The panel reads them on the server with `gh issue list --repo Walsamer/tactus --state open --json number,title,labels,milestone,body,updatedAt,url` (`server.ts`), so no token reaches the browser:

- **Grouped by milestone** (M0 first, no milestone last), the newest work first inside a group. Each issue shows its number and title, the labels that matter (`priority:*`, `type:*`, `area:*`, `fleet:*`, with `fleet:ready`, `fleet:blocked` and `fleet:human` toned apart), how many of the `## Acceptance criteria` checklist are ticked, and when it last moved; the arrow opens it on GitHub. The header counts the open issues and the ticked criteria overall and per milestone.
- **start** asks the `prompt` action for a prompt naming the issue, its URL, milestone, labels, every acceptance criterion and the project's own hard rules (read from `AGENTS.md` at call time), then submits it to @ultimate's conversation with `api.submit(text, 'followUp', id)` and shows that conversation, so you watch it land. @ultimate's id comes from the `ultimate.standing-agents` document on the root conversation. When that index does not name @ultimate, the button copies the prompt instead.
- **refresh** (the arrow in the header) reads `gh` again; otherwise a listing is cached for a minute, and a setup failure is never cached.

When `gh` is missing, logged out, or cannot see the repository, the panel says which and offers the command to copy.

Actions: `list` (no arguments → `{ status: 'ok', repo, fetchedAt, issues }` or `{ status: 'setup', reason, detail }`), `refresh` (same, read again), `prompt` (an issue number → the prompt string).
