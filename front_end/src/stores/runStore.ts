import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiGet, apiPost } from '@/services/api'
import type {
  ActiveRunDTO,
  ActiveRunFilters,
  ArtifactDTO,
  CreateRunRequest,
  RunChangesDTO,
  RunDTO,
  RunHandoffDTO,
  RunMessageResult,
  RunTranscriptDTO,
} from '@/types/api'

export interface RunFilters {
  repository_uid?: string
  executor?: string
  status?: string
  playbook?: string
  linked_pr_uid?: string
  linked_ticket_uid?: string
  linked_finding_uid?: string
  /** Default (unset) returns only surface='runs'. 'chat' returns the
   *  caller's own chat sessions; 'comment'/'all' are platform-admin only. */
  surface?: 'runs' | 'comment' | 'chat' | 'all'
  limit?: number
}

export const useRunStore = defineStore('runs', () => {
  const list = ref<RunDTO[]>([])

  async function fetchAll(opts: RunFilters = {}): Promise<RunDTO[]> {
    const data = await query(opts)
    list.value = data
    return data
  }

  /** Non-mutating list query — subject pages (discussion chips) use this so
   *  they don't clobber the RunsView list. */
  async function query(opts: RunFilters = {}): Promise<RunDTO[]> {
    const qs = new URLSearchParams()
    Object.entries(opts).forEach(([k, v]) => v && qs.set(k, String(v)))
    const url = qs.toString() ? `/runs?${qs.toString()}` : '/runs'
    return apiGet<RunDTO[]>(url)
  }

  async function get(uid: string): Promise<RunDTO> {
    return apiGet<RunDTO>(`/runs/${uid}`)
  }

  /** One-off chat/ask run — a chat run clones its workspace in the background
   *  (status 'queued' until it flips 'awaiting_input'). */
  async function createRun(req: CreateRunRequest): Promise<RunDTO> {
    return apiPost<RunDTO>('/runs', req)
  }

  /** Blocking REST follow-up turn — 409 while a turn is in flight. Accepted
   *  from awaiting_input AND ended/failed/cancelled/limit_exceeded (replying
   *  to a failed run is the recovery loop). */
  async function sendMessage(uid: string, text: string): Promise<RunMessageResult> {
    return apiPost<RunMessageResult>(`/runs/${uid}/messages`, { text })
  }

  /** Cut the current turn short — the run drops back to awaiting_input. */
  async function interrupt(uid: string): Promise<RunDTO> {
    return apiPost<RunDTO>(`/runs/${uid}/interrupt`)
  }

  /** Prepare a terminal takeover: writes the handoff brief into the workspace
   *  and returns the one-paste command for the user's shell. */
  async function handoff(uid: string): Promise<RunHandoffDTO> {
    return apiPost<RunHandoffDTO>(`/runs/${uid}/handoff`)
  }

  /** Close the run: destroys the workspace, keeps the transcript. A later
   *  follow-up message reopens the conversation. */
  async function end(uid: string): Promise<RunDTO> {
    return apiPost<RunDTO>(`/runs/${uid}/end`)
  }

  /** Cancel an active (queued/running/paused_quota) run: terminal `cancelled`
   *  status, kills any in-flight turn. 409 when the run isn't active. */
  async function cancel(uid: string): Promise<RunDTO> {
    return apiPost<RunDTO>(`/runs/${uid}/cancel`)
  }

  /** Rebuild an expired/destroyed workspace from the recorded spec. */
  async function recreateWorkspace(uid: string): Promise<RunDTO> {
    return apiPost<RunDTO>(`/runs/${uid}/workspace/recreate`)
  }

  async function getArtifact(uri: string): Promise<ArtifactDTO> {
    const qs = new URLSearchParams({ uri })
    return apiGet<ArtifactDTO>(`/artifacts?${qs.toString()}`)
  }

  /** In-flight (queued/running/paused_quota) runs for a subject — used by the
   *  dispatch surfaces to disable buttons and link to the blocking run. */
  async function fetchActive(opts: ActiveRunFilters = {}): Promise<ActiveRunDTO[]> {
    const qs = new URLSearchParams()
    Object.entries(opts).forEach(([k, v]) => v && qs.set(k, String(v)))
    const url = qs.toString() ? `/runs/active?${qs.toString()}` : '/runs/active'
    return apiGet<ActiveRunDTO[]>(url)
  }

  /** Incremental structured transcript — poll with the last_seq returned by
   *  the previous chunk. */
  async function getTranscript(uid: string, afterSeq = 0): Promise<RunTranscriptDTO> {
    return apiGet<RunTranscriptDTO>(`/runs/${uid}/transcript?after_seq=${afterSeq}`)
  }

  /** Files the agent added/modified/deleted plus the workspace file tree —
   *  live diff while the workspace exists, end-of-run snapshot afterwards. */
  async function getChanges(uid: string): Promise<RunChangesDTO> {
    return apiGet<RunChangesDTO>(`/runs/${uid}/changes`)
  }

  return {
    list,
    fetchAll,
    query,
    get,
    createRun,
    sendMessage,
    interrupt,
    handoff,
    end,
    cancel,
    recreateWorkspace,
    getArtifact,
    fetchActive,
    getTranscript,
    getChanges,
  }
})
