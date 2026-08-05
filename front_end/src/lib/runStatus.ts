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

// ── Elapsed / duration — a 40-minute run should not look identical to a
//    40-second one anywhere it's listed. ────────────────────────────────────

type RunTimingLike = Pick<
  RunDTO,
  'status' | 'started_at' | 'created_at' | 'last_activity_at' | 'updated_at' | 'duration_ms'
>

/** "45s" / "3m 12s" / "1h 04m". */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return '—'
  const totalSeconds = Math.floor(ms / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, '0')}m`
  if (minutes > 0) return `${minutes}m ${String(seconds).padStart(2, '0')}s`
  return `${seconds}s`
}

/** Elapsed time for a run row: ticks live (from started_at, falling back to
 *  created_at while still queued) for anything still in flight; the recorded
 *  duration — or a best-effort span to its last known activity — otherwise. */
export function runElapsedMs(run: RunTimingLike, nowMs: number): number {
  if (run.duration_ms) return run.duration_ms
  const startIso = run.started_at || run.created_at
  if (!startIso) return 0
  const start = new Date(startIso).getTime()
  if (Number.isNaN(start)) return 0
  if (isLiveRunStatus(run.status)) return Math.max(0, nowMs - start)
  const endIso = run.last_activity_at || run.updated_at
  const end = endIso ? new Date(endIso).getTime() : nowMs
  return Math.max(0, (Number.isNaN(end) ? nowMs : end) - start)
}

/** Coarse phase, limited to what the backend actually signals. There is no
 *  distinct "finalizing" stage in the data today — output parsing happens
 *  synchronously at turn-end, not as an observable live phase — so it isn't
 *  represented here rather than guessed at. */
export type RunPhase = 'queued' | 'starting' | 'running' | 'paused' | 'done'

export function runPhase(run: Pick<RunDTO, 'status' | 'turns'>): RunPhase {
  if (run.status === 'queued') return 'queued'
  if (run.status === 'paused_quota') return 'paused'
  if (run.status === 'running') return run.turns > 0 ? 'running' : 'starting'
  return 'done'
}

export function runPhaseLabel(phase: RunPhase): string {
  if (phase === 'starting') return 'starting — cloning workspace'
  return phase
}

/** 1-based position within the other runs still ahead of it in the same
 *  queue (older created_at first) — null once the run has left `queued`. */
export function queuePosition(
  run: Pick<RunDTO, 'uid' | 'status' | 'created_at'>,
  allQueued: Pick<RunDTO, 'uid' | 'status' | 'created_at'>[],
): number | null {
  if (run.status !== 'queued') return null
  const queued = allQueued
    .filter((r) => r.status === 'queued')
    .sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''))
  const idx = queued.findIndex((r) => r.uid === run.uid)
  return idx === -1 ? null : idx + 1
}
