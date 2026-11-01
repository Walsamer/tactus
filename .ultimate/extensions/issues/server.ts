/**
 * The open GitHub issues of this repository, read with `gh` on the server, and the prompt that hands one to @ultimate.
 * The panel (`web.tsx`) shows what these actions return; no token ever leaves this process.
 */
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import type { JsonValue } from '@earendil-works/chord'
import { defineExtension } from '@earendil-works/pi-durable'

export default defineExtension({ name: 'issues' })

/** The repository whose issues are this project's open work. */
const REPO = 'Walsamer/tactus'

/** Facts a listing page shows, per issue. */
export type Issue = {
  number: number
  title: string
  url: string
  /** The milestone's title, or null when the issue has none. */
  milestone: string | null
  /** The labels that say what the work is: `priority:*`, `type:*`, `area:*`, `fleet:*`, in that order. */
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

/** The slice of the core's `ActionContext` these actions use: `exec` and the project root. */
type Context = {
  exec(
    command: string,
    args?: string[],
    options?: { cwd?: string; timeout?: number },
  ): Promise<{ stdout: string; stderr: string }>
  host: { projectRoot: string }
}

/** Labelled `- [ ]` / `- [x]` items of the body's `## Acceptance criteria` section, until the next `##` heading. */
function criteriaOf(body: string): Issue['criteria'] {
  const lines = body.split('\n')
  const start = lines.findIndex((line) => /^##\s+acceptance criteria\s*$/i.test(line.trim()))
  if (start < 0) return []
  const criteria: Issue['criteria'] = []
  for (const line of lines.slice(start + 1)) {
    if (/^#{1,2}\s/.test(line.trim())) break
    const item = /^\s*[-*]\s*\[([ xX])\]\s+(.*\S)\s*$/.exec(line)
    if (item) criteria.push({ text: item[2], done: item[1].toLowerCase() === 'x' })
  }
  return criteria
}

/** The order labels read in: what it is, where, how urgent, whether it may be picked up. */
const PREFIXES = ['type', 'area', 'priority', 'fleet']

/** The labels that matter from a list, kept in `PREFIXES` order and otherwise as they came. */
function labelsOf(names: readonly string[]): string[] {
  const kept = names.filter((name) => PREFIXES.some((prefix) => name.startsWith(`${prefix}:`)))
  return kept.toSorted(
    (a, b) => PREFIXES.indexOf(a.slice(0, a.indexOf(':'))) - PREFIXES.indexOf(b.slice(0, b.indexOf(':'))),
  )
}

/** One `gh issue list` entry, narrowed to what the panel shows. */
type GhIssue = {
  number: number
  title: string
  url: string
  body: string
  updatedAt: string
  milestone: { title: string } | null
  labels: { name: string }[]
}

/** The issues as `gh` answered, newest work first. */
async function fetchIssues(ctx: Context): Promise<Issues> {
  let stdout: string
  try {
    ;({ stdout } = await ctx.exec(
      'gh',
      [
        'issue',
        'list',
        '--repo',
        REPO,
        '--state',
        'open',
        '--limit',
        '100',
        '--json',
        'number,title,labels,milestone,body,updatedAt,url',
      ],
      { timeout: 20_000 },
    ))
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    if (/ENOENT|not found/i.test(message))
      return { status: 'setup', reason: 'gh is not installed', detail: 'brew install gh' }
    if (/auth|authenticate|login/i.test(message))
      return { status: 'setup', reason: 'gh is not logged in', detail: 'gh auth login' }
    if (/Could not resolve to a Repository/i.test(message))
      return {
        status: 'setup',
        reason: `${REPO} is not reachable with this gh login`,
        detail: 'gh auth status',
      }
    throw error
  }
  const issues = (JSON.parse(stdout) as GhIssue[]).map((issue) => {
    const criteria = criteriaOf(issue.body ?? '')
    return {
      number: issue.number,
      title: issue.title,
      url: issue.url,
      milestone: issue.milestone?.title ?? null,
      labels: labelsOf((issue.labels ?? []).map((label) => label.name)),
      criteria,
      done: criteria.filter((item) => item.done).length,
      total: criteria.length,
      updatedAt: issue.updatedAt,
    }
  })
  return { status: 'ok', repo: REPO, fetchedAt: Date.now(), issues }
}

/** A minute: short enough that a just-closed issue disappears, long enough that a panel remount costs no call. */
const CACHE_MS = 60_000

let cached: { at: number; value: Issues } | undefined

/** The listing, from the cache while it is fresh; `force` reads `gh` again. */
async function listing(ctx: Context, force: boolean): Promise<Issues> {
  if (!force && cached && Date.now() - cached.at < CACHE_MS) return cached.value
  const value = await fetchIssues(ctx)
  // A setup problem is not cached: the user fixing `gh` should not wait for the cache to expire.
  cached = value.status === 'ok' ? { at: Date.now(), value } : undefined
  return value
}

/** The project's own hard rules, as `AGENTS.md` lists them: what a prompt that starts work has to say. */
async function rules(projectRoot: string): Promise<string[]> {
  let text: string
  try {
    text = await readFile(join(projectRoot, 'AGENTS.md'), 'utf8')
  } catch {
    return []
  }
  const lines = text.split('\n')
  const start = lines.findIndex((line) => /^##\s+hard rules\s*$/i.test(line.trim()))
  if (start < 0) return []
  const rules: string[] = []
  for (const line of lines.slice(start + 1)) {
    if (/^##\s/.test(line)) break
    if (/^[-*]\s+/.test(line)) rules.push(line.replace(/^[-*]\s+/, '').trim())
    else if (rules.length && line.trim()) rules[rules.length - 1] += ` ${line.trim()}`
  }
  return rules
}

/** The prompt that starts work on `issue`: what it is, what done means, and the rules it is done under. */
function promptFor(issue: Issue, rules: readonly string[]): string {
  const ticks =
    issue.total === 0
      ? ['(this issue states no acceptance criteria; read the issue and say what done means)']
      : issue.criteria.map((item) => `- [${item.done ? 'x' : ' '}] ${item.text}`)
  return [
    `Work on ${REPO} issue #${issue.number} — ${issue.title}.`,
    '',
    issue.url,
    `Milestone: ${issue.milestone ?? 'none'}`,
    `Labels: ${issue.labels.join(', ') || 'none'}`,
    '',
    `## Acceptance criteria (${issue.done} of ${issue.total} ticked)`,
    ...ticks,
    '',
    '## Project rules (AGENTS.md)',
    ...(rules.length ? rules.map((rule) => `- ${rule}`) : ['- see AGENTS.md in the project']),
    '',
    'Read the issue first, then plan and do the work under these rules.',
  ].join('\n')
}

export const actions = {
  /** No arguments → the open issues, grouped by the panel; a setup state when `gh` cannot read them. */
  async list(_args: JsonValue, ctx: Context): Promise<Issues> {
    return listing(ctx, false)
  },
  /** No arguments → the open issues, read from `gh` again rather than the cache. */
  async refresh(_args: JsonValue, ctx: Context): Promise<Issues> {
    return listing(ctx, true)
  },
  /** An issue number → the prompt that starts work on it, naming its criteria and the project's rules. */
  async prompt(args: JsonValue, ctx: Context): Promise<string> {
    const number = typeof args === 'object' && args !== null ? (args as { number?: unknown }).number : args
    if (!Number.isSafeInteger(number)) throw new Error('Expected an issue number')
    const value = await listing(ctx, false)
    if (value.status !== 'ok') throw new Error(value.reason)
    const issue = value.issues.find((item) => item.number === number)
    if (!issue) throw new Error(`Issue #${String(number)} is not open`)
    return promptFor(issue, await rules(ctx.host.projectRoot))
  },
}
