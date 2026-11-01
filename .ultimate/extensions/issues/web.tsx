/**
 * The Issues panel: the project's open GitHub issues, grouped by milestone, each with the labels that matter, its
 * acceptance criteria and when it last moved — and one click that hands it to @ultimate with the rules to work by.
 */
import { CheckIcon, CircleDotIcon, CopyIcon, ExternalLinkIcon, PlayIcon, RotateCwIcon } from 'lucide-react'
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import {
  actionRef,
  api,
  cn,
  defineWebExtension,
  docRef,
  errorMessage,
  formatAgo,
  useAction,
  useCopy,
  useDocOf,
  useNow,
  useUltimate,
} from '@/sdk'
import { store } from '@/sdk/client'

/** One open issue, as the `list` and `refresh` actions of `server.ts` return it. */
type Issue = {
  number: number
  title: string
  url: string
  milestone: string | null
  labels: string[]
  criteria: { text: string; done: boolean }[]
  done: number
  total: number
  updatedAt: string
}

/** The listing, or the setup step that keeps `gh` from reading it. */
type Issues =
  | { status: 'ok'; repo: string; fetchedAt: number; issues: Issue[] }
  | { status: 'setup'; reason: string; detail?: string }

/** No arguments → the open issues (`refresh` reads `gh` again); an issue number → the prompt that starts work on it. */
const ListAction = actionRef<null, Issues>('issues', 'list')
const RefreshAction = actionRef<null, Issues>('issues', 'refresh')
const PromptAction = actionRef<number, string>('issues', 'prompt')

/** The standing agents' index on the root conversation: `ultimate` names @ultimate's conversation. */
const StandingDoc = docRef<Record<string, number>>('ultimate.standing-agents')

/** The milestone's number, so M3 comes before M10; issues without one go last. */
const milestoneOrder = (title: string | null) => {
  if (title === null) return Number.MAX_SAFE_INTEGER
  const match = /\d+/.exec(title)
  return match ? Number(match[0]) : Number.MAX_SAFE_INTEGER - 1
}

/** The issues in their groups: by milestone, the newest work first inside one. */
function group(issues: readonly Issue[]) {
  const groups = new Map<string | null, Issue[]>()
  for (const issue of issues) {
    const key = issue.milestone
    groups.set(key, [...(groups.get(key) ?? []), issue])
  }
  return [...groups]
    .toSorted(([a], [b]) => milestoneOrder(a) - milestoneOrder(b))
    .map(([milestone, list]) => ({
      milestone,
      list: list.toSorted((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt)),
    }))
}

/** The tone of one label: priority and fleet state carry colour, what the work is stays quiet. */
function tone(label: string) {
  const at = label.indexOf(':')
  const [prefix, value] = at < 0 ? ['', label] : [label.slice(0, at), label.slice(at + 1)]
  if (prefix === 'priority')
    return value === 'p0'
      ? 'border-error/40 text-error'
      : value === 'p1'
        ? 'border-warning/40 text-warning'
        : 'border-border/80 text-muted-foreground'
  if (prefix === 'fleet')
    switch (value) {
      case 'ready':
        return 'border-success/40 text-success'
      case 'human':
        return 'border-brand/40 text-brand'
      case 'blocked':
        return 'border-border/80 bg-muted/50 text-muted-foreground/80'
      default:
        return 'border-border/80 text-muted-foreground'
    }
  return 'border-border/70 text-muted-foreground/80'
}

/** A label that matters, without its prefix: `type:`, `area:`, `priority:` and `fleet:` say what the rest is. */
function Label({ label }: { label: string }) {
  const at = label.indexOf(':')
  return (
    <span
      className={cn(
        'inline-flex h-[17px] items-center rounded-sm border px-1.5 font-mono text-[10px] whitespace-nowrap',
        tone(label),
      )}
      title={label}
    >
      {at < 0 ? label : label.slice(at + 1)}
    </span>
  )
}

