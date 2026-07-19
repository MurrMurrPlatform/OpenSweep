import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiDelete, apiGet, apiPatch, apiPost } from '@/services/api'

export interface AgentPromptDTO {
  uid: string
  title: string
  description: string
  body: string
  default_job_type: string
  default_scope: 'repository' | 'paths'
  default_effort: 'short' | 'normal' | 'deep' | 'unlimited' | 'quick' | 'small' | 'large'
  tags: string[]
  source: 'platform' | 'user' | 'imported'
  source_url: string
  source_commit: string
  enabled: boolean
  created_at?: string | null
  updated_at?: string | null
}

export interface CreateAgentPromptRequest {
  title: string
  description?: string
  body?: string
  default_job_type?: string
  default_scope?: 'repository' | 'paths'
  default_effort?: string
  tags?: string[]
  enabled?: boolean
}

export type UpdateAgentPromptRequest = Partial<CreateAgentPromptRequest>

/**
 * Seeded per-playbook base instructions (opensweep://agent/<playbook>) — an
 * internal composition layer that is already part of every run of that
 * playbook. Never a meaningful choice in a run-launch or stage-guidance
 * picker: selecting it would only duplicate what composition already adds.
 */
export function isAgentBasePrompt(p: AgentPromptDTO): boolean {
  return p.source === 'platform' && p.source_url.startsWith('opensweep://agent/')
}

/**
 * Seeded per-stage workflow default (opensweep://workflow/<stage>) — what an
 * unconfigured workflow stage already resolves to. In run-launch pickers the
 * "default" option covers it, so listing it as a separate entry just shows
 * the same run twice under two names.
 */
export function isStageDefaultPrompt(p: AgentPromptDTO): boolean {
  return p.source === 'platform' && p.source_url.startsWith('opensweep://workflow/')
}

export interface ImportEccResult {
  imported: number
  skipped_user_edited: number
  source_commit: string
  errors: string[]
}

export const useAgentPromptStore = defineStore('agentPrompts', () => {
  const prompts = ref<AgentPromptDTO[]>([])

  async function fetchAll(filters: { tag?: string; source?: string; enabled_only?: boolean } = {}) {
    const params = new URLSearchParams()
    if (filters.tag) params.set('tag', filters.tag)
    if (filters.source) params.set('source', filters.source)
    if (filters.enabled_only) params.set('enabled_only', 'true')
    const suffix = params.toString() ? `?${params.toString()}` : ''
    const list = await apiGet<AgentPromptDTO[]>(`/agent-prompts${suffix}`)
    prompts.value = list
    return list
  }

  async function fetchByUid(uid: string): Promise<AgentPromptDTO> {
    return apiGet<AgentPromptDTO>(`/agent-prompts/${uid}`)
  }

  async function create(req: CreateAgentPromptRequest): Promise<AgentPromptDTO> {
    return apiPost<AgentPromptDTO>('/agent-prompts', req)
  }

  async function update(uid: string, req: UpdateAgentPromptRequest): Promise<AgentPromptDTO> {
    return apiPatch<AgentPromptDTO>(`/agent-prompts/${uid}`, req)
  }

  async function remove(uid: string): Promise<void> {
    await apiDelete(`/agent-prompts/${uid}`)
  }

  async function importEcc(): Promise<ImportEccResult> {
    return apiPost<ImportEccResult>('/agent-prompts/import-ecc')
  }

  return { prompts, fetchAll, fetchByUid, create, update, remove, importEcc }
})
