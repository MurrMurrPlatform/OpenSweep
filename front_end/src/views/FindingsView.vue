<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { Archive, FileCode2, Inbox, Plus, Repeat2, Search, ShieldCheck, SquareKanban, Trash2, Wrench, X } from 'lucide-vue-next'
import { useFindingStore, type FindingSortBy } from '@/stores/findingStore'
import { useTicketStore } from '@/stores/ticketStore'
import { formatRelativeTime } from '@/lib/utils'
import { useCurrentRepo } from '@/composables/useCurrentRepo'
import { useToast } from '@/composables/useToast'
import { PageHeader } from '@/components/ui/page-header'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Separator } from '@/components/ui/separator'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import FindingEditDialog from '@/components/findings/FindingEditDialog.vue'
import TrustBadge from '@/components/findings/TrustBadge.vue'
import {
  corroborationCount,
  SEVERITY_RANK,
  severityVariant,
  statusLabel,
  statusVariant,
  TRUST_HIGH,
  TRUST_MEDIUM,
  trustPercent,
} from '@/components/findings/findingMeta'
import type {
  FindingDTO,
  FindingStatus,
  FindingStatusFilter,
  Severity,
  TicketPriority,
  TicketSize,
} from '@/types/api'

const findings = useFindingStore()
const ticketStore = useTicketStore()
const { uid: repoUid } = useCurrentRepo()
const toast = useToast()

