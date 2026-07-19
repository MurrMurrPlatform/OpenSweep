import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiDelete, apiGet, apiPatch, apiPost } from '@/services/api'
import type {
  CreateScheduledAgentRequest,
  RunDTO,
  ScheduledAgentDTO,
  UpdateScheduledAgentRequest,
} from '@/types/api'

export const useScheduledAgentStore = defineStore('scheduledAgents', () => {
  const list = ref<ScheduledAgentDTO[]>([])
  const runsByScheduledAgent = ref<Record<string, RunDTO[]>>({})

  async function fetchAll(repository_uid?: string): Promise<ScheduledAgentDTO[]> {
    const qs = repository_uid ? `?repository_uid=${repository_uid}` : ''
    const data = await apiGet<ScheduledAgentDTO[]>(`/scheduled-agents${qs}`)
    list.value = data
    return data
  }

  async function get(uid: string): Promise<ScheduledAgentDTO> {
    return apiGet<ScheduledAgentDTO>(`/scheduled-agents/${uid}`)
  }

  async function create(req: CreateScheduledAgentRequest): Promise<ScheduledAgentDTO> {
    const sa = await apiPost<ScheduledAgentDTO>(`/scheduled-agents`, req)
    list.value = [sa, ...list.value]
    return sa
  }

  async function update(uid: string, req: UpdateScheduledAgentRequest): Promise<ScheduledAgentDTO> {
    const sa = await apiPatch<ScheduledAgentDTO>(`/scheduled-agents/${uid}`, req)
    list.value = list.value.map((s) => (s.uid === sa.uid ? sa : s))
    return sa
  }

  async function remove(uid: string): Promise<void> {
    await apiDelete(`/scheduled-agents/${uid}`)
    list.value = list.value.filter((s) => s.uid !== uid)
  }

  async function trigger(uid: string): Promise<RunDTO> {
    return apiPost<RunDTO>(`/scheduled-agents/${uid}/trigger`, {})
  }

  async function fetchRuns(uid: string): Promise<RunDTO[]> {
    const data = await apiGet<RunDTO[]>(`/scheduled-agents/${uid}/runs`)
    runsByScheduledAgent.value = { ...runsByScheduledAgent.value, [uid]: data }
    return data
  }

  return {
    list,
    runsByScheduledAgent,
    fetchAll,
    get,
    create,
    update,
    remove,
    trigger,
    fetchRuns,
  }
})
