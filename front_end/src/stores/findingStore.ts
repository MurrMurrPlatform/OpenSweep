import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiDelete, apiGet, apiPatch, apiPost } from '@/services/api'
import type {
  FindingDTO,
  FindingKind,
  FindingStatus,
  RatchetDispatch,
  RatchetRequest,
  RunDTO,
  Severity,
} from '@/types/api'

/** Whitelisted server-side sort fields (see back_end api/v1/findings.py). */
export type FindingSortBy = 'updated_at' | 'created_at' | 'severity' | 'confidence' | 'title' | 'trust'

export const useFindingStore = defineStore('findings', () => {
  const list = ref<FindingDTO[]>([])

  async function fetchAll(opts: {
    repository_uid?: string
    source_run_uid?: string
    tag?: string
    kind?: FindingKind
    exclude_kind?: string
    status?: FindingStatus
    severity?: Severity
    sort_by?: FindingSortBy
    sort_dir?: 'asc' | 'desc'
  } = {}): Promise<FindingDTO[]> {
    const qs = new URLSearchParams()
    Object.entries(opts).forEach(([k, v]) => {
      if (v) qs.set(k, String(v))
    })
    const url = qs.toString() ? `/findings?${qs.toString()}` : '/findings'
    const data = await apiGet<FindingDTO[]>(url)
    list.value = data
    return data
  }

  async function get(uid: string): Promise<FindingDTO> {
    return apiGet<FindingDTO>(`/findings/${uid}`)
  }

  /** File a finding by hand (manual triage / audit report). */
  async function create(req: Partial<FindingDTO> & { repository_uid: string; title: string }): Promise<FindingDTO> {
    const f = await apiPost<FindingDTO>('/findings', req)
    list.value = [f, ...list.value]
    return f
  }

  /** Human correction of narrative/triage fields. Status is not editable here. */
  async function update(uid: string, req: Partial<FindingDTO>): Promise<FindingDTO> {
    const f = await apiPatch<FindingDTO>(`/findings/${uid}`, req)
    list.value = list.value.map((x) => (x.uid === uid ? f : x))
    return f
  }

  async function dismiss(uid: string): Promise<FindingDTO> {
    const f = await apiPost<FindingDTO>(`/findings/${uid}/dismiss`)
    list.value = list.value.map((x) => (x.uid === uid ? f : x))
    return f
  }

  async function acknowledge(uid: string): Promise<FindingDTO> {
    const f = await apiPost<FindingDTO>(`/findings/${uid}/acknowledge`)
    list.value = list.value.map((x) => (x.uid === uid ? f : x))
    return f
  }

  async function wontFix(uid: string): Promise<FindingDTO> {
    const f = await apiPost<FindingDTO>(`/findings/${uid}/wont-fix`)
    list.value = list.value.map((x) => (x.uid === uid ? f : x))
    return f
  }

  async function markFixed(uid: string): Promise<FindingDTO> {
    const f = await apiPost<FindingDTO>(`/findings/${uid}/mark-fixed`)
    list.value = list.value.map((x) => (x.uid === uid ? f : x))
    return f
  }

  async function remove(uid: string): Promise<void> {
    await apiDelete(`/findings/${uid}`)
    list.value = list.value.filter((x) => x.uid !== uid)
  }

  async function removeMany(uids: string[]): Promise<{ deleted: number; missing: number }> {
    const result = await apiPost<{ deleted: number; missing: number }>(`/findings/bulk-delete`, { uids })
    const dead = new Set(uids)
    list.value = list.value.filter((x) => !dead.has(x.uid))
    return result
  }

  /** Ratchet-run: turn a recurring finding class (subtype) into a
   *  born-approved guard ticket + implement run. 404 when nothing matches. */
  async function triggerRatchet(req: RatchetRequest): Promise<RatchetDispatch> {
    return apiPost<RatchetDispatch>('/findings/ratchet', req)
  }

  async function launchVerification(uid: string): Promise<RunDTO> {
    return apiPost<RunDTO>(`/findings/${uid}/verify`)
  }

  /** Refine-run: read-only triage that verifies the finding is real and
   *  sharpens its title/description + attaches a plan via the platform tools. */
  async function launchRefine(uid: string): Promise<RunDTO> {
    return apiPost<RunDTO>(`/findings/${uid}/refine`)
  }

  async function listVerifications(uid: string): Promise<RunDTO[]> {
    return apiGet<RunDTO[]>(`/findings/${uid}/verifications`)
  }

  return {
    list,
    fetchAll,
    get,
    create,
    update,
    dismiss,
    acknowledge,
    wontFix,
    markFixed,
    remove,
    removeMany,
    triggerRatchet,
    launchVerification,
    launchRefine,
    listVerifications,
  }
})
