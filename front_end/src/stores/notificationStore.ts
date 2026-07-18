import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiGet, apiPost } from '@/services/api'
import type { NotificationCountsDTO, NotificationDTO } from '@/types/api'

const EMPTY_COUNTS: NotificationCountsDTO = { total: 0, attention: 0, activity: 0, mentions: 0 }

export const useNotificationStore = defineStore('notifications', () => {
  const list = ref<NotificationDTO[]>([])
  const counts = ref<NotificationCountsDTO>({ ...EMPTY_COUNTS })
  const loaded = ref(false)

  async function fetchList(
    opts: { category?: string; repository_uid?: string; unread?: boolean; limit?: number } = {},
  ) {
    const qs = new URLSearchParams()
    if (opts.category) qs.set('category', opts.category)
    if (opts.repository_uid) qs.set('repository_uid', opts.repository_uid)
    if (opts.unread) qs.set('unread', 'true')
    qs.set('limit', String(opts.limit ?? 100))
    list.value = await apiGet<NotificationDTO[]>(`/notifications?${qs}`)
    loaded.value = true
  }

  async function fetchCounts() {
    counts.value = await apiGet<NotificationCountsDTO>('/notifications/counts')
  }

  async function markRead(uid: string) {
    await apiPost(`/notifications/${uid}/read`)
    const now = new Date().toISOString()
    list.value = list.value.map((n) => (n.uid === uid && !n.read_at ? { ...n, read_at: now } : n))
    await fetchCounts()
  }

  async function dismiss(uid: string) {
    await apiPost(`/notifications/${uid}/dismiss`)
    list.value = list.value.filter((n) => n.uid !== uid)
    await fetchCounts()
  }

  async function markAllRead() {
    await apiPost('/notifications/read-all')
    const now = new Date().toISOString()
    list.value = list.value.map((n) => (n.read_at ? n : { ...n, read_at: now }))
    await fetchCounts()
  }

  return { list, counts, loaded, fetchList, fetchCounts, markRead, dismiss, markAllRead }
})