const all = ref<FindingDTO[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const createOpen = ref(false)
const search = ref('')
const filter = ref<'all' | 'issues' | 'improvements' | 'proposals'>('all')
const tagFilter = ref('')
// Search box inside the "+N more" tag popover — filters the folded-away tags.
const tagSearch = ref('')
const tagPopoverOpen = ref(false)
const severityFilter = ref<'' | Severity>('')
const statusFilter = ref<FindingStatusFilter | 'all'>('open')
const trustFilter = ref<TrustFilter>('all')
// This is a triage inbox: the first question is "what is most likely real and
// worth my time", not "what happened last". Trust answers that directly —
// cross-run corroboration and static-analyzer confirmation are evidence the
// platform verified itself, unlike a model's self-reported confidence.
// Recency stays one click away in the same control.
const sortKey = ref('trust_desc')
const selected = ref<Set<string>>(new Set())

const SEVERITY_OPTIONS = [
  { label: 'Critical', value: 'critical' },
  { label: 'High', value: 'high' },
  { label: 'Medium', value: 'medium' },
  { label: 'Low', value: 'low' },
]

// "Processed" is a server-side pseudo-status meaning "not open" — it is what
// you reach for after promoting or triaging something and wanting to find it
// again. The individual statuses below it narrow that same set.
const STATUS_OPTIONS = [
  { label: 'Open', value: 'open' },
  { label: 'Processed', value: 'processed' },
  { label: 'Ticketed', value: 'ticketed' },
  { label: 'Acknowledged', value: 'acknowledged' },
  { label: "Won't fix", value: 'wont-fix' },
  { label: 'Fixed', value: 'fixed' },
  { label: 'Dismissed', value: 'dismissed' },
  { label: 'All statuses', value: 'all' },
]

type TrustFilter = 'all' | 'high' | 'medium'

const TRUST_OPTIONS: { label: string; value: TrustFilter }[] = [
  { label: 'Any trust', value: 'all' },
  { label: `High trust · ≥${trustPercent(TRUST_HIGH)}%`, value: 'high' },
  { label: `Medium+ · ≥${trustPercent(TRUST_MEDIUM)}%`, value: 'medium' },
]

const TRUST_THRESHOLD: Record<TrustFilter, number> = {
  all: 0,
  high: TRUST_HIGH,
  medium: TRUST_MEDIUM,
}

const SORT_OPTIONS = [
  { label: 'Most credible', value: 'trust_desc' },
  { label: 'Newest first', value: 'updated_desc' },
  { label: 'Oldest first', value: 'updated_asc' },
  { label: 'First found', value: 'created_asc' },
  { label: 'Last found', value: 'created_desc' },
  { label: 'Severity: high → low', value: 'severity_desc' },
  { label: 'Severity: low → high', value: 'severity_asc' },
  { label: 'Confidence', value: 'confidence_desc' },
  { label: 'Title A–Z', value: 'title_asc' },
]

/** Sort key → the server's whitelisted `sort_by`/`sort_dir`. The list is also
 *  re-sorted client-side (below) so switching sort stays instant, but the
 *  fetch asks for the same order so the first paint is already correct. */
const SORT_TO_API: Record<string, { sort_by: FindingSortBy; sort_dir: 'asc' | 'desc' }> = {
  trust_desc: { sort_by: 'trust', sort_dir: 'desc' },
  updated_desc: { sort_by: 'updated_at', sort_dir: 'desc' },
  updated_asc: { sort_by: 'updated_at', sort_dir: 'asc' },
  created_desc: { sort_by: 'created_at', sort_dir: 'desc' },
  created_asc: { sort_by: 'created_at', sort_dir: 'asc' },
  severity_desc: { sort_by: 'severity', sort_dir: 'desc' },
  severity_asc: { sort_by: 'severity', sort_dir: 'asc' },
  confidence_desc: { sort_by: 'confidence', sort_dir: 'desc' },
  title_asc: { sort_by: 'title', sort_dir: 'asc' },
}

// reka SelectItem values can't be empty strings; 'all' is the "no filter"
// sentinel, translated back to '' (the item.filter treats '' as no severity).
function onSeverity(v: unknown) {
  severityFilter.value = v === 'all' ? '' : (v as Severity)
}
function onStatus(v: unknown) {
  statusFilter.value = v as FindingStatusFilter | 'all'
}
function onTrust(v: unknown) {
  trustFilter.value = v as TrustFilter
}
function onSort(v: unknown) {
  sortKey.value = v as string
}

function ts(value?: string | null): number {
  return value ? new Date(value).getTime() : 0
}

function sortFindings(list: FindingDTO[], key: string): FindingDTO[] {
  const recency = (f: FindingDTO) => ts(f.updated_at) || ts(f.created_at)
  const sev = (f: FindingDTO) => SEVERITY_RANK[f.severity] ?? 1
  const cmp: Record<string, (a: FindingDTO, b: FindingDTO) => number> = {
    // Mirrors the server's `sort_by=trust`: severity breaks trust ties, because
    // of two equally credible findings the worse one is worth opening first.
    trust_desc: (a, b) => b.trust - a.trust || sev(b) - sev(a) || recency(b) - recency(a),
    updated_desc: (a, b) => recency(b) - recency(a),
    updated_asc: (a, b) => recency(a) - recency(b),
    created_desc: (a, b) => ts(b.created_at) - ts(a.created_at),
    created_asc: (a, b) => ts(a.created_at) - ts(b.created_at),
    severity_desc: (a, b) => sev(b) - sev(a) || recency(b) - recency(a),
    severity_asc: (a, b) => sev(a) - sev(b) || recency(b) - recency(a),
    confidence_desc: (a, b) => b.confidence - a.confidence || recency(b) - recency(a),
    title_asc: (a, b) => (a.title || '').localeCompare(b.title || '', undefined, { sensitivity: 'base' }),
  }
  return [...list].sort(cmp[key] ?? cmp.trust_desc)
}

const counts = computed(() => ({
  all: all.value.length,
  issues: all.value.filter((f) => f.kind === 'defect' || f.kind === 'gap').length,
  improvements: all.value.filter((f) => f.kind === 'improvement').length,
  proposals: all.value.filter((f) => f.kind === 'proposal').length,
}))

/** How many tag chips stay inline before the rest fold into the popover.
 *  Tag vocabulary grows with the finding count, so an unbounded chip row would
 *  push the list itself off the screen. */
const INLINE_TAG_LIMIT = 8

/** Distinct tags across the loaded findings, most frequent first — the common
 *  tags are the useful filters, and frequency order keeps them stable as new
 *  findings land. Ties break alphabetically so the row doesn't jitter. */
const tagCounts = computed(() => {
  const counts = new Map<string, number>()
  for (const f of all.value) for (const t of f.tags || []) counts.set(t, (counts.get(t) ?? 0) + 1)
  return [...counts.entries()]
    .map(([tag, count]) => ({ tag, count }))
    .sort((a, b) => b.count - a.count || a.tag.localeCompare(b.tag))
})

/** Inline chips: the top N, plus the active tag pinned in if it ranks lower —
 *  a selected filter must never be invisible. */
const inlineTags = computed(() => {
  const top = tagCounts.value.slice(0, INLINE_TAG_LIMIT)
  if (tagFilter.value && !top.some((t) => t.tag === tagFilter.value)) {
    const active = tagCounts.value.find((t) => t.tag === tagFilter.value)
    if (active) return [...top.slice(0, INLINE_TAG_LIMIT - 1), active]
  }
  return top
})

/** Everything not shown inline, narrowed by the popover's own search box. */
const overflowTags = computed(() => {
  const inline = new Set(inlineTags.value.map((t) => t.tag))
  const rest = tagCounts.value.filter((t) => !inline.has(t.tag))
  const q = tagSearch.value.trim().toLowerCase()
  return q ? rest.filter((t) => t.tag.toLowerCase().includes(q)) : rest
})

// Switching repo or status can retire a tag entirely. Left set, it would filter
// the list down to nothing with no chip on screen to explain why.
watch(tagCounts, (tags) => {
  if (tagFilter.value && !tags.some((t) => t.tag === tagFilter.value)) tagFilter.value = ''
})

watch(tagPopoverOpen, (open) => {
  if (!open) tagSearch.value = ''
})

const items = computed(() => {
  let out = all.value
  if (filter.value === 'issues') out = out.filter((f) => f.kind === 'defect' || f.kind === 'gap')
  else if (filter.value === 'improvements') out = out.filter((f) => f.kind === 'improvement')
  else if (filter.value === 'proposals') out = out.filter((f) => f.kind === 'proposal')
  if (tagFilter.value) out = out.filter((f) => (f.tags || []).includes(tagFilter.value))
  if (severityFilter.value) out = out.filter((f) => f.severity === severityFilter.value)
  // The API has no trust threshold param — the whole list is already loaded,
  // so the cut happens here.
  if (trustFilter.value !== 'all') {
    const floor = TRUST_THRESHOLD[trustFilter.value]
    out = out.filter((f) => f.trust >= floor)
  }
  const q = search.value.trim().toLowerCase()
  if (q) {
    out = out.filter((f) =>
      [f.title, f.description, f.subtype, ...(f.tags || []), ...(f.affected_paths || [])]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
        .includes(q),
    )
  }
  return sortFindings(out, sortKey.value)
})

const filtersActive = computed(
  () =>
    search.value.trim() !== '' ||
    filter.value !== 'all' ||
    tagFilter.value !== '' ||
    severityFilter.value !== '' ||
    trustFilter.value !== 'all',
)

/** How much noise the high-trust cut would remove — makes the filter
 *  discoverable without the user having to try it. */
const highTrustCount = computed(() => all.value.filter((f) => f.trust >= TRUST_HIGH).length)

const visibleSelectedCount = computed(() => items.value.filter((f) => selected.value.has(f.uid)).length)
const allVisibleSelected = computed(() => items.value.length > 0 && visibleSelectedCount.value === items.value.length)

// Drops stale responses when the workspace switches mid-flight
// (pattern: composables/useActiveRuns.ts).
let reloadGeneration = 0

async function reload() {
  if (!repoUid.value) return
  const gen = ++reloadGeneration
  loading.value = true
  error.value = null
  // A selection must never survive into another repo's list — "Remove
  // selected" would delete invisible findings from the previous workspace.
  selected.value = new Set()
  try {
    // Feature ideas live on their own page (FeatureIdeasView) — excluded server-side.
    const data = await findings.fetchAll({
      status: statusFilter.value === 'all' ? undefined : statusFilter.value,
      repository_uid: repoUid.value,
      exclude_kind: 'feature-idea',
      ...(SORT_TO_API[sortKey.value] ?? SORT_TO_API.trust_desc),
    })
    if (gen !== reloadGeneration) return
    all.value = data
  } catch (e: unknown) {
    if (gen !== reloadGeneration) return
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    if (gen === reloadGeneration) loading.value = false
  }
}

onMounted(reload)
watch(repoUid, () => {
  selected.value = new Set()
  void reload()
})

const CHIPS: { id: typeof filter.value; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'issues', label: 'Issues' },
  { id: 'improvements', label: 'Improvements' },
  { id: 'proposals', label: 'Proposals' },
]

