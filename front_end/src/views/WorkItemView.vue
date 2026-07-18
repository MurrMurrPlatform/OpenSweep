<script setup lang="ts">
/**
 * Unified work-item view — ONE page with ONE header for the ticket, its
 * thread and its pull request, which are the same piece of work.
 *
 * All three legacy routes (/tickets/:uid, /threads/:uid, /pull-requests/:uid)
 * render this view; the route decides the focused tab and the uid kind, and
 * the sibling uids are resolved from whichever anchor we landed on. Deep
 * links keep working and tab switches rewrite the URL (router.replace) so
 * what you share matches what you see.
 *
 * The header (title, state chips, tabs) lives HERE; the embedded views only
 * render their content + one horizontal action menu. They announce state
 * changes by dispatching a `workitem:changed` window event, which refreshes
 * the header.
 *
 * A PR can exist WITHOUT a ticket (hand-made branch pushed to GitHub and
 * synced in) — then only the PR tab is enabled and the page says so.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import {
  CheckCircle2,
  GitPullRequest,
  MessagesSquare,
  Pencil,
  Plus,
  Search,
  Sparkles,
  SquareKanban,
} from 'lucide-vue-next'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import CiStateBadge from '@/components/delivery/CiStateBadge.vue'
import TicketOriginBadge from '@/components/tickets/TicketOriginBadge.vue'
import TicketDetailView from '@/views/TicketDetailView.vue'
import ThreadView from '@/views/ThreadView.vue'
import PullRequestDetailView from '@/views/PullRequestDetailView.vue'
import { useDeliveryStore } from '@/stores/deliveryStore'
import { useThreadStore } from '@/stores/threadStore'
import { useTicketStore } from '@/stores/ticketStore'
import { useRepositoryStore } from '@/stores/repositoryStore'
import { STATUS_LABELS, priorityVariant, statusVariant } from '@/components/tickets/ticketMeta'
import type { PullRequestDTO, ThreadDTO, TicketDTO } from '@/types/api'

type Kind = 'ticket' | 'thread' | 'pr'

const route = useRoute()
const router = useRouter()
const tickets = useTicketStore()
const threads = useThreadStore()
const delivery = useDeliveryStore()
const repos = useRepositoryStore()
const toast = useToast()

const KIND_BY_ROUTE: Record<string, Kind> = {
  'ticket-detail': 'ticket',
  'thread-detail': 'thread',
  'pull-request-detail': 'pr',
}

const kind = computed<Kind>(() => KIND_BY_ROUTE[String(route.name)] ?? 'ticket')
const uid = computed(() => String(route.params.uid))

const ticket = ref<TicketDTO | null>(null)
const thread = ref<ThreadDTO | null>(null)
const pr = ref<PullRequestDTO | null>(null)
const resolving = ref(true)

const THREAD_PHASE_LABELS: Record<string, string> = {
  refining: 'Planning',
  implementing: 'Implementing',
  in_review: 'In review',
  done: 'Done',
  abandoned: 'Abandoned',
}

function pickThread(list: ThreadDTO[]): ThreadDTO | null {
  const active = list.find((t) => t.phase !== 'done' && t.phase !== 'abandoned')
  if (active) return active
  // Terminal threads still hold the conversation history — show the latest.
  return [...list].sort((a, b) => ((a.created_at ?? '') < (b.created_at ?? '') ? 1 : -1))[0] ?? null
}

async function threadForTicket(tUid: string): Promise<ThreadDTO | null> {
  try {
    return pickThread(await threads.listThreads({ subject_ticket_uid: tUid }))
  } catch {
    return null
  }
}

async function fetchTicket(tUid: string): Promise<TicketDTO | null> {
  try {
    const { children: _children, ...fields } = await tickets.getTicket(tUid)
    return fields as TicketDTO
  } catch {
    return null
  }
}

async function fetchPr(pUid: string): Promise<PullRequestDTO | null> {
  try {
    return await delivery.getPullRequest(pUid)
  } catch {
    return null
  }
}

/** Resolve the ticket/thread/PR cluster from the route's anchor uid. */
async function resolve() {
  resolving.value = true
  const anchor = uid.value
  let t: TicketDTO | null = null
  let th: ThreadDTO | null = null
  let p: PullRequestDTO | null = null
  try {
    if (kind.value === 'ticket') {
      ;[t, th] = await Promise.all([fetchTicket(anchor), threadForTicket(anchor)])
      const prUid = th?.pr_uid || t?.linked_pr_uids?.[t.linked_pr_uids.length - 1] || ''
      if (prUid) p = await fetchPr(prUid)
    } else if (kind.value === 'thread') {
      try {
        th = await threads.getThread(anchor)
      } catch {
        th = null
      }
      ;[t, p] = await Promise.all([
        th?.subject_ticket_uid ? fetchTicket(th.subject_ticket_uid) : Promise.resolve(null),
        th?.pr_uid ? fetchPr(th.pr_uid) : Promise.resolve(null),
      ])
    } else {
      p = await fetchPr(anchor)
      if (p?.ticket_uid) {
        ;[t, th] = await Promise.all([fetchTicket(p.ticket_uid), threadForTicket(p.ticket_uid)])
      }
    }
    if (!repos.loaded) await repos.fetchAll().catch(() => undefined)
  } finally {
    // Route anchor may have changed mid-flight — only commit the latest.
    if (anchor === uid.value) {
      ticket.value = t
      thread.value = th
      pr.value = p
      resolving.value = false
    }
  }
}

