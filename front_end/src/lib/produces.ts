import type { ProducesKind, RunPlaybook } from '@/types/api'

/** The produces values a user can pick when creating/editing an agent. */
export const PRODUCES_OPTIONS: { value: ProducesKind; label: string; description: string }[] = [
  {
    value: 'findings',
    label: 'Findings',
    description: 'Investigate and file evidenced Findings (audits, sweeps, checks).',
  },
  {
    value: 'answer',
    label: 'Answer',
    description: 'A conversational answer — reports, summaries, questions.',
  },
  {
    value: 'documentation',
    label: 'Documentation',
    description: 'Keep the Docs wiki and Memories true and current.',
  },
]

/** Labels for every produces value, including system-only ones. */
export const PRODUCES_LABELS: Record<ProducesKind, string> = {
  findings: 'Findings',
  answer: 'Answer',
  documentation: 'Documentation',
  'doc-tree': 'Doc tree',
  analysis: 'Analysis',
  'review-verdict': 'Review verdict',
  verification: 'Verification',
  'code-changes': 'Code changes',
}

/**
 * Display-only mapping from a run's internal playbook to the produces kind
 * it best matches. `playbook` is internal machinery — this is the ONLY file
 * that translates it for the UI.
 */
export const PLAYBOOK_TO_PRODUCES: Record<RunPlaybook, ProducesKind> = {
  chat: 'answer',
  ask: 'findings',
  review: 'review-verdict',
  fix: 'code-changes',
  implement: 'code-changes',
  verify: 'verification',
  document: 'documentation',
  refine: 'findings',
}

export function producesLabel(kind: string): string {
  return PRODUCES_LABELS[kind as ProducesKind] ?? kind
}

/** Badge variant per produces kind (UI_CONVENTIONS badge variants). */
export function producesBadgeVariant(
  kind: string,
): 'default' | 'secondary' | 'outline' | 'success' | 'warn' | 'info' {
  switch (kind) {
    case 'findings':
      return 'info'
    case 'answer':
      return 'secondary'
    case 'documentation':
      return 'success'
    case 'code-changes':
      return 'warn'
    case 'review-verdict':
    case 'verification':
      return 'default'
    default:
      return 'outline'
  }
}