watch([filter, tagFilter, severityFilter, trustFilter, search], () => {
  selected.value = new Set()
})

// Status is filtered server-side — refetch on change.
watch(statusFilter, () => void reload())

function toggleOne(uid: string, value: boolean) {
  const next = new Set(selected.value)
  if (value) next.add(uid)
  else next.delete(uid)
  selected.value = next
}

function toggleVisible(value: boolean) {
  const next = new Set(selected.value)
  for (const f of items.value) {
    if (value) next.add(f.uid)
    else next.delete(f.uid)
  }
  selected.value = next
}

// ── Bulk actions on the selection (floating action bar) ─────────────────────

type BulkAction = 'tickets' | 'dismiss' | 'delete'

const bulkBusy = ref<BulkAction | null>(null)
// Open flag and action live apart: the dialog's own close event fires before
// the action button's handler, and must not wipe the pending action.
const confirmOpen = ref(false)
const confirmAction = ref<BulkAction | null>(null)

function askConfirm(action: BulkAction) {
  confirmAction.value = action
  confirmOpen.value = true
}

const selectedFindings = computed(() => all.value.filter((f) => selected.value.has(f.uid)))

const confirmCopy = computed(() => {
  const n = selected.value.size
  const s = n === 1 ? '' : 's'
  switch (confirmAction.value) {
    case 'tickets':
      return {
        title: `Create ${n} ticket${s}?`,
        description: `One ticket per finding, prefilled from it and linked as the ticket origin. New tickets land in Backlog until a human approves them (Gate 1).`,
        cta: 'Create tickets',
      }
    case 'dismiss':
      return {
        title: `Dismiss ${n} finding${s}?`,
        description: `Dismissed findings leave the open inbox but stay on record — find them via the status filter. This is reversible from the finding page.`,
        cta: 'Dismiss',
      }
    case 'delete':
      return {
        title: `Delete ${n} finding${s}?`,
        description: `This permanently removes them from OpenSweep tracking. If they might matter later, dismiss instead.`,
        cta: 'Delete',
      }
    default:
      return { title: '', description: '', cta: '' }
  }
})

