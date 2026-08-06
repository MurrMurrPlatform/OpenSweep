<script setup lang="ts">
import { computed, ref, onMounted, watch } from 'vue'
import { RouterLink } from 'vue-router'
import {
  CalendarClock,
  Check,
  ChevronDown,
  ChevronRight,
  FolderTree,
  HelpCircle,
  Map as MapIcon,
  Radar,
  Search,
  TriangleAlert,
  X,
} from 'lucide-vue-next'
import { useAreaStore } from '@/stores/areaStore'
import { useCampaignStore } from '@/stores/campaignStore'
import { useDocStore } from '@/stores/docStore'
import { useAnalysisStore, type AnalysisDTO } from '@/stores/analysisStore'
import { useScheduledAgentStore } from '@/stores/scheduledAgentStore'
import { useCurrentRepo } from '@/composables/useCurrentRepo'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { daysAgo } from '@/lib/utils'
import { AREA_KIND_HELP, areaStaleTitle } from '@/lib/areas'
import { buildTreeRows } from '@/lib/treeRows'
import type { TreeRow } from '@/lib/treeRows'
import AreaEditReviewCard from '@/components/areas/AreaEditReviewCard.vue'
import AreaHealthCells from '@/components/areas/AreaHealthCells.vue'
import CronScheduleInput from '@/components/agents/CronScheduleInput.vue'
import { MarkdownView } from '@/components/ui/markdown'
import { PageHeader } from '@/components/ui/page-header'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Button } from '@/components/ui/button'
import { Badge, type BadgeVariants } from '@/components/ui/badge'
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
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import { Skeleton } from '@/components/ui/skeleton'
import { AnimatedNumber } from '@/components/ui/animated-number'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import type {
  AreaDTO,
  AreaEditDTO,
  AreaHealthRowDTO,
  AreasHealthDTO,
  Autonomy,
  CampaignAreasPreview,
  ScheduledAgentDTO,
} from '@/types/api'

const areaStore = useAreaStore()
const campaignStore = useCampaignStore()
const docs = useDocStore()
const analyses = useAnalysisStore()
const scheduledAgents = useScheduledAgentStore()
const toast = useToast()
const { uid: repoUid, slug: repoSlug } = useCurrentRepo()

const loading = ref(true)
const error = ref<string | null>(null)
const latestAnalysis = ref<AnalysisDTO | null>(null)

// ── Load ─────────────────────────────────────────────────────────────────────

/** Best-effort partition drift (campaign-areas preview) — null hides the strip. */
const preview = ref<CampaignAreasPreview | null>(null)

/** Review / docs / audit state per area — the folded-in Health page. */
const health = ref<AreasHealthDTO | null>(null)

async function reload() {
  const target = repoUid.value
  if (!target) return
  loading.value = true
  error.value = null
  // The drift strip is best-effort: any preview failure just hides it.
  preview.value = null
  void campaignStore
    .fetchAreas(target)
    .then((p) => { if (repoUid.value === target) preview.value = p })
    .catch(() => { if (repoUid.value === target) preview.value = null })
  // The latest deep-scan report link is decorative — never block the board.
  void analyses
    .latestForRepo(target)
    .then((a) => { if (repoUid.value === target) latestAnalysis.value = a })
    .catch(() => { if (repoUid.value === target) latestAnalysis.value = null })
  void scheduledAgents
    .fetchAll(target)
    .then((all) => {
      if (repoUid.value !== target) return
      auditScheduleBinding.value = all.find((s) => s.agent_key === 'audit-stale') ?? null
    })
    .catch(() => { if (repoUid.value === target) auditScheduleBinding.value = null })
  try {
    const [, , h] = await Promise.all([
      areaStore.fetchAreas(target),
      areaStore.fetchEdits(target, 'pending'),
      areaStore.fetchHealth(target),
    ])
    if (repoUid.value === target) health.value = h
  } catch (e: unknown) {
    if (repoUid.value === target) error.value = e instanceof Error ? e.message : String(e)
  } finally {
    if (repoUid.value === target) loading.value = false
  }
}

onMounted(reload)
watch(repoUid, reload)

// ── Map areas (one LLM run proposing the whole tree) ─────────────────────────

const mapping = ref(false)

async function mapAreas() {
  if (!repoUid.value || mapping.value) return
  mapping.value = true
  try {
    const result = await areaStore.mapNow(repoUid.value)
    if (!result.run_uid) {
      // The endpoint resolves 200 with errors captured on the result when
      // dispatch fails (no provider, etc.) — that is a failure, not success.
      toast.error('Map areas failed', result.errors?.join('; ') || result.summary)
      return
    }
    toast.success(
      'Map areas dispatched',
      result.summary || (result.run_uid ? `run ${result.run_uid.slice(0, 8)}` : undefined),
      result.run_uid ? { label: 'View run', to: { name: 'run-detail', params: { uid: result.run_uid } } } : undefined,
    )
  } catch (e: unknown) {
    if (e instanceof ApiError && e.status === 409) {
      toast.warn('Map areas is already running', 'One mapping run per repository at a time — review its proposals when it lands.')
    } else {
      const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
      toast.error('Map areas failed', msg)
    }
  } finally {
    mapping.value = false
  }
}

// ── Upkeep state per area (review / docs / audit) ────────────────────────────

const healthByUid = computed<Map<string, AreaHealthRowDTO>>(
  () => new Map((health.value?.rows ?? []).map((r) => [r.uid, r])),
)

/** Stale leaves under every key PREFIX, including synthetic group headers.
 *
 *  The backend's per-row `stale_descendants` only exists for keys that are
 *  real Area nodes, and group headers are emitted precisely when no area owns
 *  that prefix — so reading the row field directly returns 0 for exactly the
 *  rows that need the number. Rolled up once per health load instead of
 *  rescanning every row on every render. */
const staleByPrefix = computed<Map<string, number>>(() => {
  const out = new Map<string, number>()
  for (const r of health.value?.rows ?? []) {
    if (!r.stale) continue
    const parts = r.key.split('/')
    for (let i = 1; i < parts.length; i++) {
      const prefix = parts.slice(0, i).join('/')
      out.set(prefix, (out.get(prefix) ?? 0) + 1)
    }
  }
  return out
})

function healthFor(uid: string): AreaHealthRowDTO | null {
  return healthByUid.value.get(uid) ?? null
}

/** Stale leaves under a key — what a grouping row reports instead of its own
 *  staleness (a grouping owns no paths, so it can never itself be stale). */
function staleUnder(key: string): number {
  return staleByPrefix.value.get(key) ?? 0
}

function outcomeVariant(outcome: string): BadgeVariants['variant'] {
  if (outcome === 'clean') return 'success'
  if (outcome === 'findings') return 'info'
  if (outcome === 'failed') return 'destructive'
  return 'secondary'
}

const summary = computed(
  () =>
    health.value?.summary ?? {
      total: 0,
      untracked: 0,
      stale: 0,
      never_audited: 0,
      fresh: 0,
    },
)

