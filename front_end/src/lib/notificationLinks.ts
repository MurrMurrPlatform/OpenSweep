// Deep-link resolution for notification items — shared by the topbar bell
// popover and the /notifications view. Flat detail routes resolve from the
// subject alone; workspace-scoped surfaces (news, docs) need the repo slug.

import type { RouteLocationRaw } from 'vue-router'
import type { NotificationDTO } from '@/types/api'

/** subject_type (audit) → flat detail route name. */
const DETAIL_ROUTES: Record<string, string> = {
  Run: 'run-detail',
  Ticket: 'ticket-detail',
  Finding: 'finding-detail',
  PullRequest: 'pull-request-detail',
  Analysis: 'analysis-detail',
  ScheduledAgent: 'scheduled-agent-detail',
  // Pre-migration notification rows keep resolving.
  Investigation: 'scheduled-agent-detail',
}

/** comment_subject_type (payload) → flat detail route name for the thread. */
const COMMENT_SUBJECT_ROUTES: Record<string, string> = {
  finding: 'finding-detail',
  ticket: 'ticket-detail',
  pull_request: 'pull-request-detail',
  run: 'run-detail',
  scheduled_agent: 'scheduled-agent-detail',
  investigation: 'scheduled-agent-detail',
}

/**
 * Where a notification should navigate, or null when its subject has no page
 * (callers render such items unlinked). `repoSlug` is the slug of
 * `n.repository_uid` when the caller could resolve it.
 */
export function notificationLink(n: NotificationDTO, repoSlug?: string): RouteLocationRaw | null {
  // Comment events link to the thread's subject, not the Comment node.
  if (n.subject_type === 'Comment') {
    const type = String(n.payload.comment_subject_type || '')
    const uid = String(n.payload.comment_subject_uid || '')
    const name = COMMENT_SUBJECT_ROUTES[type]
    if (name && uid) return { name, params: { uid } }
    if (repoSlug && type === 'news_item') return { name: 'news', params: { repoSlug } }
    if (repoSlug && type === 'doc') return { name: 'documentation', params: { repoSlug } }
    return null
  }
  const name = DETAIL_ROUTES[n.subject_type]
  if (name && n.subject_uid) return { name, params: { uid: n.subject_uid } }
  if (repoSlug && n.subject_type === 'NewsItem') return { name: 'news', params: { repoSlug } }
  if (repoSlug && n.subject_type === 'Doc') return { name: 'documentation', params: { repoSlug } }
  if (repoSlug && n.subject_type === 'Repository') return { name: 'workspace-home', params: { repoSlug } }
  return null
}