function runConfirmedAction() {
  const action = confirmAction.value
  confirmAction.value = null
  confirmOpen.value = false
  if (action === 'tickets') void bulkCreateTickets()
  else if (action === 'dismiss') void bulkDismiss()
  else if (action === 'delete') void bulkDelete()
}

const SEVERITY_TO_PRIORITY: Record<Severity, TicketPriority> = {
  low: 'low',
  medium: 'medium',
  high: 'high',
  critical: 'urgent',
}

const TICKET_SIZES = ['trivial', 'small', 'medium', 'large']

/** Same prefill as the single-finding "Promote to ticket" dialog. */
function ticketRequestFor(f: FindingDTO) {
  const topPath = (f.affected_paths || [])[0]
  return {
    title: f.title,
    repository_uid: f.repository_uid,
    description: [
      f.description,
      f.root_cause ? `Root cause:\n${f.root_cause}` : '',
      f.why_it_matters,
      f.suggested_fix ? `Suggested fix:\n${f.suggested_fix}` : '',
    ].filter(Boolean).join('\n\n'),
    acceptance_criteria: [
      topPath
        ? `The problem no longer occurs at ${topPath}`
        : 'The problem described in the origin finding no longer occurs',
      f.suggested_fix ? 'The suggested fix (or an equivalent remedy) is implemented' : '',
      'A regression test covers this case',
    ].filter(Boolean),
    labels: f.tags || [],
    priority: SEVERITY_TO_PRIORITY[f.severity] || 'medium',
    size: (TICKET_SIZES.includes(f.size) ? f.size : '') as TicketSize,
    origin: 'finding' as const,
    origin_finding_uid: f.uid,
  }
}

/**
 * Reflect a status transition in the loaded list without a refetch.
 *
 * Status is filtered SERVER-side, so a transition can push a finding out of
 * the view it is currently in — that is the whole point of "processed". Patch
 * the findings that still match the active filter; drop the ones that no
 * longer do. `changed` maps each affected uid to any extra fields that move
 * with the status (ticket_uid), so one pass applies the whole transition.
 */
function applyStatusLocally(
  changed: Map<string, Partial<FindingDTO>>,
  status: FindingStatus,
) {
  const active = statusFilter.value
  const stillMatches =
    active === 'all' || (active === 'processed' ? status !== 'open' : active === status)
  all.value = stillMatches
    ? all.value.map((f) => (changed.has(f.uid) ? { ...f, status, ...changed.get(f.uid) } : f))
    : all.value.filter((f) => !changed.has(f.uid))
}