/** Confirm-current: clear stale after eyeballing an area, without inventing a
 *  no-op edit just to move the badge. */
const confirmingUid = ref('')

async function confirmCurrent(area: AreaDTO) {
  if (confirmingUid.value) return
  confirmingUid.value = area.uid
  try {
    await areaStore.confirmCurrent(area.uid)
    if (repoUid.value) health.value = await areaStore.fetchHealth(repoUid.value)
    toast.success('Marked reviewed', `${area.key} is current as of now`)
  } catch (e: unknown) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t confirm', msg)
  } finally {
    confirmingUid.value = ''
  }
}

// ── Audit dispatch (staleness-driven or whole repo) ──────────────────────────

const auditOpen = ref(false)
const auditing = ref(false)
/** true = pick the stalest / never-checked pages automatically. */
const auditAutoSelect = ref(true)
const auditLimit = ref('3')
const auditMaxFindings = ref('')
const auditIntent = ref('')

function openAudit() {
  auditAutoSelect.value = true
  auditOpen.value = true
}

async function dispatchAudit() {
  if (!repoUid.value || auditing.value) return
  auditing.value = true
  try {
    const budget = Number.parseInt(auditMaxFindings.value, 10)
    const limit = Number.parseInt(auditLimit.value, 10)
    const result = await docs.audit(repoUid.value, [], {
      auto_select: auditAutoSelect.value,
      limit: Number.isFinite(limit) && limit > 0 ? limit : 3,
      custom_intent: auditIntent.value.trim() || undefined,
      max_findings: Number.isFinite(budget) && budget > 0 ? budget : undefined,
    })
    auditOpen.value = false
    auditIntent.value = ''
    const picked = result.selected.length
      ? `picked: ${result.selected.map((s) => s.slug).join(', ')}`
      : null
    if (result.runs_dispatched.length === 0 && result.errors.length === 0) {
      toast.success('Nothing to audit', result.summary)
    } else if (result.errors.length) {
      toast.error('Audit partially dispatched', result.errors.join(' · '))
    } else {
      toast.success(result.summary, picked ?? undefined)
    }
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t dispatch audit', msg)
  } finally {
    auditing.value = false
  }
}

// ── Deep scan (one long whole-repo sweep: plan → sweep → synthesize) ─────────

const deepScanOpen = ref(false)
const deepScanning = ref(false)
const deepScanIntent = ref('')
const deepScanMaxFindings = ref('')

async function dispatchDeepScan() {
  if (!repoUid.value || deepScanning.value) return
  deepScanning.value = true
  try {
    const budget = Number.parseInt(deepScanMaxFindings.value, 10)
    const result = await docs.deepScan(repoUid.value, {
      custom_intent: deepScanIntent.value.trim() || undefined,
      max_findings: Number.isFinite(budget) && budget > 0 ? budget : undefined,
    })
    deepScanOpen.value = false
    deepScanIntent.value = ''
    deepScanMaxFindings.value = ''
    if (result.errors.length) {
      toast.error('Deep scan not dispatched', result.errors.join(' · '))
    } else {
      toast.success(result.summary, 'One long run — findings land as it works through the repo.')
    }
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t start deep scan', msg)
  } finally {
    deepScanning.value = false
  }
}

// ── Scheduled audits — edits the repo's seeded "Audit stale code" binding ────

const scheduleOpen = ref(false)
const scheduleLoading = ref(false)
const savingSchedule = ref(false)
const auditScheduleBinding = ref<ScheduledAgentDTO | null>(null)

type ScheduleMode = 'manual' | 'cron'
const scheduleMode = ref<ScheduleMode>('manual')
const cronExpr = ref('')
const dial = ref<Autonomy>('ask-before-run')
const schedulePagesPerTick = ref('3')

const SCHEDULE_OPTIONS = [
  { label: 'Off — audit only when I click', value: 'manual' },
  { label: 'On a schedule (cron)', value: 'cron' },
]

const DIAL_OPTIONS = [
  { label: 'Disabled — kill switch, never runs', value: 'disabled' },
  { label: 'Ask before run', value: 'ask-before-run' },
  { label: 'Auto-run on free (local) compute', value: 'auto-run-cheap' },
  { label: 'Auto-run on any provider', value: 'auto-run-any' },
]

const scheduleSummary = computed(() => {
  const sa = auditScheduleBinding.value
  if (!sa || !sa.trigger.startsWith('cron:')) return null
  if (sa.autonomy === 'disabled') return 'scheduled · disabled'
  return `cron ${sa.trigger.slice('cron:'.length)}`
})

async function openSchedule() {
  if (!repoUid.value) return
  scheduleOpen.value = true
  scheduleLoading.value = true
  try {
    const all = await scheduledAgents.fetchAll(repoUid.value)
    const sa = all.find((s) => s.agent_key === 'audit-stale') ?? null
    auditScheduleBinding.value = sa
    if (sa?.trigger.startsWith('cron:')) {
      scheduleMode.value = 'cron'
      cronExpr.value = sa.trigger.slice('cron:'.length)
    } else {
      scheduleMode.value = 'manual'
      cronExpr.value = ''
    }
    dial.value = sa?.autonomy ?? 'ask-before-run'
    const rawLimit = sa?.target?.limit
    schedulePagesPerTick.value = String(
      typeof rawLimit === 'number' && rawLimit > 0 ? rawLimit : 3,
    )
  } catch (e) {
    toast.error('Couldn’t load audit schedule', e instanceof Error ? e.message : String(e))
    scheduleOpen.value = false
  } finally {
    scheduleLoading.value = false
  }
}

async function saveSchedule() {
  if (!repoUid.value || savingSchedule.value) return
  if (scheduleMode.value === 'cron' && !cronExpr.value.trim()) {
    toast.error('Cron expression required', 'Pick a preset or enter a 5-field crontab.')
    return
  }
  savingSchedule.value = true
  try {
    const limit = Number.parseInt(schedulePagesPerTick.value, 10)
    const payload = {
      trigger: scheduleMode.value === 'cron' ? `cron:${cronExpr.value.trim()}` : '',
      autonomy: dial.value,
      target: { limit: Number.isFinite(limit) && limit > 0 ? limit : 3 },
    }
    if (!auditScheduleBinding.value) {
      // The seeded binding is created on repo registration; if it is missing
      // the backend seeding hasn't run — surface that instead of guessing.
      throw new Error('Seeded "Audit stale code" binding not found for this repository')
    }
    auditScheduleBinding.value = await scheduledAgents.update(
      auditScheduleBinding.value.uid,
      payload,
    )
    scheduleOpen.value = false
    toast.success(
      scheduleMode.value === 'cron' ? 'Scheduled audits on' : 'Scheduled audits off',
      scheduleMode.value === 'cron'
        ? `cron ${cronExpr.value.trim()} · up to ${payload.target.limit} stalest pages per tick`
        : undefined,
    )
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t save schedule', msg)
  } finally {
    savingSchedule.value = false
  }
}

// ── Drift strip (partition drift, from the campaign-areas preview) ───────────

const n = (x: number) => x.toLocaleString('en-US')

