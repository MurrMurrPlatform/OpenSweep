import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiDelete, apiGet, apiPatch, apiPost } from '@/services/api'
import type {
  AcceptAreaEditResponse,
  AreaDTO,
  AreaEditDTO,
  AreaEditStatus,
  MapAreasResponse,
  UpdateAreaRequest,
} from '@/types/api'

/** Response of the bulk accept/reject endpoints — per-uid outcomes. */
export interface BulkAreaEditResult {
  accepted?: string[]
  rejected?: string[]
  /** bulk-accept only: partition warnings per accepted edit. */
  warnings?: Record<string, string[]>
  errors: Record<string, string>
}

export const useAreaStore = defineStore('areas', () => {
  const areas = ref<AreaDTO[]>([])
  const edits = ref<AreaEditDTO[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function fetchAreas(repoUid: string): Promise<AreaDTO[]> {
    loading.value = true
    error.value = null
    try {
      const data = await apiGet<AreaDTO[]>(`/areas?repository_uid=${encodeURIComponent(repoUid)}`)
      areas.value = data
      return data
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : String(e)
      throw e
    } finally {
      loading.value = false
    }
  }

  async function fetchEdits(repoUid: string, status: AreaEditStatus = 'pending'): Promise<AreaEditDTO[]> {
    const qs = new URLSearchParams({ repository_uid: repoUid, status })
    const data = await apiGet<AreaEditDTO[]>(`/area-edits?${qs.toString()}`)
    edits.value = data
    return data
  }

  async function patchArea(uid: string, body: UpdateAreaRequest): Promise<AreaDTO> {
    const area = await apiPatch<AreaDTO>(`/areas/${uid}`, body)
    areas.value = areas.value.map((a) => (a.uid === uid ? area : a))
    return area
  }

  async function deleteArea(uid: string): Promise<void> {
    await apiDelete(`/areas/${uid}`)
    areas.value = areas.value.filter((a) => a.uid !== uid)
    // Pending edits against a deleted area are auto-rejected server-side.
    edits.value = edits.value.filter((e) => e.area_uid !== uid)
  }

  /** Applies the edit's full replacement; returns partition warnings for a toast. */
  async function acceptEdit(uid: string): Promise<AcceptAreaEditResponse> {
    const result = await apiPost<AcceptAreaEditResponse>(`/area-edits/${uid}/accept`)
    edits.value = edits.value.filter((e) => e.uid !== uid)
    const area = result.area
    areas.value = areas.value.some((a) => a.uid === area.uid)
      ? areas.value.map((a) => (a.uid === area.uid ? area : a))
      : [...areas.value, area].sort((a, b) => a.key.localeCompare(b.key))
    return result
  }

  async function rejectEdit(uid: string): Promise<AreaEditDTO> {
    const edit = await apiPost<AreaEditDTO>(`/area-edits/${uid}/reject`)
    edits.value = edits.value.filter((e) => e.uid !== uid)
    return edit
  }

  async function bulkAccept(uids: string[]): Promise<BulkAreaEditResult> {
    const result = await apiPost<BulkAreaEditResult>('/area-edits/bulk-accept', { uids })
    // Only drop the edits the server confirmed — failed ones stay reviewable.
    const done = new Set(result.accepted ?? [])
    edits.value = edits.value.filter((e) => !done.has(e.uid))
    return result
  }

  async function bulkReject(uids: string[]): Promise<BulkAreaEditResult> {
    const result = await apiPost<BulkAreaEditResult>('/area-edits/bulk-reject', { uids })
    // Only drop the edits the server confirmed — failed ones stay reviewable.
    const done = new Set(result.rejected ?? [])
    edits.value = edits.value.filter((e) => !done.has(e.uid))
    return result
  }

  /** One LLM run that proposes the area map (409 when already running). */
  /** Destructive: deletes every area + edit for the repo. */
  async function resetAll(repoUid: string): Promise<{ areas_deleted: number; edits_deleted: number }> {
    const result = await apiPost<{ areas_deleted: number; edits_deleted: number }>(
      `/repositories/${repoUid}/areas/reset`,
    )
    areas.value = []
    edits.value = []
    return result
  }

  async function mapNow(repoUid: string): Promise<MapAreasResponse> {
    return apiPost<MapAreasResponse>(`/repositories/${repoUid}/sweep/map-areas`)
  }

  return {
    areas,
    edits,
    loading,
    error,
    fetchAreas,
    fetchEdits,
    patchArea,
    deleteArea,
    acceptEdit,
    rejectEdit,
    bulkAccept,
    bulkReject,
    mapNow,
    resetAll,
  }
})