/** How many of an issue's criteria are ticked: a thin bar, and the count. */
function Criteria({ issue }: { issue: Issue }) {
  if (issue.total === 0) return <span className="text-muted-foreground/50">no criteria</span>
  return (
    <span
      className="flex items-center gap-1.5"
      title={issue.criteria.map((item) => `${item.done ? '[x]' : '[ ]'} ${item.text}`).join('\n')}
    >
      <span className="relative h-1 w-7 shrink-0 overflow-hidden rounded-full bg-muted">
        <span
          className={cn(
            'absolute inset-y-0 left-0 rounded-full',
            issue.done === issue.total ? 'bg-success/70' : 'bg-foreground/45',
          )}
          style={{ width: `${Math.round((issue.done / issue.total) * 100)}%` }}
        />
      </span>
      <span className={cn('tabular-nums', issue.done === issue.total && 'text-success')}>
        {issue.done}/{issue.total}
      </span>
    </span>
  )
}

/** What the handing button shows: waiting while @ultimate is resolved, else start, busy, handed or copy. */
type HandState = 'waiting' | 'idle' | 'busy' | 'handed' | 'copy'

/** One issue: its number and title, the labels that matter, its criteria and when it last moved. */
function Row({
  issue,
  hand,
  state,
  now,
}: {
  issue: Issue
  hand: (issue: Issue) => void
  state: HandState
  now: number
}) {
  return (
    <div className="group/row border-b border-border/50 px-3 py-2 last:border-b-0" data-issue={issue.number}>
      <div className="flex min-w-0 items-start gap-2">
        <span className="mt-[3px] shrink-0 font-mono text-[10.5px] text-muted-foreground/60 tabular-nums">
          #{issue.number}
        </span>
        <span className="line-clamp-3 min-w-0 flex-1 text-[12.5px] leading-snug text-foreground/90">
          {issue.title}
        </span>
      </div>
      {issue.labels.length > 0 && (
        <div className="mt-1 flex flex-wrap items-center gap-1 pl-[1.65rem]">
          {issue.labels.map((label) => (
            <Label key={label} label={label} />
          ))}
        </div>
      )}
      <div className="mt-1 flex items-center gap-2 pl-[1.65rem] font-mono text-[10.5px] text-muted-foreground/70">
        <Criteria issue={issue} />
        <span className="truncate" title={new Date(issue.updatedAt).toLocaleString()}>
          {formatAgo(Date.parse(issue.updatedAt), now, { ago: true })}
        </span>
        <span className="ml-auto flex shrink-0 items-center gap-0.5">
          <Hand hand={hand} issue={issue} state={state} />
          <a
            className="flex size-5 items-center justify-center rounded-sm text-muted-foreground/60 transition-colors hover:bg-accent hover:text-foreground"
            href={issue.url}
            rel="noreferrer"
            target="_blank"
            title={`Open #${issue.number} on GitHub`}
          >
            <ExternalLinkIcon className="size-3" />
          </a>
        </span>
      </div>
    </div>
  )
}

/** The click that starts the work: hand the issue to @ultimate, or copy the prompt when there is no @ultimate. */
function Hand({
  issue,
  hand,
  state,
}: {
  issue: Issue
  hand: (issue: Issue) => void
  state: HandState
}) {
  const copying = state === 'copy'
  return (
    <Button
      className="h-5 gap-1 px-1.5 font-mono text-[10.5px] text-muted-foreground hover:text-foreground"
      disabled={state === 'busy' || state === 'waiting'}
      onClick={() => hand(issue)}
      size="sm"
      title={
        copying
          ? 'Copy the prompt that starts work on this issue'
          : 'Hand this issue to @ultimate with its criteria and the project rules'
      }
      variant="ghost"
    >
      {state === 'handed' ? (
        <CheckIcon className="size-3 text-success" />
      ) : copying ? (
        <CopyIcon className="size-3" />
      ) : (
        <PlayIcon className={cn('size-3', state === 'busy' && 'animate-pulse')} />
      )}
      {state === 'handed' ? 'handed' : copying ? 'copy' : 'start'}
    </Button>
  )
}

/** A quiet line the panel shows instead of issues, with the command that fixes it. */
function Notice({
  text,
  detail,
  tone = 'muted',
}: {
  text: string
  detail?: string
  tone?: 'muted' | 'error'
}) {
  const { copied, copy } = useCopy()
  return (
    <div className="px-3 py-6">
      <div
        className={cn(
          'font-mono text-[11.5px]',
          tone === 'error' ? 'text-error' : 'text-muted-foreground',
        )}
      >
        {text}
      </div>
      {detail && (
        <button
          className="mt-1.5 flex items-center gap-1 font-mono text-[10.5px] text-muted-foreground/70 transition-colors hover:text-foreground"
          onClick={() => copy(detail)}
          title="Copy"
          type="button"
        >
          {detail}
          {copied ? <CheckIcon className="size-3 text-success" /> : <CopyIcon className="size-3" />}
        </button>
      )}
    </div>
  )
}

