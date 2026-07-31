/**
 * The selection vocabulary both epic producers share.
 *
 * "Take the top 20 criticals in the auth area" is one sentence with one
 * meaning, and the backend enforces that by routing both `/tickets/plan-epics`
 * and `/tickets/suggest-epics` through a single `EpicSelection`. Two dialogs
 * each carrying their own severity ladder and their own sort list is how the
 * two paths quietly stop meaning the same thing — so the lists live here once.
 *
 * Only the SELECTION half is shared. The rule builder's partition knobs
 * (`max_epics`, `min_members`, `max_members`) have no counterpart on the agent
 * path: how big an epic should be is part of what the agent is asked to judge.
 */
import type { TicketStatus } from '@/types/api'

/** Sentinel for "no floor" — reka Select rejects an empty-string value. */
export const ANY = 'any'

export const SEVERITY_OPTIONS = ['low', 'medium', 'high', 'critical']
export const PRIORITY_OPTIONS = ['low', 'medium', 'high', 'urgent']

export const STATUS_OPTIONS: { value: TicketStatus; label: string }[] = [
  { value: 'backlog', label: 'Backlog' },
  { value: 'todo', label: 'Todo' },
]

export const SORT_OPTIONS = [
  { value: 'priority', label: 'Priority: high → low' },
  { value: 'severity', label: 'Severity: high → low' },
  { value: 'age', label: 'Age: oldest first' },
  { value: 'size', label: 'Size: smallest first' },
]

/** `ANY` → `''`, the backend's "unset filter admits everything". */
export function floorValue(value: string): string {
  return value === ANY ? '' : value
}

/**
 * Toggle a status in a FIXED order, so ticking two statuses back and forth
 * cannot produce a different request object for the same selection — which
 * would re-fire a watcher on `request` and re-run a preview for nothing.
 */
export function toggleStatus(current: TicketStatus[], status: TicketStatus): TicketStatus[] {
  const next = current.includes(status)
    ? current.filter((s) => s !== status)
    : [...current, status]
  return STATUS_OPTIONS.map((o) => o.value).filter((s) => next.includes(s))
}
