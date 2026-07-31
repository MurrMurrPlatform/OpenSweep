import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiGet } from '@/services/api'
import type { AuditEvent } from '@/types/api'

export const useAuditStore = defineStore('audit', () => {
  const list = ref<AuditEvent[]>([])
  const loaded = ref(false)

  async function fetchAll(
    opts: { subject_type?: string; kind?: string; repository_uid?: string; limit?: number } = {},
  ) {
    const qs = new URLSearchParams()
    if (opts.subject_type) qs.set('subject_type', opts.subject_type)
    if (opts.kind) qs.set('kind', opts.kind)
    if (opts.repository_uid) qs.set('repository_uid', opts.repository_uid)
    qs.set('limit', String(opts.limit ?? 100))
    list.value = await apiGet<AuditEvent[]>(`/audit?${qs}`)
    loaded.value = true
  }

  return { list, loaded, fetchAll }
})
