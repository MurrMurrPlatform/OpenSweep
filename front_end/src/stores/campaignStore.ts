import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiGet, apiPost } from '@/services/api'
import type { CampaignAreasPreview, CampaignDTO, CreateCampaignRequest } from '@/types/api'

export const useCampaignStore = defineStore('campaigns', () => {
  const list = ref<CampaignDTO[]>([])

  async function fetchForRepo(repository_uid: string): Promise<CampaignDTO[]> {
    const data = await apiGet<CampaignDTO[]>(`/repositories/${repository_uid}/campaigns`)
    list.value = data
    return data
  }

  async function get(uid: string): Promise<CampaignDTO> {
    return apiGet<CampaignDTO>(`/campaigns/${uid}`)
  }

  /** The partition a campaign would use right now — computed live, nothing persisted.
   *  `areaPrefix` scopes the preview to areas under that key prefix. */
  async function fetchAreas(repository_uid: string, areaPrefix = ''): Promise<CampaignAreasPreview> {
    const qs = areaPrefix ? `?area_prefix=${encodeURIComponent(areaPrefix)}` : ''
    return apiGet<CampaignAreasPreview>(`/repositories/${repository_uid}/campaign-areas${qs}`)
  }

  /** Plans only (status=planning) — launch is the separate, explicit go signal. */
  async function create(repository_uid: string, req: CreateCampaignRequest): Promise<CampaignDTO> {
    const c = await apiPost<CampaignDTO>(`/repositories/${repository_uid}/campaigns`, req)
    list.value = [c, ...list.value]
    return c
  }

  async function launch(uid: string): Promise<CampaignDTO> {
    const c = await apiPost<CampaignDTO>(`/campaigns/${uid}/launch`)
    list.value = list.value.map((x) => (x.uid === c.uid ? c : x))
    return c
  }

  async function cancel(uid: string, reason = ''): Promise<CampaignDTO> {
    const c = await apiPost<CampaignDTO>(`/campaigns/${uid}/cancel`, { reason })
    list.value = list.value.map((x) => (x.uid === c.uid ? c : x))
    return c
  }

  return { list, fetchForRepo, get, fetchAreas, create, launch, cancel }
})
