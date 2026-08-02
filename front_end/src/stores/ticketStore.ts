import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiDelete, apiGet, apiPatch, apiPost } from '@/services/api'
import type {
  PlanEpicsRequest,
  PlanEpicsPreview,
  PlanEpicsResult,
  BulkApproveResult,
  BulkTicketResult,
  CreateTicketRequest,
  EpicProposalStatus,
  GroupTicketsRequest,
  ImplementRunDispatch,
  SuggestEpicsDispatch,
  SuggestEpicsRequest,
  PullRequestDTO,
  TicketDTO,
  TicketDetailDTO,
  EpicProposalDTO,
  TicketOrigin,
  TicketStatus,
  UpdateTicketRequest,
} from '@/types/api'

export const useTicketStore = defineStore('tickets', () => {
  const tickets = ref<TicketDTO[]>([])

  function upsert(ticket: TicketDTO) {
    tickets.value = tickets.value.some((x) => x.uid === ticket.uid)
      ? tickets.value.map((x) => (x.uid === ticket.uid ? ticket : x))
      : [...tickets.value, ticket]
  }

  // ── Tickets ─────────────────────────────────────────────────────────────────

  interface TicketFilters {
    repository_uid?: string
    status?: TicketStatus
    origin?: TicketOrigin
    parent_ticket_uid?: string
    /** true = archived tickets only; omitted/false = active only (the
     *  querystring builder skips falsy values, which matches the backend
     *  default — there is no "both" mode). */
    archived?: boolean
  }

  /** Side-effect-free list — safe to call in parallel (e.g. link dialogs). */
  async function listTickets(opts: TicketFilters = {}): Promise<TicketDTO[]> {
    const qs = new URLSearchParams()
    Object.entries(opts).forEach(([k, v]) => {
      if (v) qs.set(k, String(v))
    })
    const url = qs.toString() ? `/tickets?${qs.toString()}` : '/tickets'
    return apiGet<TicketDTO[]>(url)
  }

  /** List + replace the store's ticket set (board view). */
  async function fetchTickets(opts: TicketFilters = {}): Promise<TicketDTO[]> {
    const data = await listTickets(opts)
    tickets.value = data
    return data
  }

  async function getTicket(uid: string): Promise<TicketDetailDTO> {
    return apiGet<TicketDetailDTO>(`/tickets/${uid}`)
  }

  async function createTicket(req: CreateTicketRequest): Promise<TicketDTO> {
    const ticket = await apiPost<TicketDTO>('/tickets', req)
    upsert(ticket)
    return ticket
  }

  async function updateTicket(uid: string, req: UpdateTicketRequest): Promise<TicketDTO> {
    const ticket = await apiPatch<TicketDTO>(`/tickets/${uid}`, req)
    upsert(ticket)
    return ticket
  }

  /** Status machine transition — 409 on illegal transitions. backlog→todo is Gate 1. */
  async function setStatus(uid: string, status: TicketStatus): Promise<TicketDTO> {
    const ticket = await apiPost<TicketDTO>(`/tickets/${uid}/status`, { status })
    upsert(ticket)
    return ticket
  }

  async function linkFinding(uid: string, findingUid: string): Promise<TicketDTO> {
    const ticket = await apiPost<TicketDTO>(`/tickets/${uid}/link-finding`, { finding_uid: findingUid })
    upsert(ticket)
    return ticket
  }

  async function linkPullRequest(uid: string, pullRequestUid: string): Promise<TicketDTO> {
    const ticket = await apiPost<TicketDTO>(`/tickets/${uid}/link-pr`, { pull_request_uid: pullRequestUid })
    upsert(ticket)
    return ticket
  }

  /** Write path: dispatch an implement-run — 409 when not todo/in-progress,
   *  an open PR already implements the ticket, or no provider is available. */
  async function implementTicket(uid: string): Promise<ImplementRunDispatch> {
    return apiPost<ImplementRunDispatch>(`/tickets/${uid}/implement`)
  }

  /** Move N tickets at once — Gate 1 for a whole selection.
   *  Partial success is REPORTED, never thrown: one archived ticket must not
   *  decide the fate of the other twenty-nine. */
  async function bulkSetStatus(
    uids: string[],
    status: TicketStatus,
  ): Promise<BulkTicketResult> {
    return apiPost<BulkTicketResult>('/tickets/bulk-status', { uids, status })
  }

  /** Dispatch implement runs for N approved tickets. A selection larger than
   *  the provider's concurrency headroom lands the first few and reports the
   *  rest in `errors` — it does not stampede the provider. */
  async function bulkImplement(uids: string[]): Promise<BulkTicketResult> {
    return apiPost<BulkTicketResult>('/tickets/bulk-implement', { uids })
  }

  async function bulkArchive(uids: string[]): Promise<BulkTicketResult> {
    return apiPost<BulkTicketResult>('/tickets/bulk-archive', { uids })
  }

  /** Read-only refine-run: sharpen the ticket's title/description/acceptance
   *  criteria and attach an implementation plan via the platform tools. */
  async function refineTicket(uid: string): Promise<ImplementRunDispatch> {
    return apiPost<ImplementRunDispatch>(`/tickets/${uid}/refine`)
  }

  /** Backlog-only delete. */
  async function deleteTicket(uid: string): Promise<void> {
    await apiDelete(`/tickets/${uid}`)
    tickets.value = tickets.value.filter((t) => t.uid !== uid)
  }

  /** Reversible "leave the board" from any status — 409 while a thread is
   *  active, for epic members, and for epics with unfinished members. The
   *  board list excludes archived tickets, so drop it from the store. */
  async function archiveTicket(uid: string): Promise<TicketDTO> {
    const ticket = await apiPost<TicketDTO>(`/tickets/${uid}/archive`)
    tickets.value = tickets.value.filter((t) => t.uid !== uid)
    return ticket
  }

  async function unarchiveTicket(uid: string): Promise<TicketDTO> {
    return apiPost<TicketDTO>(`/tickets/${uid}/unarchive`)
  }

  // ── Grouping (batch related tickets under one parent) ───────────────────────

  /** Group ≥2 tickets under a new parent ticket (created in Backlog). */
  async function createEpic(req: GroupTicketsRequest): Promise<TicketDTO> {
    const parent = await apiPost<TicketDTO>('/tickets/epics', req)
    upsert(parent)
    return parent
  }

  /** Dissolve a group: detach every subticket from this parent. */
  async function dissolveEpic(uid: string): Promise<{ ticket_uid: string; detached: number }> {
    return apiPost<{ ticket_uid: string; detached: number }>(`/tickets/${uid}/dissolve-epic`)
  }

  /** Detach a single ticket from its parent group. */
  async function removeFromEpic(uid: string): Promise<TicketDTO> {
    const ticket = await apiPost<TicketDTO>(`/tickets/${uid}/remove-from-epic`)
    upsert(ticket)
    return ticket
  }

  /** Dispatch a read-only run that proposes ticket groupings — every
   *  proposal is human-approved before anything changes. 409 when fewer
   *  than 2 ungrouped work items match the request's filters. */
  async function suggestEpics(req: SuggestEpicsRequest): Promise<SuggestEpicsDispatch> {
    return apiPost<SuggestEpicsDispatch>('/tickets/suggest-epics', req)
  }

  async function listEpicProposals(opts: {
    repository_uid?: string
    status?: EpicProposalStatus
  } = {}): Promise<EpicProposalDTO[]> {
    const qs = new URLSearchParams()
    Object.entries(opts).forEach(([k, v]) => {
      if (v) qs.set(k, String(v))
    })
    const url = qs.toString() ? `/epic-proposals?${qs.toString()}` : '/epic-proposals'
    return apiGet<EpicProposalDTO[]>(url)
  }

  /** Approve: creates the parent ticket and re-parents the members. */
  async function approveEpic(uid: string): Promise<EpicProposalDTO> {
    return apiPost<EpicProposalDTO>(`/epic-proposals/${uid}/approve`)
  }

  async function rejectEpic(uid: string): Promise<EpicProposalDTO> {
    return apiPost<EpicProposalDTO>(`/epic-proposals/${uid}/reject`)
  }

  /**
   * Approve a whole plan of batches as one review action. Partial success is
   * reported rather than thrown — a stale batch must not stop its siblings
   * from landing — so callers MUST read `errors`, not assume every uid
   * succeeded.
   */
  async function bulkApproveEpics(uids: string[]): Promise<BulkApproveResult> {
    return apiPost<BulkApproveResult>('/epic-proposals/bulk-approve', { uids })
  }

  /**
   * Build batches from a rule on a COMPUTED axis — no agent, no run, returns
   * the plan immediately and PERSISTS it as reviewable proposals.
   */
  async function planEpics(
    req: Omit<PlanEpicsRequest, 'dry_run'>,
  ): Promise<PlanEpicsResult> {
    return apiPost<PlanEpicsResult>('/tickets/plan-epics', { ...req, dry_run: false })
  }

  /**
   * Preview the identical plan without persisting anything, so the counts
   * shown before approval come from the code that builds them rather than an
   * estimate. Returns unpersisted drafts — they have no `uid` yet, which is
   * why this is a separate method rather than a `dry_run` flag callers can
   * forget they set.
   */
  async function previewEpics(
    req: Omit<PlanEpicsRequest, 'dry_run'>,
  ): Promise<PlanEpicsPreview> {
    return apiPost<PlanEpicsPreview>('/tickets/plan-epics', { ...req, dry_run: true })
  }

  // ── PR side of the link (delivery surface) ──────────────────────────────────

  async function linkTicketToPullRequest(pullRequestUid: string, ticketUid: string): Promise<PullRequestDTO> {
    return apiPost<PullRequestDTO>(`/delivery/pull-requests/${pullRequestUid}/link-ticket`, {
      ticket_uid: ticketUid,
    })
  }

  return {
    tickets,
    listTickets,
    fetchTickets,
    getTicket,
    createTicket,
    updateTicket,
    setStatus,
    linkFinding,
    linkPullRequest,
    implementTicket,
    bulkSetStatus,
    bulkImplement,
    bulkArchive,
    refineTicket,
    deleteTicket,
    archiveTicket,
    unarchiveTicket,
    createEpic,
    dissolveEpic,
    removeFromEpic,
    suggestEpics,
    planEpics,
    previewEpics,
    listEpicProposals,
    approveEpic,
    bulkApproveEpics,
    rejectEpic,
    linkTicketToPullRequest,
  }
})
