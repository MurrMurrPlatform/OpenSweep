/**
 * Mention tokens — mirror of back_end/domains/comments/mentions.py.
 *
 * `@opensweep` summons the platform agent; `@[Label](type:uid)` references a
 * data item. Bodies are stored as plain text with these tokens inline; the
 * UI parses them into segments for rendering.
 */

import type { MentionTargetType } from '@/types/api'

const OPENSWEEP_RE = /@opensweep\b/gi
const ITEM_RE = /@\[([^\]]+)\]\((\w+):([A-Za-z0-9_-]+)\)/g

export interface MentionSegment {
  kind: 'text' | 'opensweep' | 'item'
  text: string
  type?: MentionTargetType
  uid?: string
}

/** Every type offered in the @-dropdown, with its display label. */
export const MENTION_TYPES: { type: MentionTargetType; label: string }[] = [
  { type: 'ticket', label: 'Ticket' },
  { type: 'finding', label: 'Finding' },
  { type: 'pull_request', label: 'Pull request' },
  { type: 'news_item', label: 'News item' },
  { type: 'doc', label: 'Doc' },
  { type: 'run', label: 'Run' },
  { type: 'scheduled_agent', label: 'Scheduled agent' },
  { type: 'group', label: 'Ticket group' },
]

/** Detail-route name per mentionable type; undefined = render without a link. */
export const MENTION_ROUTES: Partial<Record<MentionTargetType | 'investigation', string>> = {
  ticket: 'ticket-detail',
  finding: 'finding-detail',
  pull_request: 'pull-request-detail',
  run: 'run-detail',
  scheduled_agent: 'scheduled-agent-detail',
  // Pre-migration mention tokens in old comment bodies keep resolving.
  investigation: 'scheduled-agent-detail',
}

export function mentionToken(type: MentionTargetType, uid: string, label: string): string {
  return `@[${label.replace(/[[\]]/g, '')}](${type}:${uid})`
}

/** Split a body into text / @opensweep / item-mention segments, in order. */
export function parseMentionSegments(body: string): MentionSegment[] {
  const segments: MentionSegment[] = []
  // One combined scan keeps ordering stable across both token kinds.
  const combined = new RegExp(`${ITEM_RE.source}|${OPENSWEEP_RE.source}`, 'gi')
  let cursor = 0
  for (const match of body.matchAll(combined)) {
    const start = match.index ?? 0
    if (start > cursor) segments.push({ kind: 'text', text: body.slice(cursor, start) })
    if (match[1] !== undefined) {
      segments.push({
        kind: 'item',
        text: match[1],
        type: match[2] as MentionTargetType,
        uid: match[3],
      })
    } else {
      segments.push({ kind: 'opensweep', text: match[0] })
    }
    cursor = start + match[0].length
  }
  if (cursor < body.length) segments.push({ kind: 'text', text: body.slice(cursor) })
  return segments
}
