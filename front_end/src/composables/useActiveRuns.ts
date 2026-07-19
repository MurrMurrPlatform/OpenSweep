import { computed, onBeforeUnmount, ref, toValue, watch, type MaybeRefOrGetter } from 'vue'
import { useRunStore } from '@/stores/runStore'
import { ApiError } from '@/services/api'
import type { ActiveRunDTO, ActiveRunFilters, DispatchConflictDetail } from '@/types/api'

const POLL_MS = 5000

/**
 * In-flight run awareness for dispatch surfaces (PR review/fix, ticket
 * implement, repo sweep).
 *
 * Fetches GET /runs/active for the given subject filters on
 * mount (and whenever the filters resolve/change), then polls every ~5s while
 * at least one run is in flight. Polling stops on its own once every run
 * reaches a terminal state. Call `refresh()` after any dispatch, or
 * `noteDispatched()` to reflect a just-dispatched run immediately while the
 * next poll catches up.
 */
export function useActiveRuns(filters: MaybeRefOrGetter<ActiveRunFilters | null | undefined>) {
  const runs = useRunStore()
  const activeRuns = ref<ActiveRunDTO[]>([])
  let timer: number | undefined
  let generation = 0 // drops stale responses when the subject changes mid-flight

  // Chat runs are conversations, not work: they never gate a dispatch
  // surface (useDiscussions feeds the non-blocking DiscussionChip instead).
  const workRuns = computed(() => activeRuns.value.filter((r) => r.playbook !== 'chat'))
  const activeRun = computed<ActiveRunDTO | null>(() => workRuns.value[0] ?? null)
  const hasActive = computed(() => workRuns.value.length > 0)

  function syncTimer() {
    if (activeRuns.value.length && timer === undefined) {
      timer = window.setInterval(() => void refresh(), POLL_MS)
    } else if (!activeRuns.value.length && timer !== undefined) {
      window.clearInterval(timer)
      timer = undefined
    }
  }

  async function refresh(): Promise<ActiveRunDTO[]> {
    const f = toValue(filters)
    if (!f || Object.values(f).every((v) => !v)) {
      generation += 1
      activeRuns.value = []
      syncTimer()
      return []
    }
    const gen = ++generation
    try {
      const data = await runs.fetchActive(f)
      if (gen === generation) activeRuns.value = data
    } catch {
      /* transient fetch error — keep the last known state, next poll retries */
    }
    syncTimer()
    return activeRuns.value
  }

  /** Optimistically add a just-dispatched run so the surface flips to the
   *  in-flight state immediately; the next poll replaces it with the truth. */
  function noteDispatched(partial: {
    run_uid?: string
    scheduled_agent_uid?: string
    title?: string
    playbook?: string
  }): void {
    if (partial.run_uid && !activeRuns.value.some((r) => r.run_uid === partial.run_uid)) {
      const f = toValue(filters) || {}
      activeRuns.value = [
        {
          run_uid: partial.run_uid,
          scheduled_agent_uid: partial.scheduled_agent_uid || '',
          title: partial.title || '',
          playbook: partial.playbook || f.playbook || '',
          status: 'queued',
          started_at: null,
          repository_uid: f.repository_uid || '',
        },
        ...activeRuns.value,
      ]
    }
    syncTimer()
    void refresh()
  }

  watch(
    () => toValue(filters),
    () => void refresh(),
    { immediate: true, deep: true },
  )

  onBeforeUnmount(() => {
    if (timer !== undefined) {
      window.clearInterval(timer)
      timer = undefined
    }
  })

  return { activeRuns, workRuns, activeRun, hasActive, refresh, noteDispatched }
}

/**
 * Pulls the structured 409 detail ({message, run_uid, scheduled_agent_uid})
 * out of a dispatch error. Returns null for plain-string 409s and every other
 * error shape — callers fall back to the generic message toast.
 */
export function extractDispatchConflict(e: unknown): DispatchConflictDetail | null {
  if (!(e instanceof ApiError) || e.status !== 409) return null
  const body = e.detailBody
  if (!body || typeof body !== 'object' || Array.isArray(body)) return null
  const rec = body as Record<string, unknown>
  if (typeof rec.message !== 'string' || typeof rec.run_uid !== 'string' || !rec.run_uid) return null
  return {
    message: rec.message,
    run_uid: rec.run_uid,
    scheduled_agent_uid: typeof rec.scheduled_agent_uid === 'string' ? rec.scheduled_agent_uid : '',
  }
}
