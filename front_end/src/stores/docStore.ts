import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiDelete, apiGet, apiPost, apiPut } from '@/services/api'
import type {
  CreateDocRequest,
  DocDTO,
  DocEditDTO,
  DocEditStatus,
  ScopeFreshnessDTO,
  UpdateDocRequest,
} from '@/types/api'

/** Response of POST /repositories/{uid}/sweep/generate-docs. */
export interface GenerateDocsResult {
  repository_uid: string
  run_uid: string
  errors: string[]
  summary: string
}

/** Response of POST /repositories/{uid}/docs/export (409 raises ApiError). */
export interface DocsExportResult {
  status: string
  pages: number
  removed: number
  pull_request_uid: string
  pr_number: number
  pr_url: string
}

/** Response of POST /repositories/{uid}/sweep/audit. */
export interface DocAuditResult {
  repository_uid: string
  doc_count: number
  runs_dispatched: string[]
  skipped_docs: string[]
  errors: string[]
  summary: string
  /** Auto-selection provenance: why each page was picked. */
  selected: { doc_uid: string; slug: string; reason: string }[]
}

/** Response of POST /repositories/{uid}/sweep/deep-scan. */
export interface DeepScanResult {
  repository_uid: string
  run_uid: string
  errors: string[]
  summary: string
}

export interface SweepEstimate {
  docs: number
  generate_docs_runs: number
  audit_runs_if_all_selected: number
  note: string
}

export interface BulkEditResult {
  accepted?: number
  rejected?: number
  errors: string[]
}

