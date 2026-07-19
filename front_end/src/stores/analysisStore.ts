import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiGet, apiPost } from '@/services/api'

// ── DTOs (mirror back_end/domains/analysis/schemas.py) ──────────────────────

export type AnalysisStatus = 'in_progress' | 'complete' | 'superseded' | 'archived'
export type QuestionStatus = 'open' | 'answered' | 'dismissed'

export interface ScorecardEntry {
  dimension: string
  score: number | null
  max: number
  grade: string
  rationale: string
}

export interface CoverageEntry {
  area: string
  paths: string[]
  status: string // examined | partial | skipped
  note: string
}

export interface StrengthEntry {
  title: string
  detail: string
  paths: string[]
}

export interface ValidationEntry {
  check: string
  command: string
  result: string
  details: string
}

export interface AnalysisQuestion {
  uid: string
  question: string
  why_it_matters: string
  category: string
  status: QuestionStatus
  answer: string
  answered_by: string
  answered_at: string | null
}

export interface AnalysisDTO {
  uid: string
  repository_uid: string
  source_run_uid: string
  revision: string
  title: string
  status: AnalysisStatus
  supersedes: string
  superseded_by: string
  executor: string
  health_grade: string
  health_score: number | null
  scorecard: ScorecardEntry[]
  confidence: string
  limitations: string
  stats: Record<string, unknown>
  sections: Record<string, string>
  coverage: CoverageEntry[]
  strengths: StrengthEntry[]
  validation_baseline: ValidationEntry[]
  questions: AnalysisQuestion[]
  finding_count: number
  findings_by_severity: Record<string, number>
  open_question_count: number
  created_at: string | null
  updated_at: string | null
  completed_at: string | null
}

export const useAnalysisStore = defineStore('analysis', () => {
  const list = ref<AnalysisDTO[]>([])

  async function fetchForRepo(
    repoUid: string,
    opts: { status?: string; includeSuperseded?: boolean } = {},
  ): Promise<AnalysisDTO[]> {
    const qs = new URLSearchParams({ repository_uid: repoUid })
    if (opts.status) qs.set('status', opts.status)
    if (opts.includeSuperseded === false) qs.set('include_superseded', 'false')
    const data = await apiGet<AnalysisDTO[]>(`/analyses?${qs.toString()}`)
    list.value = data
    return data
  }

  async function get(uid: string): Promise<AnalysisDTO> {
    return apiGet<AnalysisDTO>(`/analyses/${uid}`)
  }

  /** Newest non-superseded Analysis for a repo (Health's "current" report). */
  async function latestForRepo(repoUid: string): Promise<AnalysisDTO | null> {
    return apiGet<AnalysisDTO | null>(`/analyses/latest?repository_uid=${encodeURIComponent(repoUid)}`)
  }

  async function answerQuestion(uid: string, qid: string, answer: string): Promise<AnalysisDTO> {
    return apiPost<AnalysisDTO>(`/analyses/${uid}/questions/${qid}/answer`, { answer })
  }

  async function dismissQuestion(uid: string, qid: string): Promise<AnalysisDTO> {
    return apiPost<AnalysisDTO>(`/analyses/${uid}/questions/${qid}/dismiss`)
  }

  /** Dispatch a fresh deep-scan that ingests the answered questions; the new
   *  run's Analysis supersedes this one. 409 if a deep scan is already running. */
  async function refineWithAnswers(
    uid: string,
  ): Promise<{ analysis_uid: string; run_uid: string; supersedes: string }> {
    return apiPost(`/analyses/${uid}/refine`)
  }

  return { list, fetchForRepo, get, latestForRepo, answerQuestion, dismissQuestion, refineWithAnswers }
})
