// Presentation helpers for Run statuses — notably the quota-paused state,
// which carries retry info in usage.quota.

import type { BadgeVariants } from '@/components/ui/badge'
import type { RunDTO, RunQuotaUsage, RunStatus } from '@/types/api'

type RunLike = Pick<RunDTO, 'status' | 'usage'>

/** Statuses that keep producing output — poll while one of these holds. */
export const LIVE_RUN_STATUSES: RunStatus[] = ['queued', 'running', 'paused_quota']

/** Statuses from which a follow-up message is accepted (V3 §2) — replying to
 *  a failed run is the recovery loop; replying to an ended run reopens it. */
export const FOLLOW_UP_STATUSES: RunStatus[] = [
  'awaiting_input',
  'ended',
  'failed',
  'cancelled',
  'limit_exceeded',
]

export function isLiveRunStatus(status: RunStatus): boolean {
  return LIVE_RUN_STATUSES.includes(status)
}

export function acceptsFollowUp(status: RunStatus): boolean {
  return FOLLOW_UP_STATUSES.includes(status)
}

/** ask_user set usage.needs_input: the run is genuinely waiting on a human
 *  answer (cleared when the next follow-up message starts a turn). */
export function runNeedsInput(run: RunLike): boolean {
  const usage = run.usage
  if (!usage || typeof usage !== 'object') return false
  return (usage as Record<string, unknown>).needs_input === true
}

export function runQuota(run: RunLike): RunQuotaUsage | null {
  const usage = run.usage
  if (!usage || typeof usage !== 'object') return null
  const quota = (usage as Record<string, unknown>).quota
  if (!quota || typeof quota !== 'object' || Array.isArray(quota)) return null
  return quota as RunQuotaUsage
}

function formatEta(iso: string): string {
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return iso
  const deltaMin = Math.round((t - Date.now()) / 60_000)
  if (deltaMin > 0 && deltaMin < 120) return `in ~${deltaMin}m`
  return `~${new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
}

/** "Paused (quota) — retry 2 in ~14m" for paused_quota runs, a readable label otherwise. */
export function runStatusLabel(run: RunLike): string {
  // awaiting_input is the everyday "turn finished" state (there is no
  // terminal completed status) — only an open agent question means the run
  // is actually waiting on the user.
  if (run.status === 'awaiting_input') return runNeedsInput(run) ? 'needs your input' : 'done'
  if (run.status !== 'paused_quota') return run.status
  const quota = runQuota(run)
  const retry = Number(quota?.retry_count ?? 0) + 1
  const eta = typeof quota?.next_retry_at === 'string' && quota.next_retry_at
    ? ` ${formatEta(quota.next_retry_at)}`
    : ''
  return `Paused (quota) — retry ${retry}${eta}`
}

/** Badge variant per status: awaiting_input green (warn when an agent
 *  question is open), ended neutral, queued/running live, failures red,
 *  paused_quota purple-ish warn. */
export function runStatusVariant(
  status: RunStatus,
  run?: RunLike,
): 'success' | 'danger' | 'warn' | 'info' | 'default' {
  if (status === 'awaiting_input') return run && runNeedsInput(run) ? 'warn' : 'success'
  if (status === 'running' || status === 'queued') return 'info'
  if (status === 'failed' || status === 'cancelled' || status === 'limit_exceeded') return 'danger'
  if (status === 'paused_quota') return 'warn'
  return 'default' // ended
}

/** runStatusVariant's tones predate the shadcn Badge set (danger/default
 *  are gone). Views were each carrying their own copy of this mapping. */
export function toneToBadgeVariant(
  tone: ReturnType<typeof runStatusVariant>,
): BadgeVariants['variant'] {
  if (tone === 'danger') return 'destructive'
  if (tone === 'default') return 'secondary'
  return tone
}
