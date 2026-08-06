import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiDelete, apiGet } from '@/services/api'
import type { Sandbox } from '@/types/api'

export const useSandboxStore = defineStore('sandboxes', () => {
  const list = ref<Sandbox[]>([])
  const loaded = ref(false)

  async function fetchAll() {
    list.value = await apiGet<Sandbox[]>('/sandboxes')
    loaded.value = true
  }

  async function destroy(uid: string) {
    const s = await apiDelete<Sandbox>(`/sandboxes/${uid}`)
    list.value = list.value.filter(x => x.uid !== uid)
    return s
  }

  return { list, loaded, fetchAll, destroy }
})
