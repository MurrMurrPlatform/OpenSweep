// Display metadata for findings, shared by the list, detail and ledger views.

import type { BadgeVariants } from '@/components/ui/badge'
import type { FindingDTO, FindingStatus, Severity } from '@/types/api'

export const SEVERITY_RANK: Record<string, number> = { low: 0, medium: 1, high: 2, critical: 3 }

export function severityVariant(sev: Severity): BadgeVariants['variant'] {
  if (sev === 'critical' || sev === 'high') return 'destructive'
  if (sev === 'medium') return 'warn'
  return 'secondary'
}

// ── Status ──────────────────────────────────────────────────────────────────
//
// Anything that is not `open` counts as PROCESSED — already dealt with, and
// hidden from the board's default view. The board labels the status only when
// you are looking at processed findings; in the open view every row would
// carry the same chip, which is noise.

const STATUS_META: Record<FindingStatus, { label: string; variant: BadgeVariants['variant'] }> = {
  open: { label: 'Open', variant: 'warn' },
  ticketed: { label: 'Ticketed', variant: 'info' },
  acknowledged: { label: 'Acknowledged', variant: 'info' },
  'wont-fix': { label: "Won't fix", variant: 'secondary' },
  fixed: { label: 'Fixed', variant: 'success' },
  accepted: { label: 'Accepted', variant: 'success' },
  superseded: { label: 'Superseded', variant: 'secondary' },
  dismissed: { label: 'Dismissed', variant: 'secondary' },
}

export function statusLabel(status: FindingStatus): string {
  return STATUS_META[status]?.label ?? status
}

export function statusVariant(status: FindingStatus): BadgeVariants['variant'] {
  return STATUS_META[status]?.variant ?? 'secondary'
}

// ── Trust ───────────────────────────────────────────────────────────────────
//
// `trust` is a composite 0..1 credibility the API derives from signals it can
// check independently of what the model asserted (see back_end
// domains/findings/services/trust.py). A bare float means nothing to a human,
// so everything below exists to name the score and, more importantly, to
// enumerate the evidence behind it.
//
// Thresholds follow the server's arithmetic: an uncorroborated agent finding
// sits at its raw confidence (0.7 by default), so clearing 0.8 means something
// external agreed — a second run, or a static analyzer.

export const TRUST_HIGH = 0.8
export const TRUST_MEDIUM = 0.6

export interface TrustTier {
  key: 'high' | 'medium' | 'low'
  label: string
  variant: BadgeVariants['variant']
}

export function trustTier(trust: number): TrustTier {
  if (trust >= TRUST_HIGH) return { key: 'high', label: 'High trust', variant: 'success' }
  if (trust >= TRUST_MEDIUM) return { key: 'medium', label: 'Medium trust', variant: 'secondary' }
  return { key: 'low', label: 'Low trust', variant: 'warn' }
}

export function trustPercent(trust: number): number {
  return Math.round((Number.isFinite(trust) ? trust : 0) * 100)
}

/** Distinct runs that filed or re-confirmed this finding. Mirrors the server's
 *  `corroboration_count`: `source_run_uids` is lazily backfilled from the
 *  singular `source_run_uid`, so a finding seen once may carry an empty list —
 *  that is still one sighting, not zero. */
export function corroborationCount(f: Pick<FindingDTO, 'source_run_uids' | 'source_run_uid'>): number {
  const uids = new Set((f.source_run_uids || []).filter(Boolean))
  if (f.source_run_uid) uids.add(f.source_run_uid)
  return Math.max(uids.size, 1)
}

export type TrustSignalKey = 'corroboration' | 'tool' | 'confidence' | 'verification'

export interface TrustSignal {
  key: TrustSignalKey
  /** Terse chip text for dense rows ("3 runs agree"). */
  label: string
  /** One sentence explaining what the signal means, for tooltips/breakdowns. */
  detail: string
  /** Whether this signal argues for the finding, against it, or is neutral. */
  tone: 'positive' | 'neutral' | 'negative'
}

/**
 * Outcome of the skeptic pass, when the caller has loaded verification runs.
 * The list view has none of this; the finding detail view does.
 *
 * The keys mirror the verdict vocabulary a verification run emits
 * (`resolved-properly` / `partially-resolved` / `not-resolved` /
 * `cannot-determine`), read here for what it says about whether the finding is
 * REAL — a second agent that re-read the code and still saw the problem is the
 * strongest independent evidence available.
 */
export type VerificationOutcome =
  | 'none'
  | 'running'
  | 'still-present'
  | 'partly-present'
  | 'resolved'
  | 'inconclusive'

const VERIFICATION_COPY: Record<
  VerificationOutcome,
  { label: string; detail: string; tone: TrustSignal['tone'] } | null
> = {
  'still-present': {
    label: 'Re-checked, still present',
    detail: 'A verification run re-read the affected code and found the problem still there — independent confirmation it is real.',
    tone: 'positive',
  },
  resolved: {
    label: 'Verified fixed',
    detail: 'A verification run confirmed the problem was real and is now resolved in the code.',
    tone: 'positive',
  },
  'partly-present': {
    label: 'Partly resolved',
    detail: 'A verification run found part of this still present and part resolved.',
    tone: 'neutral',
  },
  inconclusive: {
    label: 'Verdict inconclusive',
    detail: 'A verification run finished without reaching a verdict either way.',
    tone: 'neutral',
  },
  running: {
    label: 'Verification running',
    detail: 'A verification run is in flight — its verdict lands here when it finishes.',
    tone: 'neutral',
  },
  none: null,
}

/**
 * The evidence behind a finding's trust score, strongest signal first.
 *
 * Cross-run corroboration leads: agreement between separate runs (different
 * context windows, often different models) is the cheapest false-positive
 * discriminator the platform has. A static-analyzer match ranks next because
 * it is deterministic rather than asserted. The executor's own confidence is
 * always shown, but last — it is the model grading its own homework.
 */
export function trustSignals(
  f: FindingDTO,
  verification: VerificationOutcome = 'none',
): TrustSignal[] {
  const signals: TrustSignal[] = []

  const runs = corroborationCount(f)
  signals.push(
    runs > 1
      ? {
          key: 'corroboration',
          label: `${runs} runs agree`,
          detail: `Filed or re-confirmed by ${runs} independent runs — separate passes reached the same conclusion.`,
          tone: 'positive',
        }
      : {
          key: 'corroboration',
          label: 'Seen once',
          detail: 'Only one run has ever seen this. No cross-run corroboration yet.',
          tone: 'neutral',
        },
  )

  const tool = (f.detected_by_tool || '').trim()
  const rule = (f.detected_by_rule || '').trim()
  if (tool) {
    signals.push({
      key: 'tool',
      label: rule ? `${tool} · ${rule}` : tool,
      detail: `Confirmed by ${tool}${rule ? ` (rule ${rule})` : ''} — a deterministic analyzer matched, not just a model assertion.`,
      tone: 'positive',
    })
  } else {
    signals.push({
      key: 'tool',
      label: 'Agent-reported',
      detail: 'No static analyzer matched this; it rests on the agent’s own analysis of the code.',
      tone: 'neutral',
    })
  }

  const copy = VERIFICATION_COPY[verification]
  if (copy) signals.push({ key: 'verification', ...copy })

  signals.push({
    key: 'confidence',
    label: `${trustPercent(f.confidence)}% self-reported`,
    detail: `The executor's own confidence that this finding is real: ${trustPercent(f.confidence)}%.`,
    tone: 'neutral',
  })

  return signals
}
