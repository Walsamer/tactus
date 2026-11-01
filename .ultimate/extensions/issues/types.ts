/**
 * What the panel shows and the actions return: shared by `server.ts` and `web.tsx`, so a renamed field cannot drift
 * between the two facets. Types only: nothing is imported at runtime.
 */

/** One open issue, as the `list` and `refresh` actions return it. */
export type Issue = {
  number: number
  title: string
  url: string
  /** The milestone's title, or null when the issue has none. */
  milestone: string | null
  /** The labels that say what the work is: `type:*`, `area:*`, `priority:*`, `fleet:*`, in that order. */
  labels: string[]
  /** The `## Acceptance criteria` checklist of the body, as parsed; empty when the body has none. */
  criteria: { text: string; done: boolean }[]
  /** How many criteria are ticked, and how many there are. */
  done: number
  total: number
  updatedAt: string
}

/** What the panel renders: the issues, or the setup step that stands in their way. */
export type Issues =
  | { status: 'ok'; repo: string; fetchedAt: number; issues: Issue[] }
  | { status: 'setup'; reason: string; detail?: string }