async function bulkCreateTickets() {
  const targets = selectedFindings.value
  if (!targets.length || bulkBusy.value) return
  bulkBusy.value = 'tickets'
  try {
    const results = await Promise.allSettled(targets.map((f) => ticketStore.createTicket(ticketRequestFor(f))))
    const failed = results.filter((r) => r.status === 'rejected').length
    const ok = results.length - failed
    if (ok) {
      // Promotion moves a finding to "ticketed" and off the open board — but
      // ONLY if it was open. The server refuses to overwrite an existing
      // triage decision, so an already-acknowledged finding keeps its status
      // and must not be patched here either.
      const promoted = new Map<string, Partial<FindingDTO>>()
      targets.forEach((f, i) => {
        const r = results[i]
        if (r.status === 'fulfilled' && f.status === 'open') {
          promoted.set(f.uid, { ticket_uid: r.value.uid })
        }
      })
      applyStatusLocally(promoted, 'ticketed')
      toast.success(
        `Created ${ok} ticket${ok === 1 ? '' : 's'} in Backlog`,
        'Each stays linked to its origin finding. Approve them (Gate 1) to make them implementable.',
      )
      // Keep only the failed ones selected so a retry is one click away.
      const failedUids = new Set(targets.filter((_, i) => results[i].status === 'rejected').map((f) => f.uid))
      selected.value = failedUids
    }
    if (failed) toast.error(`${failed} ticket${failed === 1 ? '' : 's'} failed to create`, 'The affected findings stay selected.')
  } finally {
    bulkBusy.value = null
  }
}

async function bulkDismiss() {
  const targets = selectedFindings.value
  if (!targets.length || bulkBusy.value) return
  bulkBusy.value = 'dismiss'
  try {
    const results = await Promise.allSettled(targets.map((f) => findings.dismiss(f.uid)))
    const okUids = new Set(targets.filter((_, i) => results[i].status === 'fulfilled').map((f) => f.uid))
    const failed = results.length - okUids.size
    applyStatusLocally(new Map(Array.from(okUids, (uid) => [uid, {}])), 'dismissed')
    selected.value = new Set(Array.from(selected.value).filter((uid) => !okUids.has(uid)))
    if (okUids.size) toast.success(`Dismissed ${okUids.size} finding${okUids.size === 1 ? '' : 's'}`)
    if (failed) toast.error(`${failed} dismissal${failed === 1 ? '' : 's'} failed`, 'The affected findings stay selected.')
  } finally {
    bulkBusy.value = null
  }
}

async function bulkDelete() {
  const uids = Array.from(selected.value)
  if (!uids.length || bulkBusy.value) return
  bulkBusy.value = 'delete'
  try {
    await findings.removeMany(uids)
    all.value = all.value.filter((f) => !selected.value.has(f.uid))
    selected.value = new Set()
    toast.success(`Deleted ${uids.length} finding${uids.length === 1 ? '' : 's'}`)
  } catch (e: unknown) {
    toast.error('Delete failed', e instanceof Error ? e.message : String(e))
  } finally {
    bulkBusy.value = null
  }
}

/** A manually-filed finding lands open — surface it at the top immediately. */
function onFiled(finding: FindingDTO) {
  // Feature ideas live on the Ideas page, not in this inbox.
  if (finding.kind === 'feature-idea') {
    toast.info('Filed as feature idea — see the Ideas page')
    return
  }
  if (finding.status === 'open' && !all.value.some((f) => f.uid === finding.uid)) {
    all.value = [finding, ...all.value]
  }
}

const emptyCopy = computed(() => {
  if (trustFilter.value !== 'all' && all.value.length) {
    return {
      title: 'Nothing clears that trust bar',
      description: `No finding here reaches ${trustPercent(TRUST_THRESHOLD[trustFilter.value])}% trust. Loosen the trust filter, or launch verification runs to corroborate the ones you have.`,
    }
  }
  if (search.value.trim()) {
    return {
      title: 'No matching findings',
      description: `Nothing matches “${search.value.trim()}” in the current filter.`,
    }
  }
  switch (filter.value) {
    case 'issues':
      return { title: 'No issues', description: 'No defects or gaps in the current filter.' }
    case 'improvements':
      return { title: 'No improvements', description: 'No improvement suggestions in the current filter.' }
    case 'proposals':
      return { title: 'No proposals', description: 'No pending proposals in the current filter.' }
    default:
      return { title: 'No open findings', description: 'Agents haven’t surfaced any open items for this repository yet.' }
  }
})
</script>