function IssuesPanel() {
  const listing = useAction(ListAction)
  const { conversationId, switchConversation } = useUltimate()
  // `undefined` while the index loads, `null` when it does not name @ultimate: then there is only the prompt to copy.
  const standing = useDocOf(1, StandingDoc)
  const ultimate = standing === undefined ? undefined : (standing?.ultimate ?? null)
  const { copy } = useCopy()
  const [busy, setBusy] = useState<number | null>(null)
  const [handed, setHanded] = useState<number | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const now = useNow(60_000)

  const refresh = async () => {
    setRefreshing(true)
    try {
      await api.action(RefreshAction)
      await listing.refetch()
    } catch (error) {
      store.notify({ level: 'error', message: errorMessage(error) })
    } finally {
      setRefreshing(false)
    }
  }

  /** The prompt of `issue`, from the server, which reads the project's rules for it. */
  const hand = async (issue: Issue) => {
    if (ultimate === undefined) return
    setBusy(issue.number)
    try {
      const text = await api.action(PromptAction, issue.number)
      if (ultimate === null) {
        copy(text)
        return
      }
      await api.submit(text, 'followUp', ultimate)
      setHanded(issue.number)
      if (conversationId !== ultimate) switchConversation(ultimate)
    } catch (error) {
      store.notify({ level: 'error', message: errorMessage(error) })
    } finally {
      setBusy(null)
    }
  }

  const state = (issue: Issue): HandState => {
    if (busy === issue.number) return 'busy'
    if (ultimate === undefined) return 'waiting'
    if (ultimate === null) return 'copy'
    return handed === issue.number ? 'handed' : 'idle'
  }

  if (listing.error)
    return <Notice text={listing.error.message} tone="error" />
  if (!listing.data)
    return <Notice text="reading the issues…" />
  if (listing.data.status === 'setup')
    return <Notice detail={listing.data.detail} text={listing.data.reason} />

  const { issues } = listing.data
  const done = issues.reduce((sum, issue) => sum + issue.done, 0)
  const total = issues.reduce((sum, issue) => sum + issue.total, 0)
  const groups = group(issues)
  return (
    <div className="pb-2">
      <div className="flex h-8 items-center gap-2 px-3">
        <span className="section-label">{listing.data.repo}</span>
        <span className="ml-auto font-mono text-[10.5px] text-muted-foreground/70 tabular-nums">
          {issues.length} open · {done}/{total}
        </span>
        <Button
          className="h-5 w-5 shrink-0 p-0 text-muted-foreground/70 hover:text-foreground"
          disabled={refreshing}
          onClick={() => void refresh()}
          size="sm"
          title={`Read the issues again · ${formatAgo(listing.data.fetchedAt, now, { ago: true })}`}
          variant="ghost"
        >
          <RotateCwIcon className={cn('size-3', refreshing && 'animate-spin')} />
        </Button>
      </div>
      {groups.map(({ milestone, list }) => {
        const ticked = list.reduce((sum, issue) => sum + issue.done, 0)
        const all = list.reduce((sum, issue) => sum + issue.total, 0)
        return (
          <section key={milestone ?? 'none'}>
            <div className="flex items-baseline gap-2 px-3 pt-2.5 pb-1">
              <span className="truncate text-[11.5px] text-foreground/80">
                {milestone ?? 'No milestone'}
              </span>
              <span className="ml-auto shrink-0 font-mono text-[10.5px] text-muted-foreground/60 tabular-nums">
                {list.length} · {ticked}/{all}
              </span>
            </div>
            {list.map((issue) => (
              <Row hand={hand} issue={issue} key={issue.number} now={now} state={state(issue)} />
            ))}
          </section>
        )
      })}
      {issues.length === 0 && <Notice text="no open issues" />}
    </div>
  )
}

export default defineWebExtension({
  panels: [{ id: 'issues', title: 'Issues', icon: CircleDotIcon, component: IssuesPanel, order: 3 }],
  commands: [
    {
      id: 'issues.open',
      title: 'Show the open GitHub issues',
      run: (context) => context.openPanel('issues'),
    },
  ],
})
