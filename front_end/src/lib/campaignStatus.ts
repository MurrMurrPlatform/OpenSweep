// Presentation helpers for Campaign statuses and part states — the campaign
// counterpart of lib/runStatus.ts, but already speaking shadcn Badge tones.

import type { BadgeVariants } from '@/components/ui/badge'
import { isLiveRunStatus, runStatusVariant, toneToBadgeVariant } from '@/lib/runStatus'
import type {
  CampaignDTO,
  CampaignPart,
  CampaignPartState,
  CampaignStatus,
  CampaignTemplate,
} from '@/types/api'

/** Parts a new campaign dispatches at once. Mirrors the backend's
 *  campaigns.models.DEFAULT_MAX_PARALLEL — the effective number is the
 *  tighter of this and the provider's max_concurrent_runs. */
export const DEFAULT_MAX_PARALLEL = 5

/** Statuses that keep mutating server-side — poll while one of these holds. */
export const LIVE_CAMPAIGN_STATUSES: CampaignStatus[] = ['running', 'finalizing']

export function isLiveCampaignStatus(status: CampaignStatus): boolean {
  return LIVE_CAMPAIGN_STATUSES.includes(status)
}

export function campaignStatusVariant(status: CampaignStatus): BadgeVariants['variant'] {
  if (status === 'running' || status === 'finalizing') return 'info'
  if (status === 'done') return 'success'
  if (status === 'failed' || status === 'cancelled') return 'destructive'
  return 'secondary' // planning
}

export function partStateVariant(state: CampaignPartState): BadgeVariants['variant'] {
  if (state === 'running') return 'info'
  if (state === 'done') return 'success'
  if (state === 'failed') return 'destructive'
  return 'secondary' // pending
}

// ── Part badges: the run's status when we have it, the planner's otherwise ──
//
// `part.state` is the planner's four-value, forward-only, once-a-minute
// snapshot. It is the wrong thing to show a human: `awaiting_input` reads as
// `done`, a replied-to run stays `done`, and a cancelled campaign's parts
// stay `running` forever. Detail reads carry `run_status` — prefer it.

/** Runs that are neither terminal nor waiting on the user. */
const RUN_STATUS_LABELS: Partial<Record<NonNullable<CampaignPart['run_status']>, string>> = {
  awaiting_input: 'awaiting input',
  paused_quota: 'paused (quota)',
  limit_exceeded: 'limit exceeded',
}

export function partStatusLabel(part: CampaignPart): string {
  const status = part.run_status
  if (!status) return part.state
  return RUN_STATUS_LABELS[status] ?? status
}

export function partStatusVariant(part: CampaignPart): BadgeVariants['variant'] {
  const status = part.run_status
  // No `run` to check needs_input against, so awaiting_input tones as
  // success (turn finished) — same default runStatusVariant uses.
  if (!status) return partStateVariant(part.state)
  return toneToBadgeVariant(runStatusVariant(status))
}

/** True while this part's RUN is still producing output. Drives detail-view
 *  polling: the campaign's own status goes terminal on cancel while its runs
 *  keep going, so polling on campaign status alone freezes the view. */
export function isPartRunLive(part: CampaignPart): boolean {
  return part.run_status ? isLiveRunStatus(part.run_status) : false
}

export const CAMPAIGN_TEMPLATE_LABELS: Record<CampaignTemplate, string> = {
  full: 'Full',
  rotation: 'Rotation',
  focused: 'Focused',
}

/** Finished (done + failed) vs total parts — drives the "3/12 parts" cells. */
export function campaignProgress(c: CampaignDTO): { finished: number; total: number } {
  const parts = c.parts ?? []
  return {
    finished: parts.filter((p) => p.state === 'done' || p.state === 'failed').length,
    total: parts.length,
  }
}
