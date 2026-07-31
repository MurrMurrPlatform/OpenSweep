// Display metadata + the client-side view of the ticket status machine.
// The backend enforces transitions (409 on illegal ones); this mirrors it for the UI.

import type { BadgeVariants } from '@/components/ui/badge'
import type { ThreadPhase, TicketPriority, TicketStatus } from '@/types/api'

export const STATUS_ORDER: TicketStatus[] = ['backlog', 'todo', 'in-progress', 'in-review', 'done']

export const STATUS_LABELS: Record<TicketStatus, string> = {
  backlog: 'Backlog',
  todo: 'Todo',
  'in-progress': 'In Progress',
  'in-review': 'In Review',
  done: 'Done',
}

export interface TicketTransition {
  to: TicketStatus
  label: string
  /** gate = human Gate 1 (Approve), forward = next step, back = step back. */
  kind: 'gate' | 'forward' | 'back'
}

export const TRANSITIONS: Record<TicketStatus, TicketTransition[]> = {
  backlog: [{ to: 'todo', label: 'Approve', kind: 'gate' }],
  todo: [
    { to: 'in-progress', label: 'Start', kind: 'forward' },
    { to: 'backlog', label: 'Back to backlog', kind: 'back' },
  ],
  'in-progress': [
    { to: 'in-review', label: 'To review', kind: 'forward' },
    { to: 'todo', label: 'Back to todo', kind: 'back' },
    { to: 'backlog', label: 'To backlog', kind: 'back' },
  ],
  'in-review': [
    { to: 'done', label: 'Done', kind: 'forward' },
    { to: 'in-progress', label: 'Back to in progress', kind: 'back' },
    { to: 'backlog', label: 'To backlog', kind: 'back' },
  ],
  done: [],
}

/**
 * The transitions actually offered for a ticket.
 *
 * An epic ships as ONE run and ONE PR against the parent, so the parent is the
 * only thing that passes Gate 1 — approving a member would make it separately
 * implementable and race the epic's own PR over the same files. The backend
 * 409s on it; dropping it here means the buttons, the kebab menu and the
 * board's drag targets all agree without each re-deriving the rule.
 */
export function transitionsFor(ticket: {
  status: TicketStatus
  parent_ticket_uid?: string
}): TicketTransition[] {
  const all = TRANSITIONS[ticket.status] ?? []
  return ticket.parent_ticket_uid ? all.filter((t) => t.kind !== 'gate') : all
}

export type BadgeVariant = BadgeVariants['variant']

export function priorityVariant(priority: TicketPriority): BadgeVariant {
  switch (priority) {
    case 'urgent':
      return 'destructive'
    case 'high':
      return 'warn'
    case 'medium':
      return 'info'
    default:
      return 'secondary'
  }
}

export function statusVariant(status: TicketStatus): BadgeVariant {
  switch (status) {
    case 'done':
      return 'success'
    case 'in-review':
      return 'info'
    case 'in-progress':
      return 'default'
    case 'todo':
      return 'warn'
    default:
      return 'secondary'
  }
}

/** Thread phase, as the board and detail pages present it — one shared map
 *  so a card chip and the detail header never disagree on wording. */
export function threadPhaseLabel(phase: ThreadPhase): string {
  switch (phase) {
    case 'refining':
      return 'Planning'
    case 'implementing':
      return 'Implementing'
    case 'in_review':
      return 'In review'
    case 'done':
      return 'Done'
    default:
      return 'Abandoned'
  }
}

export function threadPhaseVariant(phase: ThreadPhase): BadgeVariant {
  switch (phase) {
    case 'refining':
      return 'secondary'
    case 'implementing':
      return 'info'
    case 'in_review':
      return 'success'
    default:
      return 'outline'
  }
}
