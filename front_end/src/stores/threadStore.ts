import { defineStore } from 'pinia'
import { apiGet, apiPatch, apiPost } from '@/services/api'
import type { ThreadDTO, ThreadDetailDTO } from '@/types/api'

export const useThreadStore = defineStore('threads', () => {
  async function createThread(ticketUid: string): Promise<ThreadDTO> {
    return apiPost<ThreadDTO>('/threads', { ticket_uid: ticketUid })
  }

  async function getThread(uid: string): Promise<ThreadDetailDTO> {
    return apiGet<ThreadDetailDTO>(`/threads/${uid}`)
  }

  async function listThreads(
    opts: {
      repository_uid?: string
      subject_ticket_uid?: string
    } = {},
  ): Promise<ThreadDTO[]> {
    const qs = new URLSearchParams()
    Object.entries(opts).forEach(([k, v]) => {
      if (v) qs.set(k, String(v))
    })
    const url = qs.toString() ? `/threads?${qs.toString()}` : '/threads'
    return apiGet<ThreadDTO[]>(url)
  }

  async function updatePlan(uid: string, planText: string): Promise<ThreadDTO> {
    return apiPatch<ThreadDTO>(`/threads/${uid}/plan`, { plan_text: planText })
  }

  async function approvePlan(uid: string): Promise<ThreadDTO> {
    return apiPost<ThreadDTO>(`/threads/${uid}/plan/approve`)
  }

  async function implement(uid: string): Promise<{ run_uid: string; thread_uid: string }> {
    return apiPost<{ run_uid: string; thread_uid: string }>(`/threads/${uid}/implement`)
  }

  async function abandon(uid: string): Promise<ThreadDTO> {
    return apiPost<ThreadDTO>(`/threads/${uid}/abandon`)
  }

  async function requestReview(uid: string): Promise<ThreadDTO> {
    return apiPost<ThreadDTO>(`/threads/${uid}/request-review`)
  }

  async function continueQuestions(uid: string): Promise<ThreadDetailDTO> {
    return apiPost<ThreadDetailDTO>(`/threads/${uid}/questions/continue`)
  }

  async function answerQuestion(
    uid: string,
    questionUid: string,
    answer: string,
  ): Promise<ThreadDetailDTO> {
    return apiPost<ThreadDetailDTO>(`/threads/${uid}/questions/${questionUid}/answer`, { answer })
  }

  return {
    createThread,
    getThread,
    listThreads,
    updatePlan,
    approvePlan,
    implement,
    abandon,
    requestReview,
    answerQuestion,
    continueQuestions,
  }
})