/** Areas whose every scope path matches nothing — they audit thin air. */
const deadAreaCount = computed(() => {
  const p = preview.value
  if (!p) return 0
  return p.areas.filter(
    (a) => a.scope_paths.length > 0 && a.dead_scope_paths.length === a.scope_paths.length,
  ).length
})

const hasDrift = computed(() => {
  const p = preview.value
  if (!p) return false
  return (
    p.uncovered_files > 0 ||
    deadAreaCount.value > 0 ||
    p.oversized_areas.length > 0 ||
    p.overlapping_files > 0 ||
    p.dead_ignore_scopes.length > 0
  )
})

// ── Sections: Partition (subsystem tree) / Features / Ignored ────────────────

const sortedAreas = computed<AreaDTO[]>(() => [...areaStore.areas].sort((a, b) => a.key.localeCompare(b.key)))
const subsystems = computed(() => sortedAreas.value.filter((a) => a.kind === 'subsystem'))
const features = computed(() => sortedAreas.value.filter((a) => a.kind === 'feature'))
// Both axes' ignore kinds share the section — they are the same idea, one per axis.
const ignored = computed(() =>
  sortedAreas.value.filter((a) => a.kind === 'ignore' || a.kind === 'feature_ignore'),
)

/** Feature areas rendered as a hierarchy (shared tree helper). */
const featureTreeRows = computed<TreeRow<AreaDTO>[]>(() =>
  buildTreeRows(features.value, (a) => a.key),
)

/** area_key → file_count. The health rollup is authoritative (it sizes every
 *  area against the tree in one pass); the campaign preview is the fallback
 *  while health is still loading. */
const fileCountByKey = computed<Map<string, number | null>>(() => {
  const m = new Map<string, number | null>()
  for (const a of preview.value?.areas ?? []) if (a.area_key) m.set(a.area_key, a.file_count)
  for (const r of health.value?.rows ?? []) m.set(r.key, r.file_count)
  return m
})

interface PartitionRow {
  type: 'group' | 'area'
  key: string
  /** Last key segment (what the row shows). */
  name: string
  depth: number
  area?: AreaDTO
  /** Leaf: its own file count. Group: rollup of all descendants. */
  fileTotal: number | null
}

function groupFileTotal(prefix: string): number | null {
  const counts = fileCountByKey.value
  let total = 0
  let any = false
  for (const a of subsystems.value) {
    if (a.key !== prefix && !a.key.startsWith(prefix + '/')) continue
    const c = counts.get(a.key)
    if (typeof c === 'number') {
      total += c
      any = true
    }
  }
  return any ? total : null
}

/** The subsystem tree: leaves under implicit group headers derived from key
 *  segments. A prefix that is itself an area renders as an area row; missing
 *  intermediate prefixes get a synthetic group row with a descendant rollup. */
const partitionRows = computed<PartitionRow[]>(() => {
  const areaKeys = new Set(subsystems.value.map((a) => a.key))
  const emittedGroups = new Set<string>()
  const rows: PartitionRow[] = []
  for (const a of subsystems.value) {
    const segments = a.key.split('/')
    for (let i = 1; i < segments.length; i++) {
      const prefix = segments.slice(0, i).join('/')
      if (areaKeys.has(prefix) || emittedGroups.has(prefix)) continue
      emittedGroups.add(prefix)
      rows.push({ type: 'group', key: prefix, name: segments[i - 1], depth: i - 1, fileTotal: groupFileTotal(prefix) })
    }
    rows.push({
      type: 'area',
      key: a.key,
      name: segments[segments.length - 1],
      depth: segments.length - 1,
      area: a,
      fileTotal: fileCountByKey.value.get(a.key) ?? null,
    })
  }
  return rows
})

function indent(depth: number) {
  return { paddingLeft: `${12 + depth * 20}px` }
}

// ── Tabs + collapse/expand of the subsystem tree ─────────────────────────────

const activeAreaTab = ref<'subsystems' | 'features' | 'ignored'>('subsystems')

const collapsedKeys = ref<Set<string>>(new Set())

