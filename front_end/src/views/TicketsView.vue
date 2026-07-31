<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Archive, ArrowLeft, ArrowRight, Boxes, ChevronDown, Crosshair, GitPullRequest, Layers, Plus,
  RefreshCw, Search, SlidersHorizontal, Sparkles, SquareKanban, X,
} from 'lucide-vue-next'
import { useTicketStore } from '@/stores/ticketStore'
import { useThreadStore } from '@/stores/threadStore'
import { useDeliveryStore } from '@/stores/deliveryStore'
import { useCurrentRepo } from '@/composables/useCurrentRepo'
import { useActiveRuns } from '@/composables/useActiveRuns'
import { useBoardPrefs } from '@/composables/useBoardPrefs'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { PageHeader } from '@/components/ui/page-header'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import { Badge } from '@/components/ui/badge'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import BoardLane from '@/components/tickets/BoardLane.vue'
import TicketCard from '@/components/tickets/TicketCard.vue'
import TicketDialog from '@/components/tickets/TicketDialog.vue'
import CreateEpicDialog from '@/components/tickets/CreateEpicDialog.vue'
import EpicBuilderDialog from '@/components/tickets/EpicBuilderDialog.vue'
import SuggestEpicsDialog from '@/components/tickets/SuggestEpicsDialog.vue'
import EpicProposalsPanel from '@/components/tickets/EpicProposalsPanel.vue'
import { STATUS_LABELS, STATUS_ORDER, statusVariant, transitionsFor } from '@/components/tickets/ticketMeta'
import type { PullRequestDTO, ThreadDTO, TicketDTO, TicketPriority, TicketStatus } from '@/types/api'

const store = useTicketStore()
const route = useRoute()
const router = useRouter()
const { uid: repoUid, repo } = useCurrentRepo()
const toast = useToast()