<template>
  <div class="space-y-4">
    <PageHeader
      title="Findings"
      subtitle="Everything the agents have surfaced — open items first, most credible first."
    >
      <Button size="sm" :disabled="!repoUid" @click="createOpen = true">
        <Plus /> File finding
      </Button>
    </PageHeader>

    <Card class="overflow-hidden">
      <div class="space-y-3 border-b p-4">
        <!-- Row 1: search + structured filters + selection -->
        <div class="flex flex-wrap items-center gap-2">
          <div class="relative w-full min-w-48 sm:w-auto sm:flex-1 sm:max-w-80">
            <Search class="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input v-model="search" placeholder="Search title, description, tags, paths…" class="h-9 pl-8" />
            <button
              v-if="search"
              type="button"
              class="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground transition-colors hover:text-foreground"
              title="Clear search"
              @click="search = ''"
            >
              <X class="size-3.5" />
            </button>
          </div>
          <Select :model-value="severityFilter || 'all'" @update:model-value="onSeverity">
            <SelectTrigger class="h-9 w-full sm:w-36">
              <SelectValue placeholder="All severities" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All severities</SelectItem>
              <SelectItem v-for="o in SEVERITY_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
            </SelectContent>
          </Select>
          <Select :model-value="statusFilter" @update:model-value="onStatus">
            <SelectTrigger class="h-9 w-full sm:w-36">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem v-for="o in STATUS_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
            </SelectContent>
          </Select>
          <!-- Trust cut: the fastest way from "200 findings" to "the ones
               something other than the model vouched for". -->
          <Select :model-value="trustFilter" @update:model-value="onTrust">
            <SelectTrigger class="h-9 w-full sm:w-40">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem v-for="o in TRUST_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
            </SelectContent>
          </Select>
          <Select :model-value="sortKey" @update:model-value="onSort">
            <SelectTrigger class="h-9 w-full sm:w-44">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem v-for="o in SORT_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</SelectItem>
            </SelectContent>
          </Select>
          <div class="flex items-center gap-2 sm:ml-auto">
            <span v-if="filtersActive" class="text-xs tabular-nums text-muted-foreground">
              {{ items.length }} of {{ all.length }}
            </span>
            <Button
              variant="outline"
              size="sm"
              :disabled="items.length === 0"
              @click="toggleVisible(!allVisibleSelected)"
            >
              {{ allVisibleSelected ? 'Clear visible' : 'Select visible' }}
            </Button>
          </div>
        </div>
        <!-- Row 2: kind chips + data-driven tag chips -->
        <div class="flex flex-wrap items-center gap-2">
          <Button
            v-for="c in CHIPS"
            :key="c.id"
            :variant="filter === c.id ? 'secondary' : 'ghost'"
            size="sm"
            @click="filter = c.id"
          >
            {{ c.label }}
            <span class="text-muted-foreground">· {{ counts[c.id] }}</span>
          </Button>
          <!-- One-click cut to the corroborated / tool-confirmed subset. -->
          <Button
            v-if="trustFilter === 'all' && highTrustCount > 0 && highTrustCount < all.length"
            variant="ghost"
            size="sm"
            class="text-good"
            @click="trustFilter = 'high'"
          >
            <ShieldCheck /> {{ highTrustCount }} high-trust
          </Button>
          <template v-if="tagCounts.length">
            <span class="mx-1 hidden h-4 w-px bg-border sm:block" />
            <button
              v-for="t in inlineTags"
              :key="t.tag"
              type="button"
              :class="[
                'rounded-full border px-2.5 py-0.5 text-xs transition-colors',
                tagFilter === t.tag
                  ? 'border-primary bg-primary/10 text-primary'
                  : 'border-border text-muted-foreground hover:bg-accent',
              ]"
              @click="tagFilter = tagFilter === t.tag ? '' : t.tag"
            >
              {{ t.tag }}
              <span class="ml-1 tabular-nums opacity-60">{{ t.count }}</span>
            </button>
            <!-- Everything past the inline limit — keeps the row a fixed
                 height no matter how large the tag vocabulary grows. -->
            <Popover v-if="tagCounts.length > inlineTags.length" v-model:open="tagPopoverOpen">
              <PopoverTrigger as-child>
                <button
                  type="button"
                  class="rounded-full border border-dashed border-border px-2.5 py-0.5 text-xs text-muted-foreground transition-colors hover:bg-accent"
                >
                  +{{ tagCounts.length - inlineTags.length }} more
                </button>
              </PopoverTrigger>
              <PopoverContent align="start" class="w-64 p-0">
                <div class="border-b p-2">
                  <Input
                    v-model="tagSearch"
                    placeholder="Filter tags…"
                    class="h-8 text-xs"
                  />
                </div>
                <div class="max-h-64 overflow-y-auto overscroll-contain p-2">
                  <p v-if="!overflowTags.length" class="px-1 py-2 text-xs text-muted-foreground">
                    No tags match “{{ tagSearch.trim() }}”.
                  </p>
                  <div v-else class="flex flex-wrap gap-1.5">
                    <button
                      v-for="t in overflowTags"
                      :key="t.tag"
                      type="button"
                      :class="[
                        'rounded-full border px-2.5 py-0.5 text-xs transition-colors',
                        tagFilter === t.tag
                          ? 'border-primary bg-primary/10 text-primary'
                          : 'border-border text-muted-foreground hover:bg-accent',
                      ]"
                      @click="tagFilter = tagFilter === t.tag ? '' : t.tag; tagPopoverOpen = false"
                    >
                      {{ t.tag }}
                      <span class="ml-1 tabular-nums opacity-60">{{ t.count }}</span>
                    </button>
                  </div>
                </div>
              </PopoverContent>
            </Popover>
          </template>
        </div>
      </div>

      <CardContent class="p-0">
        <!-- Loading -->
        <ul v-if="loading" class="divide-y px-4">
          <li v-for="i in 5" :key="i" class="grid grid-cols-[auto_1fr] gap-3 py-3">
            <Skeleton class="mt-1 h-4 w-4" />
            <div class="space-y-1.5">
              <Skeleton class="h-3 w-48" />
              <Skeleton class="h-4 w-2/3" />
              <Skeleton class="h-3 w-1/3" />
            </div>
          </li>
        </ul>

        <!-- Error -->
        <div v-else-if="error" class="p-4">
          <ErrorState
            title="Couldn't load findings"
            :message="error"
            class="border-0"
          >
            <Button variant="outline" size="sm" @click="reload">Retry</Button>
          </ErrorState>
        </div>

        <!-- Empty -->
        <div v-else-if="items.length === 0" class="p-4">
          <EmptyState
            :icon="Inbox"
            :title="emptyCopy.title"
            :description="emptyCopy.description"
            class="border-0"
          />
        </div>

        <!-- List -->
        <ul v-else class="stagger-children divide-y px-4">
          <li v-for="f in items" :key="f.uid" class="grid grid-cols-[auto_1fr] gap-3 py-3">
            <input
              type="checkbox"
              class="mt-2 h-4 w-4 cursor-pointer accent-primary"
              :checked="selected.has(f.uid)"
              @change="toggleOne(f.uid, ($event.target as HTMLInputElement).checked)"
            />
            <RouterLink
              :to="{ name: 'finding-detail', params: { uid: f.uid } }"
              class="-mx-2 block rounded-sm px-2 py-1 transition-colors hover:bg-accent"
            >
              <div class="flex flex-wrap items-center gap-1.5">
                <TrustBadge :finding="f" compact />
                <!-- Only outside the open view: there, every row is 'Open'. -->
                <Badge
                  v-if="statusFilter !== 'open'"
                  :variant="statusVariant(f.status)"
                  class="px-1.5 text-[10px]"
                >{{ statusLabel(f.status) }}</Badge>
                <Badge :variant="severityVariant(f.severity)" class="px-1.5 text-[10px]">{{ f.severity }}</Badge>
                <Badge variant="outline" class="px-1.5 text-[10px]">{{ f.kind }}</Badge>
                <!-- A deterministic analyzer matched — categorically stronger
                     evidence than an LLM assertion, so it gets its own chip. -->
                <Badge
                  v-if="f.detected_by_tool"
                  variant="info"
                  class="px-1.5 text-[10px]"
                  :title="f.detected_by_rule ? `rule ${f.detected_by_rule}` : undefined"
                >
                  <Wrench class="size-2.5" />
                  {{ f.detected_by_tool }}
                </Badge>
                <span v-if="f.subtype" class="font-mono text-[10px] uppercase text-muted-foreground">{{ f.subtype }}</span>
                <!-- Capped at 3 — a heavily-tagged finding would otherwise push
                     its own title off the row. Full list is on the detail page. -->
                <span
                  v-for="t in (f.tags || []).slice(0, 3)"
                  :key="t"
                  class="rounded-full border px-1.5 py-0 text-[10px] text-muted-foreground"
                >
                  {{ t }}
                </span>
                <span
                  v-if="(f.tags || []).length > 3"
                  class="text-[10px] text-muted-foreground"
                  :title="(f.tags || []).join(', ')"
                >
                  +{{ (f.tags || []).length - 3 }}
                </span>
                <span v-if="f.created_at" class="ml-auto text-[10px] text-muted-foreground">
                  found {{ formatRelativeTime(f.created_at) }}
                </span>
              </div>
              <div class="mt-1 font-medium">{{ f.title }}</div>
              <div class="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
                <span
                  v-if="(f.affected_paths || []).length"
                  class="inline-flex min-w-0 max-w-full items-center gap-1"
                >
                  <FileCode2 class="size-3 shrink-0" />
                  <span class="truncate font-mono">{{ (f.affected_paths || [])[0] }}</span>
                  <span v-if="(f.affected_paths || []).length > 1" class="shrink-0">
                    +{{ (f.affected_paths || []).length - 1 }} more
                  </span>
                </span>
                <span
                  v-if="corroborationCount(f) > 1"
                  class="inline-flex items-center gap-1 text-good"
                  title="Independent runs that filed or re-confirmed this finding"
                >
                  <Repeat2 class="size-3 shrink-0" />
                  confirmed by {{ corroborationCount(f) }} runs
                </span>
                <span>{{ f.executor }}</span>
              </div>
            </RouterLink>
          </li>
        </ul>
      </CardContent>
    </Card>

    <!-- Floating bulk-action bar — appears while a selection exists -->
    <div class="pointer-events-none fixed inset-x-0 bottom-6 z-40 flex justify-center px-4">
      <Transition
        enter-active-class="transition duration-200 ease-out"
        enter-from-class="translate-y-3 opacity-0"
        enter-to-class="translate-y-0 opacity-100"
        leave-active-class="transition duration-150 ease-in"
        leave-from-class="translate-y-0 opacity-100"
        leave-to-class="translate-y-3 opacity-0"
      >
        <div
          v-if="selected.size > 0"
          class="pointer-events-auto flex max-w-full flex-wrap items-center gap-1.5 rounded-2xl border bg-popover p-2 pl-4 text-popover-foreground shadow-lg"
        >
          <span class="text-sm font-medium tabular-nums">{{ selected.size }} selected</span>
          <Separator orientation="vertical" class="mx-1.5 h-5" />
          <Button size="sm" :loading="bulkBusy === 'tickets'" :disabled="!!bulkBusy" @click="askConfirm('tickets')">
            <SquareKanban /> Create tickets
          </Button>
          <Button variant="outline" size="sm" :loading="bulkBusy === 'dismiss'" :disabled="!!bulkBusy" @click="askConfirm('dismiss')">
            <Archive /> Dismiss
          </Button>
          <Button variant="destructive" size="sm" :loading="bulkBusy === 'delete'" :disabled="!!bulkBusy" @click="askConfirm('delete')">
            <Trash2 /> Delete
          </Button>
          <Button variant="ghost" size="icon-sm" title="Clear selection" :disabled="!!bulkBusy" @click="selected = new Set()">
            <X />
          </Button>
        </div>
      </Transition>
    </div>

    <!-- Bulk action confirmation -->
    <AlertDialog v-model:open="confirmOpen">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{{ confirmCopy.title }}</AlertDialogTitle>
          <AlertDialogDescription>{{ confirmCopy.description }}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            :class="confirmAction === 'delete' ? 'bg-destructive text-destructive-foreground hover:bg-destructive/90' : ''"
            @click="runConfirmedAction()"
          >
            {{ confirmCopy.cta }}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>

    <!-- File a finding by hand -->
    <FindingEditDialog v-model:open="createOpen" :create-repository-uid="repoUid || ''" @saved="onFiled" />
  </div>
</template>
