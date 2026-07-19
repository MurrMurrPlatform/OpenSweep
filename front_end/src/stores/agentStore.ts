import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiDelete, apiGet, apiPatch, apiPost, apiPut } from '@/services/api'
import type {
  AgentDTO,
  AgentDispatchRequest,
  AgentRevisionDTO,
  CreateAgentRequest,
  RunDTO,
  SaveOverrideRequest,
  UpdateAgentRequest,
} from '@/types/api'

export interface AgentFilters {
  tag?: string
  provenance?: string
  produces?: string
  enabled_only?: boolean
}

export const useAgentStore = defineStore('agents', () => {
  const list = ref<AgentDTO[]>([])
  const loaded = ref(false)

  async function fetchAll(filters: AgentFilters = {}): Promise<AgentDTO[]> {
    const params = new URLSearchParams()
    if (filters.tag) params.set('tag', filters.tag)
    if (filters.provenance) params.set('provenance', filters.provenance)
    if (filters.produces) params.set('produces', filters.produces)
    if (filters.enabled_only) params.set('enabled_only', 'true')
    const qs = params.toString()
    const data = await apiGet<AgentDTO[]>(`/agents${qs ? `?${qs}` : ''}`)
    if (!qs) list.value = data
    loaded.value = true
    return data
  }

  async function get(uid: string): Promise<AgentDTO> {
    return apiGet<AgentDTO>(`/agents/${uid}`)
  }

  async function create(req: CreateAgentRequest): Promise<AgentDTO> {
    const agent = await apiPost<AgentDTO>(`/agents`, req)
    list.value = [agent, ...list.value]
    return agent
  }

  async function update(uid: string, req: UpdateAgentRequest): Promise<AgentDTO> {
    const agent = await apiPatch<AgentDTO>(`/agents/${uid}`, req)
    list.value = list.value.map((a) => (a.uid === agent.uid ? agent : a))
    return agent
  }

  async function remove(uid: string): Promise<void> {
    await apiDelete(`/agents/${uid}`)
    list.value = list.value.filter((a) => a.uid !== uid)
  }

  async function fetchRevisions(uid: string): Promise<AgentRevisionDTO[]> {
    return apiGet<AgentRevisionDTO[]>(`/agents/${uid}/revisions`)
  }

  /** Save the org's override of a system agent (append/replace body). */
  async function saveOverride(uid: string, req: SaveOverrideRequest): Promise<AgentRevisionDTO> {
    return apiPut<AgentRevisionDTO>(`/agents/${uid}/override`, req)
  }

  /** Restore the platform default (appends a disabled tombstone revision). */
  async function restoreDefault(uid: string): Promise<void> {
    await apiDelete(`/agents/${uid}/override`)
  }

  async function revert(uid: string, rev: number): Promise<AgentRevisionDTO> {
    return apiPost<AgentRevisionDTO>(`/agents/${uid}/revert`, { rev })
  }

  async function preview(uid: string, mode: string, body: string): Promise<string> {
    const res = await apiPost<{ prompt: string }>(`/agents/${uid}/preview`, { mode, body })
    return res.prompt
  }

  /** Ad-hoc run of an agent on a repository. */
  async function dispatch(uid: string, req: AgentDispatchRequest): Promise<RunDTO> {
    return apiPost<RunDTO>(`/agents/${uid}/runs`, req)
  }

  async function importEcc(): Promise<{ imported: number }> {
    return apiPost<{ imported: number }>(`/agents/import-ecc`)
  }

  return {
    list,
    loaded,
    fetchAll,
    get,
    create,
    update,
    remove,
    fetchRevisions,
    saveOverride,
    restoreDefault,
    revert,
    preview,
    dispatch,
    importEcc,
  }
})