const tickets = ref<TicketDTO[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const createOpen = ref(false)
const groupOpen = ref(false)
const epicBuilderOpen = ref(false)
const suggestEpicsOpen = ref(false)
const proposalsPanel = ref<InstanceType<typeof EpicProposalsPanel> | null>(null)

// Threads joined onto cards by subject_ticket_uid — the "what does this need
// from me" badges. Best-effort: a failed thread fetch never blocks the board.
const threadStore = useThreadStore()
const boardThreads = ref<ThreadDTO[]>([])

/** `silent` skips the skeleton so the live heartbeat below never flickers the
 *  board the user is reading. */
async function reload({ silent = false } = {}) {
  if (!repoUid.value) return
  if (!silent) loading.value = true
  error.value = null
  try {
    const [ticketData, threadData] = await Promise.all([
      store.fetchTickets({ repository_uid: repoUid.value }),
      threadStore.listThreads({ repository_uid: repoUid.value }).catch(() => [] as ThreadDTO[]),
    ])
    tickets.value = ticketData
    boardThreads.value = threadData
    void proposalsPanel.value?.reload()
    void loadOrphanPrs()
  } catch (e: unknown) {
    // A failed background refresh must not blank out a board that is on screen.
    if (!silent) error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

/** The active (non-terminal) thread per ticket — terminal threads keep their
 *  history but stop badging the card. */
const activeThreadByTicket = computed<Record<string, ThreadDTO>>(() => {
  const out: Record<string, ThreadDTO> = {}
  for (const th of boardThreads.value) {
    if (th.phase !== 'done' && th.phase !== 'abandoned') out[th.subject_ticket_uid] = th
  }
  return out
})

// ── Ticketless PRs — work that exists on GitHub but not on the board yet ────
// Synced PRs without a ticket (opened by hand, outside OpenSweep) surface in
// their own strip; opening one lands on the work-item page where a ticket can
// be created manually or drafted by AI.
const delivery = useDeliveryStore()
const orphanPrs = ref<PullRequestDTO[]>([])

async function loadOrphanPrs() {
  if (!repoUid.value) return
  try {
    const prs = await delivery.fetchPullRequests({
      repository_uid: repoUid.value,
      state: 'open',
    })
    orphanPrs.value = prs.filter((pr) => !pr.ticket_uid)
  } catch {
    orphanPrs.value = [] // strip is best-effort; the board must still render
  }
}

watch(repoUid, () => void reload(), { immediate: true })

// Implement/refine runs move tickets across lanes and open PRs. Ride the shared
// /runs/active heartbeat: when the in-flight set changes, the board is stale.
const { signature } = useActiveRuns(
  () => (repoUid.value ? { repository_uid: repoUid.value } : null),
  { pollWhenIdle: true },
)
watch(signature, () => void reload({ silent: true }))

/** Tickets a group can absorb: ungrouped and not finished. */
const groupableTickets = computed(() =>
  tickets.value.filter((t) => !t.parent_ticket_uid && t.status !== 'done'),
)

function onGrouped(parent: TicketDTO) {
  void reload()
  toast.info(`Grouped under “${parent.title}” — approve it (Gate 1) to make the epic implementable.`)
}

const childCounts = computed<Record<string, number>>(() => {
  const counts: Record<string, number> = {}
  for (const t of tickets.value) {
    if (t.parent_ticket_uid) counts[t.parent_ticket_uid] = (counts[t.parent_ticket_uid] ?? 0) + 1
  }
  return counts
})

const COLUMN_HINTS: Record<TicketStatus, string> = {
  backlog: 'Unapproved — approve (Gate 1) or delete.',
  todo: 'Approved and ready to implement.',
  'in-progress': 'Being implemented.',
  'in-review': 'PR open, converging.',
  done: 'Merged and finished.',
}

/* ── Board-wide search + priority filter ─────────────────────────────── */

const boardSearch = ref('')
const boardPriority = ref<'all' | TicketPriority>('all')
const boardFiltering = computed(() => boardSearch.value.trim() !== '' || boardPriority.value !== 'all')

function clearBoardFilters() {
  boardSearch.value = ''
  boardPriority.value = 'all'
}

function matchesBoardFilters(t: TicketDTO): boolean {
  if (boardPriority.value !== 'all' && t.priority !== boardPriority.value) return false
  const q = boardSearch.value.trim().toLowerCase()
  if (!q) return true
  return `${t.title} ${t.description} ${t.labels.join(' ')}`.toLowerCase().includes(q)
}

/* ── Board view preferences (persisted per repo) ─────────────────────── */

const {
  prefs,
  isHidden,
  isCollapsed,
  setHidden,
  toggleCollapsed,
  setShowEpicMembers,
  focusActive,
  showAll,
} = useBoardPrefs(repoUid)

/**
 * Epic members stay off the board by default. Joining an epic deliberately
 * FREEZES a ticket's status — nothing transitions a member until the epic's
 * single PR merges and closes them all — so laning one by status would display
 * a stale value as if it were live. The parent card carries the epic's real
 * state, and its "N subtickets" badge is the way in.
 *
 * A text search always overrides the toggle: silently hiding a ticket someone
 * is searching for by name is worse than the staleness the default prevents.
 * Members surfaced this way are badged "in epic" by TicketCard.
 */
const epicMembersVisible = computed(
  () => prefs.showEpicMembers || boardSearch.value.trim() !== '',
)

const columns = computed(() =>
  STATUS_ORDER.map((status) => {
    const all = tickets.value.filter(
      (t) => t.status === status && (epicMembersVisible.value || !t.parent_ticket_uid),
    )
    return {
      status,
      title: STATUS_LABELS[status],
      subtitle: COLUMN_HINTS[status],
      items: boardFiltering.value ? all.filter(matchesBoardFilters) : all,
      total: all.length,
    }
  }),
)

const boardMatchCount = computed(() => columns.value.reduce((n, c) => n + c.items.length, 0))

/** Epic members currently withheld from the lanes — surfaced as a hint. */
const withheldMemberCount = computed(() =>
  epicMembersVisible.value ? 0 : tickets.value.filter((t) => !!t.parent_ticket_uid).length,
)

const visibleColumns = computed(() => columns.value.filter((c) => !isHidden(c.status)))
const hiddenTicketCount = computed(() =>
  columns.value.filter((c) => isHidden(c.status)).reduce((n, c) => n + c.items.length, 0),
)
const isFocusMode = computed(() =>
  prefs.hidden.length === 2 && isHidden('backlog') && isHidden('done'),
)

/* ── Single-lane focus view (?lane=<status>) ─────────────────────────── */

const laneView = computed<TicketStatus | null>(() => {
  const l = route.query.lane
  return typeof l === 'string' && (STATUS_ORDER as string[]).includes(l) ? (l as TicketStatus) : null
})

function goToLane(status: TicketStatus | null) {
  void router.replace({ query: { ...route.query, lane: status ?? undefined } })
}

const laneSearch = ref('')
const lanePriority = ref<'all' | TicketPriority>('all')
const laneSort = ref<'newest' | 'oldest' | 'priority' | 'title'>('newest')

// Entering a lane carries the board filters along; switching lanes resets.
watch(laneView, (lane) => {
  laneSearch.value = lane ? boardSearch.value : ''
  lanePriority.value = lane ? boardPriority.value : 'all'
  laneSort.value = 'newest'
})

const PRIORITY_RANK: Record<TicketPriority, number> = { urgent: 3, high: 2, medium: 1, low: 0 }
const PRIORITY_OPTIONS: TicketPriority[] = ['urgent', 'high', 'medium', 'low']
const LANE_SORT_OPTIONS = [
  { label: 'Newest first', value: 'newest' },
  { label: 'Oldest first', value: 'oldest' },
  { label: 'Priority: high → low', value: 'priority' },
  { label: 'Title A–Z', value: 'title' },
] as const

// Same rule as the board: members are withheld unless the toggle is on or a
// search is running (here the lane's own search box).
const laneTickets = computed(() =>
  tickets.value.filter(
    (t) =>
      t.status === laneView.value &&
      (prefs.showEpicMembers || laneSearch.value.trim() !== '' || !t.parent_ticket_uid),
  ),
)

const laneItems = computed<TicketDTO[]>(() => {
  let out = laneTickets.value
  const q = laneSearch.value.trim().toLowerCase()
  if (q) {
    out = out.filter((t) =>
      `${t.title} ${t.description} ${t.labels.join(' ')}`.toLowerCase().includes(q),
    )
  }
  if (lanePriority.value !== 'all') out = out.filter((t) => t.priority === lanePriority.value)
  const ts = (v?: string | null) => (v ? new Date(v).getTime() : 0)
  const cmp: Record<typeof laneSort.value, (a: TicketDTO, b: TicketDTO) => number> = {
    newest: (a, b) => ts(b.created_at) - ts(a.created_at),
    oldest: (a, b) => ts(a.created_at) - ts(b.created_at),
    priority: (a, b) =>
      (PRIORITY_RANK[b.priority] ?? 0) - (PRIORITY_RANK[a.priority] ?? 0) || ts(b.created_at) - ts(a.created_at),
    title: (a, b) => (a.title || '').localeCompare(b.title || '', undefined, { sensitivity: 'base' }),
  }
  return [...out].sort(cmp[laneSort.value])
})

const laneFiltered = computed(() =>
  laneSearch.value.trim() !== '' || lanePriority.value !== 'all',
)

/* ── Drag & drop between lanes ───────────────────────────────────────── */

const dragging = ref<TicketDTO | null>(null)

/** Lanes the dragged ticket may legally move to (mirrors the backend). */
const legalTargets = computed<Set<TicketStatus>>(() => {
  if (!dragging.value) return new Set()
  return new Set(transitionsFor(dragging.value).map((t) => t.to))
})

/** Backlog → Todo is Gate 1: dropping there asks for explicit approval.
    The move data lives apart from the open flag so the dialog's own close
    (which fires before our action handler) can't wipe it. */
const gateDialogOpen = ref(false)
const gateMove = ref<{ ticket: TicketDTO; to: TicketStatus } | null>(null)

function onDropTicket(to: TicketStatus) {
  const ticket = dragging.value
  dragging.value = null
  if (!ticket || ticket.status === to) return
  const transition = transitionsFor(ticket).find((t) => t.to === to)
  if (!transition) return
  if (transition.kind === 'gate') {
    gateMove.value = { ticket, to }
    gateDialogOpen.value = true
    return
  }
  void moveTicket(ticket, to)
}

function confirmGateMove() {
  const move = gateMove.value
  gateMove.value = null
  gateDialogOpen.value = false
  if (move) void moveTicket(move.ticket, move.to)
}

/** Card that just landed after a move — flashes briefly in its new lane. */
const flashUid = ref<string | null>(null)
let flashTimer: ReturnType<typeof setTimeout> | null = null

/** Optimistic move: reflect instantly, revert with a toast on failure. */
async function moveTicket(ticket: TicketDTO, to: TicketStatus) {
  const prev = ticket.status
  tickets.value = tickets.value.map((t) => (t.uid === ticket.uid ? { ...t, status: to } : t))
  flashUid.value = ticket.uid
  if (flashTimer) clearTimeout(flashTimer)
  flashTimer = setTimeout(() => { flashUid.value = null }, 800)
  try {
    const updated = await store.setStatus(ticket.uid, to)
    onTicketUpdated(updated)
    toast.success(prev === 'backlog' && to === 'todo' ? 'Ticket approved' : `Moved to ${STATUS_LABELS[to]}`, updated.title)
  } catch (e) {
    tickets.value = tickets.value.map((t) => (t.uid === ticket.uid ? { ...t, status: prev } : t))
    const msg = e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e)
    toast.error(e instanceof ApiError && e.status === 409 ? 'Illegal transition' : 'Move failed', msg)
  }
}

function onTicketUpdated(updated: TicketDTO) {
  // Archived tickets leave the board — the archived list is their home now.
  tickets.value = updated.archived
    ? tickets.value.filter((t) => t.uid !== updated.uid)
    : tickets.value.map((t) => (t.uid === updated.uid ? updated : t))
}

function onTicketDeleted(uid: string) {
  tickets.value = tickets.value.filter((t) => t.uid !== uid)
}

function onTicketCreated(ticket: TicketDTO) {
  if (ticket.repository_uid === repoUid.value) {
    tickets.value = [...tickets.value, ticket]
  }
}
</script>

<template>
  <div class="space-y-4">
    <PageHeader title="Work items" subtitle="Tickets, threads and pull requests — Gate 1 lives on the Backlog → Todo edge.">
      <DropdownMenu>
        <DropdownMenuTrigger as-child>
          <Button variant="outline" size="sm">
            <ArrowRight /> Go to
            <ChevronDown class="!size-3.5 text-muted-foreground" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" class="w-52">
          <DropdownMenuLabel>Focus one lane</DropdownMenuLabel>
          <DropdownMenuItem
            v-for="col in columns"
            :key="col.status"
            class="justify-between gap-2"
            @select="goToLane(col.status)"
          >
            {{ col.title }}
            <Badge :variant="statusVariant(col.status)" class="px-1.5 text-[10px]">{{ col.items.length }}</Badge>
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            class="gap-2"
            @select="router.push({ name: 'tickets-archived', params: { repoSlug: route.params.repoSlug } })"
          >
            <Archive class="size-4" /> Archived tickets
          </DropdownMenuItem>
          <template v-if="laneView">
            <DropdownMenuSeparator />
            <DropdownMenuItem class="gap-2" @select="goToLane(null)">
              <ArrowLeft class="size-4" /> Back to board
            </DropdownMenuItem>
          </template>
        </DropdownMenuContent>
      </DropdownMenu>
      <Popover v-if="!laneView">
        <PopoverTrigger as-child>
          <Button variant="outline" size="sm">
            <SlidersHorizontal /> View
            <span
              v-if="prefs.hidden.length"
              class="rounded-full bg-primary/10 px-1.5 text-[10px] font-semibold text-primary"
            >{{ prefs.hidden.length }} hidden</span>
          </Button>
        </PopoverTrigger>
        <PopoverContent align="end" class="w-64 space-y-3">
          <div class="flex items-center gap-2">
            <Button
              :variant="isFocusMode ? 'default' : 'outline'"
              size="sm"
              class="flex-1"
              title="Show only Todo, In Progress and In Review"
              @click="focusActive()"
            >
              <Crosshair /> Focus
            </Button>
            <Button variant="outline" size="sm" class="flex-1" @click="showAll()">Show all</Button>
          </div>
          <div class="space-y-2">
            <div
              v-for="col in columns"
              :key="col.status"
              class="flex items-center justify-between gap-2"
            >
              <Label :for="`lane-${col.status}`" class="flex-1 cursor-pointer text-sm font-normal">
                {{ col.title }}
                <span class="text-xs text-muted-foreground">· {{ col.items.length }}</span>
              </Label>
              <Switch
                :id="`lane-${col.status}`"
                :model-value="!isHidden(col.status)"
                @update:model-value="setHidden(col.status, !$event)"
              />
            </div>
          </div>
          <div class="space-y-2 border-t pt-3">
            <div class="flex items-center justify-between gap-2">
              <Label for="epic-members" class="flex-1 cursor-pointer text-sm font-normal">
                Epic subtickets
              </Label>
              <Switch
                id="epic-members"
                :model-value="prefs.showEpicMembers"
                @update:model-value="setShowEpicMembers($event)"
              />
            </div>
            <p class="text-xs text-muted-foreground">
              A subticket's status is frozen until its epic's PR merges, so by default
              only the epic parent is laned. Search always finds them either way.
            </p>
          </div>
          <p v-if="hiddenTicketCount" class="text-xs text-muted-foreground">
            {{ hiddenTicketCount }} ticket{{ hiddenTicketCount === 1 ? '' : 's' }} in hidden lanes.
          </p>
          <div class="border-t pt-3">
            <RouterLink
              :to="{ name: 'tickets-archived', params: { repoSlug: route.params.repoSlug } }"
              class="inline-flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
            >
              <Archive class="size-3.5" /> View archived tickets →
            </RouterLink>
          </div>
        </PopoverContent>
      </Popover>
      <Button variant="outline" size="sm" :disabled="loading" @click="reload()">
        <RefreshCw :class="{ 'animate-spin': loading }" /> Refresh
      </Button>
      <DropdownMenu>
        <DropdownMenuTrigger as-child>
          <Button
            variant="outline"
            size="sm"
            :disabled="!repoUid || groupableTickets.length < 2"
          >
            <Layers /> Epics
            <ChevronDown class="!size-3.5 text-muted-foreground" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" class="w-64">
          <DropdownMenuItem class="gap-2" @select="suggestEpicsOpen = true">
            <Sparkles class="size-4" /> Suggest epics (agent run)…
          </DropdownMenuItem>
          <DropdownMenuItem class="gap-2" @select="epicBuilderOpen = true">
            <Boxes class="size-4" /> Build epics by rule…
          </DropdownMenuItem>
          <DropdownMenuItem class="gap-2" @select="groupOpen = true">
            <Layers class="size-4" /> Create an epic by hand…
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <Button size="sm" @click="createOpen = true">
        <Plus /> New ticket
      </Button>
    </PageHeader>

    <!-- Board-wide search + priority filter — applies across every lane -->
    <div
      v-if="!loading && !error && tickets.length > 0 && !laneView"
      class="flex flex-wrap items-center gap-2"
    >
      <div class="relative w-full sm:w-72">
        <Search class="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
        <Input v-model="boardSearch" placeholder="Search tickets — title, description, labels…" class="h-9 pl-8" />
      </div>
      <Select :model-value="boardPriority" @update:model-value="boardPriority = $event as typeof boardPriority">
        <SelectTrigger class="h-9 w-full sm:w-40">
          <SelectValue placeholder="All priorities" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">All priorities</SelectItem>
          <SelectItem v-for="p in PRIORITY_OPTIONS" :key="p" :value="p">{{ p }}</SelectItem>
        </SelectContent>
      </Select>
      <template v-if="boardFiltering">
        <span class="text-xs tabular-nums text-muted-foreground">
          {{ boardMatchCount }} of {{ tickets.length }} tickets match
        </span>
        <Button variant="ghost" size="sm" @click="clearBoardFilters">
          <X /> Clear
        </Button>
      </template>
    </div>

    <!-- Agent-proposed groupings awaiting human approval -->
    <EpicProposalsPanel
      v-if="repoUid"
      ref="proposalsPanel"
      :repository-uid="repoUid"
      :tickets="tickets"
      @applied="reload()"
    />

    <!-- Externally-opened PRs with no ticket yet: work that exists on GitHub
         but not on the board. Opening one offers ticket creation. -->
    <Card v-if="orphanPrs.length">
      <CardContent class="space-y-2 p-4">
        <div class="flex items-center gap-2">
          <GitPullRequest class="size-4 text-muted-foreground" />
          <h2 class="text-sm font-semibold">Pull requests without a ticket</h2>
          <Badge variant="secondary" class="px-1.5 text-[10px]">{{ orphanPrs.length }}</Badge>
          <span class="text-xs text-muted-foreground">opened outside OpenSweep — open one to adopt it onto the board</span>
        </div>
        <div class="flex gap-2 overflow-x-auto pb-1">
          <RouterLink
            v-for="pr in orphanPrs"
            :key="pr.uid"
            :to="{ name: 'pull-request-detail', params: { uid: pr.uid } }"
            class="card-interactive w-64 shrink-0 rounded-md border bg-muted/40 p-3 hover:border-primary/60"
          >
            <div class="flex items-center gap-1.5 text-xs text-muted-foreground">
              <GitPullRequest class="size-3" /> #{{ pr.github_number }}
              <Badge v-if="pr.draft" variant="outline" class="px-1 text-[9px]">draft</Badge>
              <span class="ml-auto font-mono text-[10px]">{{ pr.ci_state || '—' }}</span>
            </div>
            <div class="mt-1 truncate text-sm font-medium">{{ pr.title || '(untitled)' }}</div>
            <div class="mt-0.5 truncate font-mono text-[10px] text-muted-foreground">
              {{ pr.head_ref }} → {{ pr.base_ref }}
            </div>
          </RouterLink>
        </div>
      </CardContent>
    </Card>

    <!-- Loading -->
    <div v-if="loading" class="flex gap-4 overflow-x-auto pb-2">
      <Card v-for="i in 5" :key="i" class="w-72 shrink-0">
        <CardContent class="space-y-3 p-4">
          <Skeleton class="h-4 w-1/2" />
          <Skeleton class="h-24" />
          <Skeleton class="h-24" />
        </CardContent>
      </Card>
    </div>

    <!-- Error -->
    <ErrorState v-else-if="error" title="Couldn't load tickets" :message="error">
      <Button variant="outline" size="sm" @click="reload()">Retry</Button>
    </ErrorState>

    <!-- Empty -->
    <EmptyState
      v-else-if="tickets.length === 0"
      :icon="SquareKanban"
      title="No tickets yet"
      description="No tickets in this workspace yet. Tickets come from deferred findings, agent proposals, or you."
    >
      <Button size="sm" @click="createOpen = true">
        <Plus /> New ticket
      </Button>
    </EmptyState>

    <!-- Single-lane focus view with search, filter and sort -->
    <Card v-else-if="laneView">
      <div class="flex flex-wrap items-center gap-2 border-b p-4">
        <Button variant="ghost" size="sm" @click="goToLane(null)">
          <ArrowLeft /> Board
        </Button>
        <h2 class="flex items-center gap-2 text-sm font-semibold">
          {{ STATUS_LABELS[laneView] }}
          <Badge :variant="statusVariant(laneView)" class="px-1.5 text-[10px]">
            {{ laneFiltered ? `${laneItems.length} / ${laneTickets.length}` : laneTickets.length }}
          </Badge>
        </h2>
        <div class="relative w-full sm:ml-auto sm:w-64">
          <Search class="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input v-model="laneSearch" placeholder="Search title, description, labels…" class="h-9 pl-8" />
        </div>
        <Select :model-value="lanePriority" @update:model-value="lanePriority = $event as typeof lanePriority">
          <SelectTrigger class="h-9 w-full sm:w-40">
            <SelectValue placeholder="All priorities" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All priorities</SelectItem>
            <SelectItem v-for="p in PRIORITY_OPTIONS" :key="p" :value="p">{{ p }}</SelectItem>
          </SelectContent>
        </Select>
        <Select :model-value="laneSort" @update:model-value="laneSort = $event as typeof laneSort">
          <SelectTrigger class="h-9 w-full sm:w-48">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem v-for="o in LANE_SORT_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <CardContent class="p-4">
        <EmptyState
          v-if="laneItems.length === 0"
          :icon="SquareKanban"
          :title="laneFiltered ? 'No matching tickets' : `Nothing in ${STATUS_LABELS[laneView]}`"
          :description="laneFiltered
            ? 'No tickets in this lane match the current search or priority filter.'
            : COLUMN_HINTS[laneView]"
          class="border-0"
        />
        <TransitionGroup
          v-else
          tag="div"
          name="board-card"
          class="stagger-children relative grid gap-3 sm:grid-cols-2 xl:grid-cols-3"
        >
          <TicketCard
            v-for="ticket in laneItems"
            :key="ticket.uid"
            :ticket="ticket"
            :subticket-count="childCounts[ticket.uid] ?? 0"
            :thread="activeThreadByTicket[ticket.uid] ?? null"
            @updated="onTicketUpdated"
            @deleted="onTicketDeleted"
          />
        </TransitionGroup>
      </CardContent>
    </Card>

    <!-- Board — horizontal scroll on narrow screens, snap per column;
         lanes scroll independently; drag cards between lanes. -->
    <TransitionGroup
      v-else
      tag="div"
      name="board-lane"
      class="stagger-children relative flex snap-x snap-mandatory items-start gap-3 overflow-x-auto pb-2"
    >
      <BoardLane
        v-for="col in visibleColumns"
        :key="col.status"
        :status="col.status"
        :subtitle="col.subtitle"
        :items="col.items"
        :total="col.total"
        :child-counts="childCounts"
        :collapsed="isCollapsed(col.status)"
        :dragging-uid="dragging?.uid ?? null"
        :drop-legal="legalTargets.has(col.status)"
        :flash-uid="flashUid"
        :active-threads="activeThreadByTicket"
        @toggle-collapse="toggleCollapsed(col.status)"
        @open-lane="goToLane(col.status)"
        @drag-start="dragging = $event"
        @drag-end="dragging = null"
        @drop-ticket="onDropTicket"
        @updated="onTicketUpdated"
        @deleted="onTicketDeleted"
      />
      <button
        v-if="hiddenTicketCount || prefs.hidden.length"
        type="button"
        class="flex h-64 w-11 shrink-0 snap-start flex-col items-center justify-center gap-2 rounded-xl border border-dashed text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        title="Show all lanes"
        @click="showAll()"
      >
        <Plus class="size-3.5" />
        <span class="text-xs font-medium [writing-mode:vertical-rl]">{{ prefs.hidden.length }} hidden lane{{ prefs.hidden.length === 1 ? '' : 's' }}</span>
      </button>
      <!-- Never hide work silently: say how much is folded into epics. -->
      <button
        v-if="withheldMemberCount"
        type="button"
        class="flex h-64 w-11 shrink-0 snap-start flex-col items-center justify-center gap-2 rounded-xl border border-dashed text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        title="Show epic subtickets as loose cards"
        @click="setShowEpicMembers(true)"
      >
        <Layers class="size-3.5" />
        <span class="text-xs font-medium [writing-mode:vertical-rl]">{{ withheldMemberCount }} in epics</span>
      </button>
    </TransitionGroup>

    <!-- Gate 1 confirmation when a card is dragged Backlog → Todo -->
    <AlertDialog v-model:open="gateDialogOpen">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Approve this ticket?</AlertDialogTitle>
          <AlertDialogDescription>
            “{{ gateMove?.ticket.title }}” moves from Backlog to Todo. This is Gate 1 —
            approved tickets become implementable by agents.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction @click="confirmGateMove()">Approve</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>

    <TicketDialog
      v-model:open="createOpen"
      :repositories="repo ? [repo] : []"
      :default-repository-uid="repoUid ?? ''"
      @saved="onTicketCreated"
    />
    <CreateEpicDialog
      v-model:open="groupOpen"
      :repository-uid="repoUid ?? ''"
      :tickets="groupableTickets"
      @saved="onGrouped"
    />
    <EpicBuilderDialog
      v-model:open="epicBuilderOpen"
      :repository-uid="repoUid ?? ''"
      @created="reload"
    />
    <SuggestEpicsDialog
      v-model:open="suggestEpicsOpen"
      :repository-uid="repoUid ?? ''"
    />
  </div>
</template>

<style scoped>
/* Card enter/leave/reflow in the single-lane grid. */
.board-card-enter-active,
.board-card-leave-active {
  transition: opacity 200ms cubic-bezier(.2, .7, .2, 1), transform 200ms cubic-bezier(.2, .7, .2, 1);
}
.board-card-enter-from {
  opacity: 0;
  transform: translateY(6px) scale(0.98);
}
.board-card-leave-to {
  opacity: 0;
  transform: scale(0.96);
}
.board-card-leave-active {
  position: absolute;
}
.board-card-move {
  transition: transform 250ms cubic-bezier(.2, .7, .2, 1);
}

/* Lanes slide smoothly when one collapses, hides, or reappears. */
.board-lane-move {
  transition: transform 250ms cubic-bezier(.2, .7, .2, 1);
}
.board-lane-enter-active,
.board-lane-leave-active {
  transition: opacity 200ms cubic-bezier(.2, .7, .2, 1), transform 200ms cubic-bezier(.2, .7, .2, 1);
}
.board-lane-enter-from,
.board-lane-leave-to {
  opacity: 0;
  transform: scale(0.97);
}
.board-lane-leave-active {
  position: absolute;
}
</style>
