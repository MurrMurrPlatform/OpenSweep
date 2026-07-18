import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiGet, apiPost, apiPut } from '@/services/api'
import type {
  BlockingOverrideValue,
  ConvergenceState,
  FindingResolutionDTO,
  FixRunDispatch,
  MergePolicyDTO,
  PRState,
  PullRequestDTO,
  ReviewRunDispatch,
  SubmitVerdictRequest,
  TriggerReviewRequest,
  UpdateMergePolicyRequest,
  VerdictDTO,
} from '@/types/api'

export const useDeliveryStore = defineStore('delivery', () => {
  const pullRequests = ref<PullRequestDTO[]>([])

  function upsertPr(pr: PullRequestDTO) {
    pullRequests.value = pullRequests.value.some((x) => x.uid === pr.uid)
      ? pullRequests.value.map((x) => (x.uid === pr.uid ? pr : x))
      : [...pullRequests.value, pr]
  }

  // ── Pull requests ──────────────────────────────────────────────────────────

  async function fetchPullRequests(opts: {
    repository_uid?: string
    state?: PRState
  } = {}): Promise<PullRequestDTO[]> {
    const qs = new URLSearchParams()
    Object.entries(opts).forEach(([k, v]) => {
      if (v) qs.set(k, String(v))
    })
    const url = qs.toString() ? `/delivery/pull-requests?${qs.toString()}` : '/delivery/pull-requests'
    const data = await apiGet<PullRequestDTO[]>(url)
    pullRequests.value = data
    return data
  }

  async function getPullRequest(uid: string): Promise<PullRequestDTO> {
    return apiGet<PullRequestDTO>(`/delivery/pull-requests/${uid}`)
  }

  /** Manual head-driven resync — same path the webhook takes. */
  async function syncPullRequest(repositoryUid: string, githubNumber: number): Promise<PullRequestDTO> {
    const qs = new URLSearchParams({
      repository_uid: repositoryUid,
      github_number: String(githubNumber),
    })
    const pr = await apiPost<PullRequestDTO>(`/delivery/pull-requests/sync?${qs.toString()}`)
    upsertPr(pr)
    return pr
  }

  /** Full 2-way reconcile with GitHub: imports PRs opened outside OpenSweep,
   *  closes out externally merged/closed ones. */
  async function syncRepository(repositoryUid: string): Promise<{ synced: number; closed: number }> {
    const qs = new URLSearchParams({ repository_uid: repositoryUid })
    return apiPost<{ synced: number; closed: number }>(
      `/delivery/pull-requests/sync-repo?${qs.toString()}`,
    )
  }

  async function recompute(uid: string): Promise<ConvergenceState> {
    return apiPost<ConvergenceState>(`/delivery/pull-requests/${uid}/recompute`)
  }

  /** Dispatch a read-only review run that ends with a SHA-bound verdict.
   *  Options: depth (quick/normal/deep), full (skip incremental scoping),
   *  max_findings (numeric budget). */
  async function triggerReview(
    uid: string,
    opts: TriggerReviewRequest = {},
  ): Promise<ReviewRunDispatch> {
    return apiPost<ReviewRunDispatch>(`/delivery/pull-requests/${uid}/review`, opts)
  }

  async function getLatestVerdict(uid: string): Promise<VerdictDTO | null> {
    return apiGet<VerdictDTO | null>(`/delivery/pull-requests/${uid}/verdict`)
  }

  /** Maintainer records/overrides a SHA-bound verdict by hand. */
  async function submitVerdict(uid: string, req: SubmitVerdictRequest): Promise<VerdictDTO> {
    return apiPost<VerdictDTO>(`/delivery/pull-requests/${uid}/verdicts`, req)
  }

  /** Reset the bounded auto-fix loop counter so a maintainer can grant another
   *  round of fix runs after 'human required'. */
  async function resetFixRounds(uid: string): Promise<PullRequestDTO> {
    const pr = await apiPost<PullRequestDTO>(`/delivery/pull-requests/${uid}/reset-fix-rounds`)
    upsertPr(pr)
    return pr
  }

  /** Write path: dispatch a fix run on the PR's head branch. Bounded by
   *  MergePolicy.max_fix_rounds — 409 when exhausted or the PR isn't open. */
  async function triggerFix(uid: string, findingUids?: string[]): Promise<FixRunDispatch> {
    return apiPost<FixRunDispatch>(
      `/delivery/pull-requests/${uid}/fix`,
      findingUids && findingUids.length ? { finding_uids: findingUids } : {},
    )
  }

  // ── Resolutions (the per-PR findings ledger) ──────────────────────────────

  async function fetchResolutions(pullRequestUid: string): Promise<FindingResolutionDTO[]> {
    return apiGet<FindingResolutionDTO[]>(`/delivery/pull-requests/${pullRequestUid}/resolutions`)
  }

  async function attachFix(uid: string, sha: string): Promise<FindingResolutionDTO> {
    return apiPost<FindingResolutionDTO>(`/delivery/resolutions/${uid}/attach-fix`, { sha })
  }

  async function verifyResolution(uid: string, sha: string): Promise<FindingResolutionDTO> {
    return apiPost<FindingResolutionDTO>(`/delivery/resolutions/${uid}/verify`, { sha })
  }

  async function waiveResolution(uid: string, reason: string): Promise<FindingResolutionDTO> {
    return apiPost<FindingResolutionDTO>(`/delivery/resolutions/${uid}/waive`, { reason })
  }

  async function deferResolution(uid: string): Promise<FindingResolutionDTO> {
    return apiPost<FindingResolutionDTO>(`/delivery/resolutions/${uid}/defer`)
  }

  async function reopenResolution(uid: string): Promise<FindingResolutionDTO> {
    return apiPost<FindingResolutionDTO>(`/delivery/resolutions/${uid}/reopen`)
  }

  async function setBlockingOverride(
    uid: string,
    override: BlockingOverrideValue,
    reason: string,
  ): Promise<FindingResolutionDTO> {
    return apiPost<FindingResolutionDTO>(`/delivery/resolutions/${uid}/blocking-override`, {
      override,
      reason,
    })
  }

  /** Adopt an externally-opened PR onto the board: create + link its ticket.
   *  mode `ai` additionally dispatches a refine run that drafts the ticket
   *  content from the PR's diff. */
  async function createTicketForPr(
    prUid: string,
    mode: 'manual' | 'ai',
  ): Promise<{ ticket_uid: string; run_uid: string }> {
    return apiPost<{ ticket_uid: string; run_uid: string }>(
      `/delivery/pull-requests/${prUid}/create-ticket`,
      { mode },
    )
  }

  // ── Merge policy ───────────────────────────────────────────────────────────

  async function getMergePolicy(repositoryUid: string): Promise<MergePolicyDTO> {
    return apiGet<MergePolicyDTO>(`/delivery/repositories/${repositoryUid}/merge-policy`)
  }

  async function updateMergePolicy(
    repositoryUid: string,
    req: UpdateMergePolicyRequest,
  ): Promise<MergePolicyDTO> {
    return apiPut<MergePolicyDTO>(`/delivery/repositories/${repositoryUid}/merge-policy`, req)
  }

  return {
    pullRequests,
    fetchPullRequests,
    getPullRequest,
    syncPullRequest,
    syncRepository,
    recompute,
    triggerReview,
    getLatestVerdict,
    submitVerdict,
    resetFixRounds,
    triggerFix,
    fetchResolutions,
    attachFix,
    verifyResolution,
    waiveResolution,
    deferResolution,
    reopenResolution,
    setBlockingOverride,
    createTicketForPr,
    getMergePolicy,
    updateMergePolicy,
  }
})
