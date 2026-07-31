import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiGet } from '@/services/api'
import type { RepoAttentionDTO } from '@/types/api'

export const useAttentionStore = defineStore('attention', () => {
  const attention = ref<RepoAttentionDTO | null>(null)
  const loaded = ref(false)

  // Drops stale responses when the workspace switches mid-flight.
  let generation = 0

  async function fetchFor(repositoryUid: string): Promise<RepoAttentionDTO | null> {
    const gen = ++generation
    const data = await apiGet<RepoAttentionDTO>(`/repositories/${repositoryUid}/attention`)
    if (gen === generation) {
      attention.value = data
      loaded.value = true
    }
    return data
  }

  function reset() {
    generation += 1
    attention.value = null
    loaded.value = false
  }

  return { attention, loaded, fetchFor, reset }
})