watch([uid, kind], () => void resolve(), { immediate: true })

// Embedded views announce mutations (status moved, plan approved, PR opened)
// via a window event — the header refreshes without prop drilling.
let refreshTimer: ReturnType<typeof setTimeout> | undefined
function onChanged() {
  clearTimeout(refreshTimer)
  refreshTimer = setTimeout(() => void resolve(), 400)
}
onMounted(() => window.addEventListener('workitem:changed', onChanged))
onBeforeUnmount(() => {
  window.removeEventListener('workitem:changed', onChanged)
  clearTimeout(refreshTimer)
})

// ── Header ───────────────────────────────────────────────────────────────────

const title = computed(() => {
  if (ticket.value?.title) return ticket.value.title
  if (pr.value) return `#${pr.value.github_number} · ${pr.value.title || '(untitled)'}`
  if (thread.value) return 'Thread'
  return resolving.value ? '…' : 'Work item'
})

const repoName = computed(() => {
  const repoUid = ticket.value?.repository_uid || pr.value?.repository_uid || thread.value?.repository_uid
  return repoUid ? (repos.find(repoUid)?.name ?? '') : ''
})

const branchLine = computed(() => {
  if (pr.value) return `${pr.value.head_ref} → ${pr.value.base_ref}`
  return thread.value?.branch || ''
})

// ── Tabs ─────────────────────────────────────────────────────────────────────

interface Tab {
  kind: Kind
  label: string
  icon: typeof SquareKanban
  target: string | null
  clickable: boolean
  hint: string
}

/** A ticketless PR's Ticket tab is clickable anyway: it opens the create
 *  pane (adopt the PR onto the board), not a dead end. */
const showTicketCreate = ref(false)
watch([uid, kind], () => (showTicketCreate.value = false))

const tabs = computed<Tab[]>(() => [
  {
    kind: 'ticket',
    label: 'Ticket',
    icon: SquareKanban,
    target: ticket.value?.uid ?? null,
    clickable: Boolean(ticket.value || pr.value),
    hint: 'No ticket yet — click to create one for this pull request.',
  },
  {
    kind: 'thread',
    label: 'Thread',
    icon: MessagesSquare,
    target: thread.value?.uid ?? null,
    clickable: Boolean(thread.value),
    hint: 'No thread yet — use “Start thread” to begin the dev conversation.',
  },
  {
    kind: 'pr',
    label: 'Pull request',
    icon: GitPullRequest,
    target: pr.value?.uid ?? null,
    clickable: Boolean(pr.value),
    hint: 'No pull request yet — one opens when the thread finishes implementing.',
  },
])

const ROUTE_BY_KIND: Record<Kind, string> = {
  ticket: 'ticket-detail',
  thread: 'thread-detail',
  pr: 'pull-request-detail',
}

function selectTab(tab: Tab) {
  if (tab.target) {
    showTicketCreate.value = false
    if (tab.kind !== kind.value) {
      void router.replace({ name: ROUTE_BY_KIND[tab.kind], params: { uid: tab.target } })
    }
    return
  }
  if (tab.kind === 'ticket' && pr.value) showTicketCreate.value = true
}

/** Which facet the page is showing — the route's kind, unless the ticket
 *  create pane is open on a ticketless PR. */
const activeFacet = computed<Kind>(() => (showTicketCreate.value ? 'ticket' : kind.value))

// ── Adopt a ticketless PR onto the board ────────────────────────────────────

const creatingTicket = ref<'manual' | 'ai' | null>(null)

async function createTicket(mode: 'manual' | 'ai') {
  if (!pr.value || creatingTicket.value) return
  creatingTicket.value = mode
  try {
    const result = await delivery.createTicketForPr(pr.value.uid, mode)
    if (mode === 'ai') {
      toast.success(
        'Ticket created — AI is drafting it',
        `A refine run is reading the PR diff (run ${result.run_uid.slice(0, 8)}).`,
      )
    } else {
      toast.success('Ticket created', 'Prefilled from the PR — edit it to fill in the details.')
    }
    showTicketCreate.value = false
    void router.replace({ name: 'ticket-detail', params: { uid: result.ticket_uid } })
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t create ticket', msg)
  } finally {
    creatingTicket.value = null
  }
}

/** The prop each embedded view receives — its OWN uid, never the anchor's. */
const focusedUid = computed(() => {
  if (kind.value === 'ticket') return ticket.value?.uid ?? uid.value
  if (kind.value === 'thread') return thread.value?.uid ?? uid.value
  return pr.value?.uid ?? uid.value
})
</script>