export const useDocStore = defineStore('docs', () => {
  const list = ref<DocDTO[]>([])
  const edits = ref<DocEditDTO[]>([])

  async function fetchAll(opts: { repository_uid?: string } = {}): Promise<DocDTO[]> {
    const qs = new URLSearchParams()
    if (opts.repository_uid) qs.set('repository_uid', opts.repository_uid)
    const url = qs.toString() ? `/docs?${qs.toString()}` : '/docs'
    const data = await apiGet<DocDTO[]>(url)
    list.value = data
    return data
  }

  async function get(uid: string): Promise<DocDTO> {
    return apiGet<DocDTO>(`/docs/${uid}`)
  }

  async function create(req: CreateDocRequest): Promise<DocDTO> {
    const doc = await apiPost<DocDTO>('/docs', req)
    list.value = [...list.value, doc]
    return doc
  }

  async function update(uid: string, req: UpdateDocRequest & { body?: string }): Promise<DocDTO> {
    const doc = await apiPut<DocDTO>(`/docs/${uid}`, req)
    list.value = list.value.map((d) => (d.uid === uid ? doc : d))
    return doc
  }

  async function remove(uid: string): Promise<void> {
    await apiDelete(`/docs/${uid}`)
    list.value = list.value.filter((d) => d.uid !== uid)
  }

  async function setPinned(uid: string, pinned: boolean): Promise<DocDTO> {
    const doc = await apiPost<DocDTO>(`/docs/${uid}/pin`, { pinned })
    list.value = list.value.map((d) => (d.uid === uid ? doc : d))
    return doc
  }

  /** LLM drafts the page body from its watch_paths; lands as a pending DocEdit. */
  async function draft(uid: string): Promise<{ run_uid: string }> {
    return apiPost<{ run_uid: string }>(`/docs/${uid}/draft`)
  }

  /** LLM verifies the page's claims against the code; files findings. 409 when the body is empty. */
  async function verify(uid: string): Promise<{ run_uid: string }> {
    return apiPost<{ run_uid: string }>(`/docs/${uid}/verify`)
  }

  /** One LLM run that proposes doc pages for the repository (409 when already running). */
  /** Destructive: deletes every doc page + edit for the repo. */
  async function resetAll(repoUid: string): Promise<{ docs_deleted: number; edits_deleted: number }> {
    const result = await apiPost<{ docs_deleted: number; edits_deleted: number }>(
      `/repositories/${repoUid}/docs/reset`,
    )
    return result
  }

  async function generate(repoUid: string, agent_uid?: string): Promise<GenerateDocsResult> {
    return apiPost<GenerateDocsResult>(`/repositories/${repoUid}/sweep/generate-docs`, {
      agent_uid,
    })
  }

  /** Syncs docs to the repo as an AGENTS.md + docs/** PR. */
  async function exportToRepo(repoUid: string): Promise<DocsExportResult> {
    return apiPost<DocsExportResult>(`/repositories/${repoUid}/docs/export`)
  }

  /** Audit runs against selected doc pages (empty = whole repository).
   *  auto_select picks the stalest / never-checked pages instead (mutually
   *  exclusive with docUids); max_findings caps each dispatched run. */
  async function audit(
    repoUid: string,
    docUids: string[],
    options: {
      agent_uid?: string
      custom_intent?: string
      auto_select?: boolean
      limit?: number
      max_findings?: number
      effort?: 'short' | 'normal' | 'deep' | 'unlimited'
    } = {},
  ): Promise<DocAuditResult> {
    return apiPost<DocAuditResult>(`/repositories/${repoUid}/sweep/audit`, {
      doc_uids: docUids,
      agent_uid: options.agent_uid,
      custom_intent: options.custom_intent,
      auto_select: options.auto_select ?? false,
      limit: options.limit ?? 3,
      max_findings: options.max_findings,
      effort: options.effort ?? 'normal',
    })
  }

  /** One long whole-repository deep-scan run (plan → sweep → synthesize).
   *  409 when a deep scan is already running for the repo. */
  async function deepScan(
    repoUid: string,
    options: {
      agent_uid?: string
      custom_intent?: string
      max_findings?: number
      effort?: 'short' | 'normal' | 'deep' | 'unlimited'
    } = {},
  ): Promise<DeepScanResult> {
    return apiPost<DeepScanResult>(`/repositories/${repoUid}/sweep/deep-scan`, {
      agent_uid: options.agent_uid,
      custom_intent: options.custom_intent,
      max_findings: options.max_findings,
      effort: options.effort ?? 'deep',
    })
  }

  async function sweepEstimate(repoUid: string): Promise<SweepEstimate> {
    return apiGet<SweepEstimate>(`/repositories/${repoUid}/sweep/estimate`)
  }

  /** Checked stamps rolled up per doc — "has this page been looked at since the code changed?" */
  async function fetchFreshness(repoUid: string): Promise<ScopeFreshnessDTO[]> {
    return apiGet<ScopeFreshnessDTO[]>(`/repositories/${repoUid}/freshness`)
  }

  // ── DocEdits (agent-proposed changes; pending → accepted | rejected) ──────

  async function fetchEdits(opts: { repository_uid?: string; status?: DocEditStatus } = {}): Promise<DocEditDTO[]> {
    const qs = new URLSearchParams()
    if (opts.repository_uid) qs.set('repository_uid', opts.repository_uid)
    if (opts.status) qs.set('status', opts.status)
    const url = qs.toString() ? `/doc-edits?${qs.toString()}` : '/doc-edits'
    const data = await apiGet<DocEditDTO[]>(url)
    edits.value = data
    return data
  }

  /** Applies the proposed_body to the Doc; returns the updated Doc. */
  async function acceptEdit(uid: string): Promise<DocDTO> {
    const doc = await apiPost<DocDTO>(`/doc-edits/${uid}/accept`)
    edits.value = edits.value.filter((e) => e.uid !== uid)
    list.value = list.value.some((d) => d.uid === doc.uid)
      ? list.value.map((d) => (d.uid === doc.uid ? doc : d))
      : [...list.value, doc]
    return doc
  }

  async function rejectEdit(uid: string): Promise<DocEditDTO> {
    const edit = await apiPost<DocEditDTO>(`/doc-edits/${uid}/reject`)
    edits.value = edits.value.filter((e) => e.uid !== uid)
    return edit
  }

  async function bulkAccept(uids: string[]): Promise<BulkEditResult> {
    const result = await apiPost<BulkEditResult>('/doc-edits/bulk-accept', { uids })
    const done = new Set(uids)
    edits.value = edits.value.filter((e) => !done.has(e.uid))
    return result
  }

  async function bulkReject(uids: string[]): Promise<BulkEditResult> {
    const result = await apiPost<BulkEditResult>('/doc-edits/bulk-reject', { uids })
    const done = new Set(uids)
    edits.value = edits.value.filter((e) => !done.has(e.uid))
    return result
  }

  return {
    list,
    edits,
    fetchAll,
    get,
    create,
    update,
    remove,
    setPinned,
    draft,
    verify,
    generate,
    resetAll,
    exportToRepo,
    audit,
    deepScan,
    sweepEstimate,
    fetchFreshness,
    fetchEdits,
    acceptEdit,
    rejectEdit,
    bulkAccept,
    bulkReject,
  }
})