function toggleCollapse(key: string) {
  const next = new Set(collapsedKeys.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  collapsedKeys.value = next
}

const collapsedFeatureKeys = ref<Set<string>>(new Set())

function toggleFeatureCollapse(key: string) {
  const next = new Set(collapsedFeatureKeys.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  collapsedFeatureKeys.value = next
}

/** A row (group or area) has children when any subsystem key nests under it. */
function hasChildren(key: string): boolean {
  return subsystems.value.some((a) => a.key.startsWith(key + '/'))
}

/** A feature row has children when any other feature key nests under it. */
function hasFeatureChildren(key: string): boolean {
  return features.value.some((a) => a.key.startsWith(key + '/'))
}

/** Rows whose ancestor prefix is collapsed are hidden. */
const visiblePartitionRows = computed(() =>
  partitionRows.value.filter((row) => {
    const segments = row.key.split('/')
    for (let i = 1; i < segments.length; i++) {
      if (collapsedKeys.value.has(segments.slice(0, i).join('/'))) return false
    }
    return true
  }),
)

/** Feature tree rows with collapsed ancestors filtered out. */
const visibleFeatureTreeRows = computed(() =>
  featureTreeRows.value.filter((row) => {
    const segments = row.key.split('/')
    for (let i = 1; i < segments.length; i++) {
      if (collapsedFeatureKeys.value.has(segments.slice(0, i).join('/'))) return false
    }
    return true
  }),
)

// Collapsible spec previews, keyed by uid.
const expandedSpecs = ref<Set<string>>(new Set())

function toggleSpec(uid: string) {
  const next = new Set(expandedSpecs.value)
  if (next.has(uid)) next.delete(uid)
  else next.add(uid)
  expandedSpecs.value = next
}

/** First non-empty spec line, de-markdown'd — the inline "why ignored" reason. */
function specSummary(a: AreaDTO): string {
  const line = a.spec.split('\n').find((l) => l.trim())
  return (line ?? '').replace(/^#+\s*/, '').trim()
}

// ── Reset (destructive: wipe the whole map) ──────────────────────────────────

const resetOpen = ref(false)
const resetting = ref(false)

async function confirmReset() {
  if (!repoUid.value) return
  resetOpen.value = false
  resetting.value = true
  try {
    const result = await areaStore.resetAll(repoUid.value)
    toast.success(
      'Area map deleted',
      `${result.areas_deleted} area${result.areas_deleted === 1 ? '' : 's'} and ${result.edits_deleted} edit${result.edits_deleted === 1 ? '' : 's'} removed. Campaigns plan from docs until a new map is accepted.`,
    )
  } catch (e: unknown) {
    toast.error('Reset failed', e instanceof Error ? e.message : String(e))
  } finally {
    resetting.value = false
  }
}

// ── Pending edit review ──────────────────────────────────────────────────────

const resolvingUid = ref('')

async function acceptEdit(edit: AreaEditDTO) {
  if (resolvingUid.value) return
  resolvingUid.value = edit.uid
  try {
    const { area, warnings } = await areaStore.acceptEdit(edit.uid)
    if (!area) {
      // A retirement of an area that was never mapped: nothing to create.
      toast.success('Edit accepted', `${edit.key} was not mapped — nothing created`)
    } else if (warnings.length) {
      // Warnings are advisory (partition overlaps, missing ignore reasons) —
      // the edit IS applied; surface them as a warning, never an error.
      toast.warn(`Accepted ${area.key} with warnings`, warnings.join('; '))
    } else {
      toast.success('Edit accepted', area.key)
    }
  } catch (e: unknown) {
    toast.error('Accept failed', e instanceof Error ? e.message : String(e))
  } finally {
    resolvingUid.value = ''
  }
}

async function rejectEdit(edit: AreaEditDTO) {
  if (resolvingUid.value) return
  resolvingUid.value = edit.uid
  try {
    await areaStore.rejectEdit(edit.uid)
    toast.success('Edit rejected')
  } catch (e: unknown) {
    toast.error('Reject failed', e instanceof Error ? e.message : String(e))
  } finally {
    resolvingUid.value = ''
  }
}

const bulkResolving = ref<'accept' | 'reject' | null>(null)
const bulkResolveOpen = ref(false)
const pendingBulkAction = ref<'accept' | 'reject' | null>(null)

function resolveAll(action: 'accept' | 'reject') {
  if (!areaStore.edits.length || bulkResolving.value) return
  pendingBulkAction.value = action
  bulkResolveOpen.value = true
}

async function confirmResolveAll() {
  const action = pendingBulkAction.value
  const uids = areaStore.edits.map((e) => e.uid)
  if (!action || !uids.length) return
  bulkResolveOpen.value = false
  bulkResolving.value = action
  try {
    const result = action === 'accept' ? await areaStore.bulkAccept(uids) : await areaStore.bulkReject(uids)
    const errorEntries = Object.entries(result.errors ?? {})
    const warningEntries = Object.entries(result.warnings ?? {})
    // Errors and warnings are independent: a batch can have both (some
    // edits failed, others accepted with partition warnings) — show each.
    if (errorEntries.length) {
      toast.warn(
        `Some edits failed to ${action}`,
        errorEntries.map(([uid, msg]) => `${uid.slice(0, 8)}: ${msg}`).join('; '),
      )
    }
    if (warningEntries.length) {
      toast.warn(
        `Accepted ${result.accepted?.length ?? uids.length} edits with warnings`,
        warningEntries.map(([uid, ws]) => `${uid.slice(0, 8)}: ${ws.join('; ')}`).join(' · '),
      )
    }
    if (!errorEntries.length && !warningEntries.length) {
      toast.success(action === 'accept' ? `Accepted ${uids.length} edits` : `Rejected ${uids.length} edits`)
    }
    await reload()
  } catch (e: unknown) {
    toast.error(`Bulk ${action} failed`, e instanceof Error ? e.message : String(e))
  } finally {
    bulkResolving.value = null
  }
}
</script>

<template>
  <div class="space-y-4">
    <PageHeader
      title="Areas"
      subtitle="The reviewed audit partition: path-keyed areas that carry what to check where, with how current each one is. Stale = code moved under the scope since the last review; it clears on review, not on an audit."
    >
      <div class="flex flex-wrap items-center gap-2">
        <Button
          v-if="areaStore.areas.length || areaStore.edits.length"
          size="sm"
          variant="outline"
          class="text-destructive hover:text-destructive"
          :loading="resetting"
          title="Delete every area and pending edit for this repository."
          @click="resetOpen = true"
        >
          Reset map…
        </Button>
        <Button variant="outline" size="sm" :disabled="!repoUid" @click="openSchedule">
          <CalendarClock />
          Scheduled audits
          <Badge v-if="scheduleSummary" class="ml-1 px-1.5 text-[10px]">{{ scheduleSummary }}</Badge>
        </Button>
        <RouterLink
          v-if="latestAnalysis"
          :to="{ name: 'analysis-detail', params: { uid: latestAnalysis.uid } }"
          class="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs transition-colors hover:bg-accent"
          title="Open the latest deep-scan report"
        >
          Latest report
          <Badge v-if="latestAnalysis.health_grade" class="px-1.5 text-[10px]">{{ latestAnalysis.health_grade }}</Badge>
        </RouterLink>
        <Button variant="outline" size="sm" :disabled="!repoUid" @click="deepScanOpen = true">
          <Radar /> Deep scan
        </Button>
        <Button variant="outline" size="sm" :disabled="!repoUid" @click="openAudit()">
          <Search /> Audit
        </Button>
        <Button
          size="sm"
          :loading="mapping"
          title="Dispatch one LLM run that walks the repository and proposes the area map. Proposals land below as pending edits."
          @click="mapAreas"
        >
          <MapIcon v-if="!mapping" />
          Map areas
        </Button>
      </div>
    </PageHeader>

    <Dialog v-model:open="deepScanOpen">
      <DialogContent class="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Deep scan — whole repository</DialogTitle>
          <DialogDescription>
            One long run that plans its own scan, then works through the whole codebase area by area — correctness, security, tests, simplifications, performance — filing findings as it goes. Best on a deep-effort provider; only one runs per repo at a time.
          </DialogDescription>
        </DialogHeader>
        <div class="space-y-4">
          <div class="space-y-1.5">
            <Label for="deep-budget">Finding budget (optional)</Label>
            <Input id="deep-budget" v-model="deepScanMaxFindings" type="number" min="1" max="200" placeholder="no cap" />
            <p class="text-xs text-muted-foreground">
              Cap total findings for the whole scan, ranked by severity × confidence. Empty = find everything defensible.
            </p>
          </div>
          <div class="space-y-1.5">
            <Label for="deep-focus">Focus (optional)</Label>
            <Textarea
              id="deep-focus"
              v-model="deepScanIntent"
              :rows="2"
              placeholder="e.g. weight the scan toward security and the multi-tenancy boundaries"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="ghost" @click="deepScanOpen = false">Cancel</Button>
          <Button :loading="deepScanning" @click="dispatchDeepScan">
            <Radar /> Start deep scan
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog v-model:open="scheduleOpen">
      <DialogContent class="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Scheduled audits</DialogTitle>
          <DialogDescription>
            Automatically audit what most needs a look — never-checked first, then longest-stale. One scoped run per target.
          </DialogDescription>
        </DialogHeader>
        <div v-if="scheduleLoading" class="space-y-3">
          <Skeleton class="h-9" />
          <Skeleton class="h-9" />
        </div>
        <div v-else class="space-y-4">
          <div class="space-y-1.5">
            <Label>Trigger</Label>
            <Select
              :model-value="scheduleMode"
              @update:model-value="scheduleMode = $event as ScheduleMode"
            >
              <SelectTrigger class="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem v-for="o in SCHEDULE_OPTIONS" :key="o.value" :value="o.value">
                  {{ o.label }}
                </SelectItem>
              </SelectContent>
            </Select>
          </div>
          <template v-if="scheduleMode === 'cron'">
            <div class="space-y-1.5">
              <Label>Schedule</Label>
              <CronScheduleInput v-model="cronExpr" />
            </div>
            <div class="grid gap-3 sm:grid-cols-2">
              <div class="space-y-1.5">
                <Label for="pages-per-tick">Targets per tick</Label>
                <Input id="pages-per-tick" v-model="schedulePagesPerTick" type="number" min="1" max="20" />
              </div>
              <div class="space-y-1.5">
                <Label>Compute</Label>
                <Select
                  :model-value="dial"
                  @update:model-value="dial = $event as Autonomy"
                >
                  <SelectTrigger class="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem v-for="o in DIAL_OPTIONS" :key="o.value" :value="o.value">
                      {{ o.label }}
                    </SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
            <p class="text-xs text-muted-foreground">
              When everything is fresh, a tick dispatches nothing. “Auto-run on free compute”
              makes the whole loop cost nothing on a local provider.
            </p>
          </template>
        </div>
        <DialogFooter>
          <Button variant="ghost" @click="scheduleOpen = false">Cancel</Button>
          <Button :loading="savingSchedule" :disabled="scheduleLoading" @click="saveSchedule">
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog v-model:open="auditOpen">
      <DialogContent class="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Audit repository</DialogTitle>
          <DialogDescription>
            Dispatch scoped audit runs against the code that most needs a look.
          </DialogDescription>
        </DialogHeader>
        <div class="space-y-4">
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0">
              <p class="text-sm font-medium">Auto-select stale targets</p>
              <p class="text-xs text-muted-foreground">
                Pick what most needs a look: never-checked first, then longest-stale.
                Off = one whole-repository run with no scoping.
              </p>
            </div>
            <Switch v-model="auditAutoSelect" class="mt-0.5 shrink-0" />
          </div>
          <div v-if="auditAutoSelect" class="space-y-1.5">
            <Label for="audit-limit">Targets per audit</Label>
            <Input id="audit-limit" v-model="auditLimit" type="number" min="1" max="20" placeholder="3" />
            <p class="text-xs text-muted-foreground">Each selected target gets its own scoped run.</p>
          </div>
          <div class="space-y-1.5">
            <Label for="audit-budget">Finding budget per run</Label>
            <Input id="audit-budget" v-model="auditMaxFindings" type="number" min="1" max="50" placeholder="no cap" />
            <p class="text-xs text-muted-foreground">
              Cap findings per run, ranked by severity × confidence. Empty = find everything defensible.
            </p>
          </div>
          <div class="space-y-1.5">
            <Label for="audit-focus">Focus (optional)</Label>
            <Textarea
              id="audit-focus"
              v-model="auditIntent"
              :rows="2"
              placeholder="e.g. focus on security of the auth flows"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="ghost" @click="auditOpen = false">Cancel</Button>
          <Button :loading="auditing" @click="dispatchAudit">
            <Search /> Dispatch audit
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <template v-if="loading">
      <div class="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Skeleton v-for="i in 4" :key="i" class="h-[72px]" />
      </div>
      <Skeleton class="h-24" />
      <Skeleton class="h-64" />
    </template>

    <ErrorState v-else-if="error" title="Couldn't load areas" :message="error">
      <Button variant="outline" size="sm" @click="reload">Retry</Button>
    </ErrorState>

    <template v-else>
      <!-- ── Upkeep tiles, over auditable leaves (groupings own no files) ── -->
      <!-- The five tiles partition `total`: every auditable leaf is in exactly
           one. They used to overlap, so they summed to more than Areas. -->
      <section v-if="health && summary.total" class="grid grid-cols-2 gap-3 text-sm lg:grid-cols-5">
        <Card>
          <CardContent class="p-3">
            <div class="text-xs uppercase tracking-wide text-muted-foreground">Areas</div>
            <div class="mt-1 text-xl font-semibold tabular-nums"><AnimatedNumber :value="summary.total" /></div>
          </CardContent>
        </Card>
        <Card>
          <CardContent class="p-3">
            <div class="text-xs uppercase tracking-wide text-muted-foreground">Fresh</div>
            <div class="mt-1 text-xl font-semibold tabular-nums text-good"><AnimatedNumber :value="summary.fresh" /></div>
          </CardContent>
        </Card>
        <Card>
          <CardContent class="p-3">
            <div class="text-xs uppercase tracking-wide text-muted-foreground">Stale</div>
            <div class="mt-1 text-xl font-semibold tabular-nums text-warn"><AnimatedNumber :value="summary.stale" /></div>
          </CardContent>
        </Card>
        <Card>
          <CardContent class="p-3">
            <div
              class="text-xs uppercase tracking-wide text-muted-foreground"
              title="Tracked and current, but no audit has ever covered them. A leaf that is also stale counts under Stale — each area appears in exactly one tile."
            >
              Never audited
            </div>
            <div class="mt-1 text-xl font-semibold tabular-nums text-muted-foreground"><AnimatedNumber :value="summary.never_audited" /></div>
          </CardContent>
        </Card>
        <Card :class="summary.untracked ? 'border-warn/40' : ''">
          <CardContent class="p-3">
            <div
              class="text-xs uppercase tracking-wide text-muted-foreground"
              title="Leaves with no scope paths — nothing can mark them stale and no audit can cover them"
            >
              Untracked
            </div>
            <div
              class="mt-1 text-xl font-semibold tabular-nums"
              :class="summary.untracked ? 'text-warn' : 'text-muted-foreground'"
            ><AnimatedNumber :value="summary.untracked" /></div>
          </CardContent>
        </Card>
      </section>

      <!-- Freshness honesty: a board that can't see recent commits must say so
           rather than render a confident all-fresh green. -->
      <p
        v-if="health?.freshness_degraded_reason"
        class="flex items-center gap-1.5 text-xs text-warn"
      >
        <TriangleAlert class="h-3.5 w-3.5 shrink-0" />
        Staleness may be incomplete — {{ health.freshness_degraded_reason }}
      </p>
      <p v-else-if="health?.freshness_synced_at" class="text-xs text-muted-foreground">
        Staleness current as of {{ daysAgo(health.freshness_synced_at) }} (default branch).
      </p>

      <!-- ── Drift strip (drift between the map and the tree) ────────────── -->
      <Card v-if="preview && sortedAreas.length">
        <CardContent class="space-y-2 p-4">
          <template v-if="hasDrift">
            <div class="flex flex-wrap items-center gap-1.5">
              <span
                v-if="preview.uncovered_files > 0"
                class="inline-flex items-center gap-1 rounded-full border border-amber-500/40 bg-amber-500/10 px-2.5 py-0.5 text-xs text-amber-800 dark:text-amber-300"
              >
                <TriangleAlert class="h-3 w-3" />
                {{ n(preview.uncovered_files) }} file{{ preview.uncovered_files === 1 ? '' : 's' }} uncovered by the map
              </span>
              <span
                v-if="deadAreaCount > 0"
                class="inline-flex items-center gap-1 rounded-full border border-amber-500/40 bg-amber-500/10 px-2.5 py-0.5 text-xs text-amber-800 dark:text-amber-300"
              >
                <TriangleAlert class="h-3 w-3" />
                {{ deadAreaCount }} area{{ deadAreaCount === 1 ? '' : 's' }} match{{ deadAreaCount === 1 ? 'es' : '' }} no files
              </span>
              <span
                v-if="preview.oversized_areas.length"
                class="inline-flex items-center gap-1 rounded-full border border-amber-500/40 bg-amber-500/10 px-2.5 py-0.5 text-xs text-amber-800 dark:text-amber-300"
                :title="preview.oversized_areas.join('\n')"
              >
                {{ preview.oversized_areas.length }} oversized
              </span>
              <span
                v-if="preview.overlapping_files > 0"
                class="inline-flex items-center gap-1 rounded-full border border-amber-500/40 bg-amber-500/10 px-2.5 py-0.5 text-xs text-amber-800 dark:text-amber-300"
              >
                {{ n(preview.overlapping_files) }} file{{ preview.overlapping_files === 1 ? '' : 's' }} in overlapping leaves
              </span>
              <span
                v-if="preview.dead_ignore_scopes.length"
                class="inline-flex max-w-full items-center gap-1 rounded-full border border-amber-500/40 bg-amber-500/10 px-2.5 py-0.5 text-xs text-amber-800 dark:text-amber-300"
                :title="preview.dead_ignore_scopes.join('\n')"
              >
                <span class="truncate font-mono">dead ignore scopes: {{ preview.dead_ignore_scopes.join(', ') }}</span>
              </span>
            </div>
            <p class="text-xs text-muted-foreground">Run Map areas to let the agent fix the partition.</p>
          </template>
          <p v-else class="text-xs text-good">Map covers the tree — no drift detected.</p>
        </CardContent>
      </Card>

      <!-- ── Pending edits (agent proposals) ─────────────────────────────── -->
      <Card v-if="areaStore.edits.length">
        <CardHeader class="flex-row flex-wrap items-center justify-between gap-2 space-y-0">
          <CardTitle class="text-base">Pending edits</CardTitle>
          <div class="flex items-center gap-2">
            <span class="text-xs text-muted-foreground">{{ areaStore.edits.length }} awaiting review</span>
            <Button
              variant="outline"
              size="sm"
              :loading="bulkResolving === 'reject'"
              :disabled="!!bulkResolving || !!resolvingUid"
              @click="resolveAll('reject')"
            >
              <X /> Reject all
            </Button>
            <Button
              size="sm"
              :loading="bulkResolving === 'accept'"
              :disabled="!!bulkResolving || !!resolvingUid"
              @click="resolveAll('accept')"
            >
              <Check /> Accept all
            </Button>
          </div>
        </CardHeader>
        <CardContent class="p-0">
          <AreaEditReviewCard
            v-for="edit in areaStore.edits"
            :key="edit.uid"
            :edit="edit"
            :warnings="edit.warnings"
            :resolving="resolvingUid === edit.uid"
            :disabled="(!!resolvingUid && resolvingUid !== edit.uid) || !!bulkResolving"
            @accept="acceptEdit(edit)"
            @reject="rejectEdit(edit)"
          />
        </CardContent>
      </Card>

      <!-- ── Empty map ───────────────────────────────────────────────────── -->
      <Card v-if="!sortedAreas.length">
        <CardContent class="p-4">
          <EmptyState
            :icon="MapIcon"
            title="No areas yet"
            description="The area map is the reviewed partition campaigns audit against: path-keyed areas with scopes and specs. Click Map areas to let an agent propose it from the code — every proposal lands here for review."
            class="border-0"
          >
            <Button size="sm" :loading="mapping" @click="mapAreas">
              <MapIcon v-if="!mapping" />
              Map areas
            </Button>
          </EmptyState>
        </CardContent>
      </Card>

      <Tabs
        v-else
        :model-value="activeAreaTab"
        @update:model-value="activeAreaTab = $event as 'subsystems' | 'features' | 'ignored'"
      >
        <TabsList class="max-w-full overflow-x-auto">
          <TabsTrigger value="subsystems">
            Subsystems ({{ subsystems.length }})
          </TabsTrigger>
          <TabsTrigger value="features">
            Features ({{ features.length }})
          </TabsTrigger>
          <TabsTrigger value="ignored">
            Ignored ({{ ignored.length }})
          </TabsTrigger>
        </TabsList>

        <!-- ── Subsystems: the exclusive partition tree ──────────────────── -->
        <TabsContent value="subsystems" class="mt-3">
        <Card>
          <CardHeader class="flex-row items-center justify-between space-y-0">
            <CardTitle class="flex items-center gap-1.5 text-base">
              Partition
              <HelpCircle class="h-3.5 w-3.5 shrink-0 text-muted-foreground" :title="AREA_KIND_HELP.subsystem" />
            </CardTitle>
            <span class="text-xs text-muted-foreground">
              {{ subsystems.length }} subsystem area{{ subsystems.length === 1 ? '' : 's' }}
            </span>
          </CardHeader>
          <CardContent class="p-0">
            <div v-if="!subsystems.length" class="p-4 text-sm text-muted-foreground">
              No subsystem areas — the exclusive partition is empty.
            </div>
            <ul v-else class="divide-y divide-border">
              <li v-for="row in visiblePartitionRows" :key="`${row.type}:${row.key}`">
                <!-- Implicit parent: a group header derived from key segments -->
                <button
                  v-if="row.type === 'group'"
                  type="button"
                  class="flex w-full items-center gap-1.5 bg-muted/40 py-1.5 pr-3 text-left text-xs font-semibold text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground"
                  :style="indent(row.depth)"
                  :title="collapsedKeys.has(row.key) ? 'Expand' : 'Collapse'"
                  @click="toggleCollapse(row.key)"
                >
                  <component :is="collapsedKeys.has(row.key) ? ChevronRight : ChevronDown" class="h-3.5 w-3.5 shrink-0" />
                  <FolderTree class="h-3.5 w-3.5 shrink-0" />
                  <span class="truncate font-mono">{{ row.name }}/</span>
                  <!-- A grouping owns no paths and so can never itself be
                       stale; report its children instead of reading clean. -->
                  <Badge
                    v-if="staleUnder(row.key) > 0"
                    variant="warn"
                    class="ml-1 shrink-0 px-1.5 text-[10px] font-normal"
                    :title="`${staleUnder(row.key)} area(s) under this group need a review`"
                  >
                    {{ staleUnder(row.key) }} stale
                  </Badge>
                  <span v-if="row.fileTotal != null" class="ml-auto font-normal tabular-nums">
                    {{ n(row.fileTotal) }} files
                  </span>
                </button>

                <template v-else-if="row.area">
                  <RouterLink
                    :to="{ name: 'area-detail', params: { uid: row.area.uid } }"
                    class="flex items-start gap-2 py-2 pr-3 transition-colors hover:bg-accent/50"
                    :class="{ 'opacity-60': !row.area.enabled }"
                    :style="indent(row.depth)"
                  >
                    <button
                      v-if="hasChildren(row.key)"
                      type="button"
                      class="mt-0.5 rounded-sm p-1 text-muted-foreground hover:text-foreground"
                      :title="collapsedKeys.has(row.key) ? 'Expand children' : 'Collapse children'"
                      @click.stop.prevent="toggleCollapse(row.key)"
                    >
                      <component :is="collapsedKeys.has(row.key) ? ChevronRight : ChevronDown" class="h-3.5 w-3.5" />
                    </button>
                    <button
                      v-else
                      type="button"
                      class="mt-0.5 rounded-sm p-1 text-muted-foreground hover:text-foreground"
                      :title="row.area.spec ? 'Toggle spec preview' : 'No spec yet'"
                      :class="!row.area.spec && 'opacity-40'"
                      @click.stop.prevent="row.area.spec && toggleSpec(row.area.uid)"
                    >
                      <component :is="expandedSpecs.has(row.area.uid) ? ChevronDown : ChevronRight" class="h-3.5 w-3.5" />
                    </button>
                    <div class="min-w-0 flex-1">
                      <div class="flex flex-wrap items-center gap-1.5">
                        <span class="truncate text-sm font-medium">{{ row.area.title || row.area.key }}</span>
                        <Badge v-if="!row.area.enabled" variant="outline" class="px-1.5 text-[10px]">disabled</Badge>
                        <span
                          v-if="row.area.stale"
                          class="h-2 w-2 shrink-0 rounded-full bg-amber-500"
                          :title="areaStaleTitle(row.area)"
                        />
                        <Badge v-if="row.area.pending_edits > 0" variant="warn" class="px-1.5 text-[10px]" title="Pending agent edits">
                          {{ row.area.pending_edits }}
                        </Badge>
                      </div>
                      <div class="truncate font-mono text-[10px] text-muted-foreground">{{ row.area.key }}</div>
                      <div v-if="row.area.scope_paths.length" class="mt-1 flex flex-wrap items-center gap-1.5">
                        <span
                          v-for="path in row.area.scope_paths.slice(0, 2)"
                          :key="path"
                          class="rounded-full border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground"
                          :title="path"
                        >
                          {{ path }}
                        </span>
                        <span
                          v-if="row.area.scope_paths.length > 2"
                          class="text-[10px] text-muted-foreground"
                          :title="row.area.scope_paths.slice(2).join('\n')"
                        >
                          +{{ row.area.scope_paths.length - 2 }}
                        </span>
                      </div>
                    </div>
                    <!-- Every real area row is confirmable: a parent that owns
                         scope paths can go stale too. Only synthetic group
                         headers (row.type === 'group') own nothing, and they
                         never render these cells. -->
                    <AreaHealthCells
                      :health="healthFor(row.area.uid)"
                      :file-total="row.fileTotal"
                      confirmable
                      :confirming="confirmingUid === row.area.uid"
                      @confirm="confirmCurrent(row.area)"
                    />
                  </RouterLink>
                  <div
                    v-if="expandedSpecs.has(row.area.uid) && row.area.spec"
                    class="mb-2 mr-3 rounded-md border border-border p-3"
                    :style="{ marginLeft: `${36 + row.depth * 20}px` }"
                  >
                    <MarkdownView :model-value="row.area.spec" preview-only />
                  </div>
                </template>
              </li>
            </ul>
          </CardContent>
        </Card>
        </TabsContent>

        <!-- ── Features: cross-cutting spec overlays (hierarchical tree) ──── -->
        <TabsContent value="features" class="mt-3">
        <Card>
          <CardHeader class="flex-row items-center justify-between space-y-0">
            <CardTitle class="flex items-center gap-1.5 text-base">
              Features
              <HelpCircle class="h-3.5 w-3.5 shrink-0 text-muted-foreground" :title="AREA_KIND_HELP.feature" />
            </CardTitle>
            <span class="text-xs text-muted-foreground">
              {{ features.length }} overlay{{ features.length === 1 ? '' : 's' }} — audited against their spec, on top of the partition
            </span>
          </CardHeader>
          <CardContent class="p-0">
            <div v-if="!features.length" class="p-4 text-sm text-muted-foreground">
              No feature areas — cross-cutting overlays the agent proposes land here.
            </div>
            <ul v-else class="divide-y divide-border">
              <li v-for="row in visibleFeatureTreeRows" :key="`${row.type}:${row.key}`">
                <!-- Implicit parent: a group header derived from key segments -->
                <button
                  v-if="row.type === 'group'"
                  type="button"
                  class="flex w-full items-center gap-1.5 bg-muted/40 py-1.5 pr-3 text-left text-xs font-semibold text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground"
                  :style="indent(row.depth)"
                  :title="collapsedFeatureKeys.has(row.key) ? 'Expand' : 'Collapse'"
                  @click="toggleFeatureCollapse(row.key)"
                >
                  <component :is="collapsedFeatureKeys.has(row.key) ? ChevronRight : ChevronDown" class="h-3.5 w-3.5 shrink-0" />
                  <FolderTree class="h-3.5 w-3.5 shrink-0" />
                  <span class="truncate font-mono">{{ row.name }}/</span>
                </button>

                <template v-else-if="row.type === 'leaf'">
                  <RouterLink
                    :to="{ name: 'area-detail', params: { uid: row.item.uid } }"
                    class="flex items-start gap-2 border-l-2 border-l-primary/60 py-2 pr-3 transition-colors hover:bg-accent/50"
                    :class="{ 'opacity-60': !row.item.enabled }"
                    :style="indent(row.depth)"
                  >
                    <button
                      v-if="hasFeatureChildren(row.key)"
                      type="button"
                      class="mt-0.5 rounded-sm p-1 text-muted-foreground hover:text-foreground"
                      :title="collapsedFeatureKeys.has(row.key) ? 'Expand children' : 'Collapse children'"
                      @click.stop.prevent="toggleFeatureCollapse(row.key)"
                    >
                      <component :is="collapsedFeatureKeys.has(row.key) ? ChevronRight : ChevronDown" class="h-3.5 w-3.5" />
                    </button>
                    <button
                      v-else
                      type="button"
                      class="mt-0.5 rounded-sm p-1 text-muted-foreground hover:text-foreground"
                      :title="row.item.spec ? 'Toggle spec preview' : 'No spec yet'"
                      :class="!row.item.spec && 'opacity-40'"
                      @click.stop.prevent="row.item.spec && toggleSpec(row.item.uid)"
                    >
                      <component :is="expandedSpecs.has(row.item.uid) ? ChevronDown : ChevronRight" class="h-3.5 w-3.5" />
                    </button>
                    <div class="min-w-0 flex-1">
                      <div class="flex flex-wrap items-center gap-1.5">
                        <span class="truncate text-sm font-medium">{{ row.item.title || row.item.key }}</span>
                        <Badge variant="info" class="px-1.5 text-[10px]">feature</Badge>
                        <Badge v-if="!row.item.enabled" variant="outline" class="px-1.5 text-[10px]">disabled</Badge>
                        <span
                          v-if="row.item.stale"
                          class="h-2 w-2 shrink-0 rounded-full bg-amber-500"
                          :title="areaStaleTitle(row.item)"
                        />
                        <Badge v-if="row.item.pending_edits > 0" variant="warn" class="px-1.5 text-[10px]" title="Pending agent edits">
                          {{ row.item.pending_edits }}
                        </Badge>
                        <span
                          v-if="row.item.scope_paths.length"
                          class="rounded-full border border-border px-2 py-0.5 text-[10px] text-muted-foreground"
                          :title="row.item.scope_paths.join('\n')"
                        >
                          spans {{ row.item.scope_paths.length }} path{{ row.item.scope_paths.length === 1 ? '' : 's' }}
                        </span>
                      </div>
                      <div class="truncate font-mono text-[10px] text-muted-foreground">{{ row.item.key }}</div>
                    </div>
                    <AreaHealthCells
                      :health="healthFor(row.item.uid)"
                      :file-total="fileCountByKey.get(row.item.key) ?? null"
                      confirmable
                      :confirming="confirmingUid === row.item.uid"
                      @confirm="confirmCurrent(row.item)"
                    />
                  </RouterLink>
                  <div
                    v-if="expandedSpecs.has(row.item.uid) && row.item.spec"
                    class="mb-2 mr-3 rounded-md border border-border p-3"
                    :style="{ marginLeft: `${36 + row.depth * 20}px` }"
                  >
                    <MarkdownView :model-value="row.item.spec" preview-only />
                  </div>
                </template>
              </li>
            </ul>
          </CardContent>
        </Card>
        </TabsContent>

        <!-- ── Ignored: not auditable, spec says why ─────────────────────── -->
        <TabsContent value="ignored" class="mt-3">
        <Card>
          <CardHeader class="flex-row items-center justify-between space-y-0">
            <CardTitle class="flex items-center gap-1.5 text-base">
              Ignored
              <HelpCircle class="h-3.5 w-3.5 shrink-0 text-muted-foreground" :title="AREA_KIND_HELP.ignore" />
            </CardTitle>
            <span class="text-xs text-muted-foreground">
              not auditable; the reason lives in the spec
            </span>
          </CardHeader>
          <CardContent class="p-0">
            <div v-if="!ignored.length" class="p-4 text-sm text-muted-foreground">
              Nothing ignored — every path on the map is auditable.
            </div>
            <ul v-else class="divide-y divide-border">
              <li v-for="a in ignored" :key="a.uid">
                <RouterLink
                  :to="{ name: 'area-detail', params: { uid: a.uid } }"
                  class="flex items-baseline gap-2 px-4 py-2 text-muted-foreground transition-colors hover:bg-accent/50"
                  :class="{ 'opacity-60': !a.enabled }"
                >
                  <span class="shrink-0 font-mono text-xs">{{ a.key }}</span>
                  <!-- An ignore area whose files moved is exactly the case
                       where the recorded reason may no longer hold. -->
                  <span
                    v-if="a.stale"
                    class="h-2 w-2 shrink-0 rounded-full bg-amber-500"
                    :title="areaStaleTitle(a)"
                  />
                  <span class="min-w-0 flex-1 truncate text-xs italic" :title="a.spec">{{ specSummary(a) || 'no reason recorded' }}</span>
                  <Badge v-if="!a.enabled" variant="outline" class="shrink-0 px-1.5 text-[10px]">disabled</Badge>
                  <Button
                    v-if="a.stale"
                    variant="ghost"
                    size="sm"
                    class="h-6 shrink-0 px-1.5 text-[10px]"
                    :loading="confirmingUid === a.uid"
                    title="I checked — this is still correctly ignored."
                    @click.stop.prevent="confirmCurrent(a)"
                  >
                    <Check class="h-3 w-3" /> Reviewed
                  </Button>
                </RouterLink>
              </li>
            </ul>
          </CardContent>
        </Card>
        </TabsContent>
      </Tabs>

      <!-- ── Doc pages no area covers ───────────────────────────────────── -->
      <Card v-if="health?.unassigned_docs.length">
        <CardHeader class="flex-row items-center justify-between space-y-0">
          <CardTitle class="flex items-center gap-1.5 text-base">
            Unassigned pages
            <HelpCircle
              class="h-3.5 w-3.5 shrink-0 text-muted-foreground"
              title="Doc pages whose watch paths fall outside every area's scope. Either the map has a gap, or the page documents something the map does not model."
            />
          </CardTitle>
          <span class="text-xs text-muted-foreground">
            {{ health.unassigned_docs.length }} page{{ health.unassigned_docs.length === 1 ? '' : 's' }} outside the map
          </span>
        </CardHeader>
        <CardContent class="p-0">
          <ul class="divide-y divide-border">
            <li v-for="d in health.unassigned_docs" :key="d.uid">
              <RouterLink
                :to="{ name: 'documentation', params: { repoSlug: repoSlug || '' }, query: { doc: d.slug } }"
                class="flex items-center gap-2 px-4 py-2 transition-colors hover:bg-accent/50"
              >
                <span class="min-w-0 flex-1 truncate text-sm">{{ d.title || d.slug }}</span>
                <span class="hidden shrink-0 truncate font-mono text-[10px] text-muted-foreground sm:block">{{ d.slug }}</span>
                <Badge v-if="d.stale" variant="warn" class="shrink-0 px-1.5 text-[10px]">stale</Badge>
                <Badge
                  v-if="d.last_checked"
                  :variant="outcomeVariant(d.outcome)"
                  class="shrink-0 px-1.5 text-[10px]"
                >
                  {{ d.outcome }} · {{ daysAgo(d.last_checked) }}
                </Badge>
                <Badge v-else variant="outline" class="shrink-0 px-1.5 text-[10px]">never audited</Badge>
              </RouterLink>
            </li>
          </ul>
        </CardContent>
      </Card>
    </template>

    <AlertDialog v-model:open="bulkResolveOpen">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{{ pendingBulkAction === 'accept' ? 'Accept' : 'Reject' }} pending edits</AlertDialogTitle>
          <AlertDialogDescription>
            {{ pendingBulkAction === 'accept' ? 'Accept' : 'Reject' }} all {{ areaStore.edits.length }} pending edit{{ areaStore.edits.length === 1 ? '' : 's' }}?
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction @click="confirmResolveAll">
            {{ pendingBulkAction === 'accept' ? 'Accept all' : 'Reject all' }}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>

    <AlertDialog v-model:open="resetOpen">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete the entire area map?</AlertDialogTitle>
          <AlertDialogDescription>
            This permanently deletes all {{ areaStore.areas.length }} area{{ areaStore.areas.length === 1 ? '' : 's' }}
            and {{ areaStore.edits.length }} pending edit{{ areaStore.edits.length === 1 ? '' : 's' }} for this repository.
            Coverage history is kept, but campaigns fall back to docs-derived planning until a new map is
            proposed and accepted. This cannot be undone.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction class="bg-destructive text-destructive-foreground hover:bg-destructive/90" @click="confirmReset">
            Delete everything
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </div>
</template>