<template>
  <div class="space-y-4">
    <!-- ── Unified header: one identity for ticket + thread + PR ─────────── -->
    <header class="space-y-2.5">
      <h1 class="text-xl font-semibold leading-snug tracking-tight sm:text-2xl">{{ title }}</h1>

      <div class="flex flex-wrap items-center gap-2">
        <template v-if="ticket">
          <Badge :variant="statusVariant(ticket.status)" class="px-1.5 text-[10px]">
            {{ STATUS_LABELS[ticket.status] }}
          </Badge>
          <Badge :variant="priorityVariant(ticket.priority)" class="px-1.5 text-[10px]">
            {{ ticket.priority }}
          </Badge>
          <TicketOriginBadge :origin="ticket.origin" />
        </template>
        <Badge v-if="thread" variant="info" class="px-1.5 text-[10px]">
          <MessagesSquare class="size-3" /> {{ THREAD_PHASE_LABELS[thread.phase] ?? thread.phase }}
        </Badge>
        <template v-if="pr">
          <Badge :variant="pr.state === 'merged' ? 'success' : pr.state === 'closed' ? 'secondary' : 'info'" class="px-1.5 text-[10px]">
            <GitPullRequest class="size-3" /> {{ pr.state }}<template v-if="pr.draft"> · draft</template>
          </Badge>
          <CiStateBadge :state="pr.ci_state" />
          <Badge v-if="pr.converged" variant="success" class="px-1.5 text-[10px]">
            <CheckCircle2 class="size-3" /> converged
          </Badge>
        </template>
        <RouterLink
          v-if="ticket?.origin_finding_uid"
          :to="{ name: 'finding-detail', params: { uid: ticket.origin_finding_uid } }"
          class="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        >
          <Search class="size-3" /> origin finding
        </RouterLink>
        <span class="text-xs text-muted-foreground">
          <template v-if="repoName">{{ repoName }}</template>
          <template v-if="branchLine"> · <span class="font-mono">{{ branchLine }}</span></template>
        </span>
      </div>

      <!-- Underline tabs, attached to a full-width rule -->
      <nav class="flex gap-0.5 border-b" aria-label="Work item facets">
        <button
          v-for="tab in tabs"
          :key="tab.kind"
          type="button"
          class="-mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm transition-colors"
          :class="
            tab.kind === activeFacet
              ? 'border-primary font-medium text-foreground'
              : tab.clickable
                ? 'border-transparent text-muted-foreground hover:border-border hover:text-foreground'
                : 'cursor-not-allowed border-transparent text-muted-foreground/40'
          "
          :disabled="!tab.clickable && !resolving"
          :title="tab.target ? undefined : tab.hint"
          @click="selectTab(tab)"
        >
          <component :is="tab.icon" class="size-3.5" /> {{ tab.label }}
          <Plus v-if="tab.kind === 'ticket' && !tab.target && tab.clickable" class="size-3 text-muted-foreground" />
        </button>
      </nav>
    </header>

    <p
      v-if="!resolving && kind === 'pr' && !ticket && !showTicketCreate"
      class="text-xs text-muted-foreground"
    >
      This pull request has no linked ticket — it was likely opened outside OpenSweep. Use the
      Ticket tab to create one, or keep reviewing it as-is.
    </p>

    <!-- ── Focused facet ─────────────────────────────────────────────────── -->
    <!-- Adopt pane: a ticketless PR's Ticket tab offers creation instead of
         a dead end. -->
    <Card v-if="activeFacet === 'ticket' && !ticket && pr">
      <CardContent class="flex flex-col items-center gap-3 p-10 text-center">
        <SquareKanban class="size-8 text-muted-foreground" />
        <h2 class="text-base font-semibold">No ticket for this pull request</h2>
        <p class="max-w-md text-sm text-muted-foreground">
          PR #{{ pr.github_number }} was probably created outside OpenSweep. Adopt it onto the
          board by creating its ticket — it will be born under review, linked to this PR.
        </p>
        <div class="flex items-center gap-2">
          <Button :loading="creatingTicket === 'ai'" :disabled="!!creatingTicket" @click="createTicket('ai')">
            <Sparkles /> Draft with AI from the PR
          </Button>
          <Button
            variant="outline"
            :loading="creatingTicket === 'manual'"
            :disabled="!!creatingTicket"
            @click="createTicket('manual')"
          >
            <Pencil /> Create ticket manually
          </Button>
        </div>
        <p class="max-w-md text-xs text-muted-foreground">
          “Draft with AI” dispatches a read-only refine run that reads the PR's diff and writes
          the title, description and acceptance criteria for you.
        </p>
      </CardContent>
    </Card>
    <TicketDetailView v-else-if="activeFacet === 'ticket' && ticket" :uid="focusedUid" />
    <ThreadView v-else-if="activeFacet === 'thread'" :uid="focusedUid" />
    <PullRequestDetailView v-else :uid="focusedUid" />
  </div>
</template>
