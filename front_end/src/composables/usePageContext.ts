import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useCurrentRepo } from '@/composables/useCurrentRepo'
import type { CommentSubjectType } from '@/types/api'

export interface PageContext {
  /** null on global pages (repo picker territory). */
  repositoryUid: string | null
  /** The data item the user is looking at, when the page is a detail view. */
  subject: { type: CommentSubjectType; uid: string; label: string } | null
}

/** Detail routes → the comment-subject type opensweep understands. */
const DETAIL_ROUTES: Record<string, { type: CommentSubjectType; label: string }> = {
  'ticket-detail': { type: 'ticket', label: 'this ticket' },
  'finding-detail': { type: 'finding', label: 'this finding' },
  'pull-request-detail': { type: 'pull_request', label: 'this pull request' },
  'run-detail': { type: 'run', label: 'this run' },
  'scheduled-agent-detail': { type: 'scheduled_agent', label: 'this scheduled agent' },
  'area-detail': { type: 'area', label: 'this area' },
}

/**
 * What the user is currently viewing, for the opensweep chat bubble's
 * context-aware pickup. Detail pages carry a subject (whose repository the
 * backend resolves); repo-scoped pages carry the repository; global pages
 * carry neither.
 */
export function usePageContext() {
  const route = useRoute()
  const { uid: repoUid } = useCurrentRepo()

  const context = computed<PageContext>(() => {
    const name = typeof route.name === 'string' ? route.name : ''
    const detail = DETAIL_ROUTES[name]
    const rawUid = route.params.uid
    const uid = Array.isArray(rawUid) ? rawUid[0] : rawUid
    if (detail && uid) {
      return {
        repositoryUid: repoUid.value,
        subject: { type: detail.type, uid, label: detail.label },
      }
    }
    return { repositoryUid: repoUid.value, subject: null }
  })

  return { context }
}
