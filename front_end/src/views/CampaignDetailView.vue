<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import {
  Activity,
  ArrowLeft,
  Ban,
  ChevronDown,
  ChevronRight,
  FolderTree,
  Gauge,
  Globe,
  RefreshCw,
  Rocket,
  Trash2,
  TriangleAlert,
  User,
  Zap,
} from 'lucide-vue-next'
import { PageHeader } from '@/components/ui/page-header'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { ErrorState } from '@/components/ui/error-state'
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
import { useCampaignStore } from '@/stores/campaignStore'
import { useRepositoryStore } from '@/stores/repositoryStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import {
  CAMPAIGN_TEMPLATE_LABELS,
  campaignProgress,
  campaignStatusVariant,
  isLiveCampaignStatus,
  partStateVariant,
} from '@/lib/campaignStatus'
import { formatRelativeTime } from '@/lib/utils'
import type { CampaignDTO, CampaignEvent, CampaignPlanSummary } from '@/types/api'

const route = useRoute()
const router = useRouter()
const campaigns = useCampaignStore()
const repositories = useRepositoryStore()
const toast = useToast()

const uid = computed(() => String(route.params.uid))
const campaign = ref<CampaignDTO | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)
const launching = ref(false)
const cancelling = ref(false)
const deleteOpen = ref(false)
const deleting = ref(false)

// ── Back link (list views are repo-scoped; details are flat) ─────────────────
const repoSlug = computed(() => {
  const repoUid = campaign.value?.repository_uid
  return repoUid ? repositories.find(repoUid)?.slug ?? null : null
})

onMounted(load)
watch(uid, () => void load())

async function load() {
  loading.value = true
  error.value = null
  try {
    campaign.value = await campaigns.get(uid.value)
    if (!repositories.loaded) {
      // Best-effort: the back link falls back to nothing if this fails.
      await repositories.fetchAll().catch(() => {})
    }
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

// ── Polling — parts move server-side while running/finalizing ────────────────
let pollTimer: number | undefined
async function poll() {
  try {
    campaign.value = await campaigns.get(uid.value)
  } catch {
    /* transient — the next tick catches up */
  }
}
watch(
  () => campaign.value?.status,
  (status) => {
    const live = !!status && isLiveCampaignStatus(status)
    if (live && pollTimer === undefined) {
      pollTimer = window.setInterval(() => void poll(), 10_000)
    } else if (!live && pollTimer !== undefined) {
      window.clearInterval(pollTimer)
      pollTimer = undefined
    }
  },
)
onBeforeUnmount(() => {
  if (pollTimer !== undefined) window.clearInterval(pollTimer)
})

// ── Actions ──────────────────────────────────────────────────────────────────

async function launch() {
  if (!campaign.value || launching.value) return
  launching.value = true
  try {
    campaign.value = await campaigns.launch(campaign.value.uid)
    toast.success('Campaign launched', 'Parts dispatch as capacity allows.')
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t launch campaign', msg)
  } finally {
    launching.value = false
  }
}

async function cancelCampaign() {
  if (!campaign.value || cancelling.value) return
  cancelling.value = true
  try {
    campaign.value = await campaigns.cancel(campaign.value.uid)
    toast.success('Campaign cancelled')
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t cancel campaign', msg)
  } finally {
    cancelling.value = false
  }
}

const isLive = computed(
  () => campaign.value?.status === 'running' || campaign.value?.status === 'finalizing',
)

async function deleteCampaign() {
  if (!campaign.value || deleting.value) return
  deleteOpen.value = false
  deleting.value = true
  try {
    await campaigns.remove(campaign.value.uid)
    toast.success('Campaign deleted', campaign.value.title || campaign.value.uid.slice(0, 12))
    void router.push(repoSlug.value ? { name: 'campaigns', params: { repoSlug: repoSlug.value } } : { name: 'overview' })
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t delete campaign', msg)
  } finally {
    deleting.value = false
  }
}

// ── Derived display state ────────────────────────────────────────────────────

const parts = computed(() => [...(campaign.value?.parts ?? [])].sort((a, b) => a.idx - b.idx))

const areaPrefix = computed(() => campaign.value?.area_prefix ?? '')

const progress = computed(() =>
  campaign.value ? campaignProgress(campaign.value) : { finished: 0, total: 0 },
)
const progressPct = computed(() =>
  progress.value.total ? Math.round((progress.value.finished / progress.value.total) * 100) : 0,
)

const hasSummary = computed(
  () => Object.keys(campaign.value?.summary ?? {}).length > 0,
)

// ── Plan explanation ("How this plan was built", set at plan time) ───────────

const planSummary = computed<CampaignPlanSummary>(() => campaign.value?.plan_summary ?? {})
const hasPlanSummary = computed(() => Object.keys(planSummary.value).length > 0)

/** The planner's reconciliation as plain sentences — zero clauses are omitted.
 *  (Oversized/degraded/prefix render separately with their own emphasis.) */
function planSentences(s: CampaignPlanSummary): string[] {
  const n = (x: number) => x.toLocaleString('en-US')
  const count = (x: number, word: string, pluralWord = `${word}s`) =>
    `${n(x)} ${x === 1 ? word : pluralWord}`
  const lines: string[] = []

  if (s.source === 'area-map') {
    lines.push('Planned from the area map.')
    if (s.map_areas) {
      const excluded = [
        s.groupings ? count(s.groupings, 'grouping') : '',
        s.ignored ? count(s.ignored, 'ignore area') : '',
      ].filter(Boolean)
      lines.push(
        `${count(s.map_areas, 'area')} on the map → ${count(s.leaves ?? 0, 'auditable leaf', 'auditable leaves')}` +
          (excluded.length ? ` (${excluded.join(' and ')} excluded).` : '.'),
      )
    }
    if (s.features) {
      const groupings = s.feature_groupings
        ? ` under ${count(s.feature_groupings, 'parent feature')}`
        : ''
      lines.push(`${count(s.features, 'feature leaf', 'feature leaves')}${groupings} audited against their specs.`)
    }
    if (s.area_parts) {
      const bundled = s.bundled_leaves
        ? ` — ${n(s.bundled_leaves)} small sibling leaves share runs with their neighbors`
        : ''
      lines.push(`Bundled into ${count(s.area_parts, 'area run')} of roughly 50–150 files${bundled}.`)
    }
  } else {
    lines.push('Planned from doc-derived areas — no area map yet.')
    if (s.area_parts) {
      lines.push(`Partitioned into ${count(s.area_parts, 'area run')} of roughly 50–150 files.`)
    }
  }

  const rides = [
    s.feature_parts ? count(s.feature_parts, 'feature spec-audit') : '',
    s.global_parts ? count(s.global_parts, 'global sweep') : '',
  ].filter(Boolean)
  if (rides.length) {
    const total = (s.feature_parts ?? 0) + (s.global_parts ?? 0)
    lines.push(`${rides.join(' and ')} ride${total === 1 ? 's' : ''} along.`)
  }
  return lines
}

const planLines = computed(() => planSentences(planSummary.value))

// ── Part row expand / collapse ───────────────────────────────────────────────

const expandedParts = ref<Set<number>>(new Set())

function togglePart(idx: number) {
  const next = new Set(expandedParts.value)
  if (next.has(idx)) next.delete(idx)
  else next.add(idx)
  expandedParts.value = next
}

// ── Plan stat-header helpers ─────────────────────────────────────────────────

const planDetailsOpen = ref(false)

/** Total run count — prefer plan_summary.total_runs, fall back to parts.length. */
const totalRuns = computed(() =>
  planSummary.value.total_runs ?? parts.value.length,
)

/** "N subsystem · N feature · N global" with zeros hidden. */
const byKindLabel = computed(() => {
  const bk = planSummary.value.by_kind
  if (!bk) return ''
  const segs: string[] = []
  if (bk.area) segs.push(`${bk.area} subsystem`)
  if (bk.feature) segs.push(`${bk.feature} feature`)
  if (bk.global) segs.push(`${bk.global} global`)
  return segs.join(' · ')
})

const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low']
const severityCounts = computed(() => {
  const by = campaign.value?.summary.counts?.by_severity ?? {}
  const known = SEVERITY_ORDER.filter((s) => by[s] != null).map((s) => [s, by[s]] as const)
  const extra = Object.entries(by).filter(([s]) => !SEVERITY_ORDER.includes(s))
  return [...known, ...extra]
})

function severityVariant(sev: string) {
  if (sev === 'critical' || sev === 'high') return 'destructive' as const
  if (sev === 'medium') return 'warn' as const
  return 'secondary' as const
}

/** Parent-feature rollup state ('covered' | 'partial' | 'uncovered'). */
function coverageStateVariant(state: string) {
  if (state === 'covered') return 'success' as const
  if (state === 'partial') return 'warn' as const
  return 'secondary' as const
}

/** Events newest-first — a lifecycle log reads like the notification feed. */
const events = computed<CampaignEvent[]>(() => [...(campaign.value?.events ?? [])].reverse())

function eventLabel(e: CampaignEvent): string {
  if (e.type === 'replanned' && typeof e.parts === 'number' && typeof e.was === 'number') {
    return `replanned at launch — ${e.was} → ${e.parts} parts`
  }
  const type = String(e.type || '').replaceAll('_', ' ')
  return e.part != null ? `${type} — part ${e.part}` : type
}

function scopePathsLabel(paths: string[]): string {
  if (!paths.length) return 'whole repo'
  return paths.length === 1 ? paths[0] : `${paths[0]} +${paths.length - 1}`
}
</script>

<template>
  <div class="space-y-4">
    <template v-if="loading && !campaign">
      <Skeleton class="h-24" />
      <Skeleton class="h-64" />
      <Skeleton class="h-40" />
    </template>

    <ErrorState v-else-if="error && !campaign" title="Couldn't load campaign" :message="error">
      <Button variant="outline" size="sm" @click="load">Retry</Button>
    </ErrorState>

    <template v-else-if="campaign">
      <PageHeader :title="campaign.title || `Campaign ${campaign.uid.slice(0, 12)}`">
        <template #breadcrumb>
          <RouterLink
            v-if="repoSlug"
            :to="{ name: 'campaigns', params: { repoSlug } }"
            class="mb-1 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft class="h-3 w-3" /> Campaigns
          </RouterLink>
          <div class="mb-1 flex flex-wrap items-center gap-2">
            <Badge :variant="campaignStatusVariant(campaign.status)">{{ campaign.status }}</Badge>
            <Badge variant="outline" class="px-1.5 text-[10px]">
              <Gauge class="h-3 w-3" /> {{ CAMPAIGN_TEMPLATE_LABELS[campaign.template] ?? campaign.template }}
            </Badge>
            <Badge variant="outline" class="px-1.5 text-[10px]" title="Effort tier">
              <Zap class="h-3 w-3" /> {{ campaign.effort || 'default tiers' }}
            </Badge>
            <Badge v-if="areaPrefix" variant="outline" class="px-1.5 font-mono text-[10px]" title="Scoped to areas under this key prefix">
              <FolderTree class="h-3 w-3" /> {{ areaPrefix }}
            </Badge>
            <Badge
              v-for="key in campaign.lens_keys"
              :key="key"
              variant="secondary"
              class="px-1.5 font-mono text-[10px]"
            >
              {{ key }}
            </Badge>
            <Badge v-if="campaign.created_by" variant="outline" class="px-1.5 text-[10px]" title="Created by">
              <User class="h-3 w-3" />
              <span class="font-mono">{{ campaign.created_by.slice(0, 8) }}</span>
            </Badge>
            <Badge v-if="campaign.trigger_provenance" variant="outline" class="px-1.5 text-[10px]" title="Trigger">
              {{ campaign.trigger_provenance }}
            </Badge>
          </div>
        </template>

        <Button variant="outline" size="sm" @click="load">
          <RefreshCw :class="{ 'animate-spin': loading }" /> Refresh
        </Button>
        <Button
          v-if="campaign.status === 'planning'"
          size="sm"
          :loading="launching"
          @click="launch"
        >
          <Rocket /> Launch
        </Button>
        <Button
          v-if="campaign.status === 'planning' || campaign.status === 'running'"
          variant="outline"
          size="sm"
          :loading="cancelling"
          @click="cancelCampaign"
        >
          <Ban /> Cancel
        </Button>
        <Button
          variant="outline"
          size="sm"
          class="text-destructive hover:text-destructive"
          :disabled="isLive"
          :loading="deleting"
          :title="isLive ? 'Cancel the campaign before deleting it' : 'Delete this campaign'"
          @click="deleteOpen = true"
        >
          <Trash2 /> Delete
        </Button>
      </PageHeader>

      <!-- Progress -->
      <div class="flex items-center gap-3">
        <div class="h-2 flex-1 overflow-hidden rounded-full bg-muted">
          <div
            class="h-full rounded-full bg-primary transition-all"
            :style="{ width: `${progressPct}%` }"
          />
        </div>
        <span class="whitespace-nowrap text-sm tabular-nums text-muted-foreground">
          {{ progress.finished }}/{{ progress.total }} parts
        </span>
      </div>

      <!-- Plan stat-header -->
      <Card v-if="hasPlanSummary || parts.length">
        <CardContent class="py-3">
          <!-- Stats row -->
          <div class="flex flex-wrap items-center gap-3">
            <span class="text-2xl font-bold tabular-nums">{{ totalRuns }}</span>
            <span class="text-sm text-muted-foreground">run{{ totalRuns === 1 ? '' : 's' }}</span>
            <span v-if="byKindLabel" class="text-sm text-muted-foreground">· {{ byKindLabel }}</span>
            <Badge
              v-if="planSummary.oversized?.length"
              variant="warn"
              class="gap-1 px-1.5 text-[10px]"
              :title="planSummary.oversized.join('\n')"
            >
              <TriangleAlert class="h-3 w-3" />
              {{ planSummary.oversized.length }} oversized
            </Badge>
            <Badge
              v-if="planSummary.degraded"
              variant="warn"
              class="px-1.5 text-[10px]"
              title="Degraded plan"
            >
              <TriangleAlert class="h-3 w-3" />
              degraded
            </Badge>
          </div>
          <!-- Collapsible plan details -->
          <div v-if="hasPlanSummary" class="mt-2">
            <button
              class="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
              @click="planDetailsOpen = !planDetailsOpen"
            >
              <component :is="planDetailsOpen ? ChevronDown : ChevronRight" class="h-3.5 w-3.5" />
              How this plan was built
            </button>
            <div v-if="planDetailsOpen" class="mt-2 space-y-1 pl-5">
              <p v-for="(line, i) in planLines" :key="i" class="text-sm text-muted-foreground">
                {{ line }}
              </p>
              <p
                v-if="planSummary.area_prefix"
                class="flex flex-wrap items-center gap-1.5 text-sm text-muted-foreground"
              >
                Sliced to areas under
                <span class="rounded-full border border-border px-2 py-0.5 font-mono text-xs">
                  {{ planSummary.area_prefix }}
                </span>
              </p>
              <p
                v-if="planSummary.oversized?.length"
                class="flex items-start gap-1.5 text-sm text-warn"
                :title="planSummary.oversized.join('\n')"
              >
                <TriangleAlert class="mt-0.5 h-3.5 w-3.5 shrink-0" />
                {{ planSummary.oversized.length }} area{{ planSummary.oversized.length === 1 ? ' exceeds' : 's exceed' }}
                the target size — ask Map areas to split {{ planSummary.oversized.length === 1 ? 'it' : 'them' }}.
              </p>
              <p v-if="planSummary.degraded" class="flex items-start gap-1.5 text-sm text-warn">
                <TriangleAlert class="mt-0.5 h-3.5 w-3.5 shrink-0" />
                {{ planSummary.degraded }}
              </p>
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- Batch children roll-up -->
      <Card v-if="campaign.kind === 'batch'">
        <CardHeader class="pb-2">
          <h2 class="text-sm font-semibold">Child campaigns</h2>
        </CardHeader>
        <CardContent class="space-y-1.5">
          <div
            v-if="!campaign.child_uids?.length"
            class="text-sm text-muted-foreground"
          >
            No child campaigns yet.
          </div>
          <RouterLink
            v-for="childUid in campaign.child_uids"
            :key="childUid"
            :to="{ name: 'campaign-detail', params: { uid: childUid } }"
            class="flex items-center gap-2 rounded-md border border-border px-3 py-2 text-sm hover:bg-muted/50"
          >
            <Activity class="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            <span class="font-mono text-xs">{{ childUid.slice(0, 12) }}</span>
          </RouterLink>
        </CardContent>
      </Card>

      <!-- Parts table (non-batch only) -->
      <Card v-if="campaign.kind !== 'batch'">
        <CardContent class="p-0">
          <div class="overflow-x-auto">
            <table class="w-full text-sm">
              <thead>
                <tr class="text-left text-xs uppercase tracking-wide text-muted-foreground">
                  <th class="w-6 px-2 py-2" />
                  <th class="px-4 py-2 font-medium">#</th>
                  <th class="px-4 py-2 font-medium">Part</th>
                  <th class="px-4 py-2 font-medium">Files</th>
                  <th class="px-4 py-2 font-medium">Lenses</th>
                  <th class="px-4 py-2 font-medium">State</th>
                  <th class="px-4 py-2 font-medium">Run</th>
                </tr>
              </thead>
              <tbody>
                <template v-for="p in parts" :key="p.idx">
                  <!-- Collapsed / summary row -->
                  <tr
                    class="border-t border-border hover:bg-muted/30"
                    :class="{ 'cursor-pointer': !p.run_uid }"
                    @click="!p.run_uid ? togglePart(p.idx) : undefined"
                  >
                    <!-- Chevron -->
                    <td class="w-6 px-2 py-2 text-muted-foreground">
                      <button
                        class="flex items-center justify-center"
                        :aria-label="expandedParts.has(p.idx) ? 'Collapse' : 'Expand'"
                        @click.stop="togglePart(p.idx)"
                      >
                        <component
                          :is="expandedParts.has(p.idx) ? ChevronDown : ChevronRight"
                          class="h-3.5 w-3.5"
                        />
                      </button>
                    </td>
                    <!-- Index -->
                    <td class="px-4 py-2 tabular-nums text-muted-foreground">{{ p.idx }}</td>
                    <!-- Title (with kind badge, truncated) -->
                    <td class="px-4 py-2">
                      <span class="flex min-w-0 items-center gap-1.5">
                        <Globe
                          v-if="p.kind === 'global'"
                          class="h-3.5 w-3.5 shrink-0 text-muted-foreground"
                          aria-label="Global sweep"
                        />
                        <FolderTree
                          v-else
                          class="h-3.5 w-3.5 shrink-0 text-muted-foreground"
                          aria-label="Area sweep"
                        />
                        <Badge
                          v-if="p.kind === 'feature'"
                          variant="info"
                          class="shrink-0 px-1.5 text-[10px]"
                          title="Spec-anchored feature audit"
                        >
                          feature
                        </Badge>
                        <span class="max-w-[240px] truncate font-medium" :title="p.title || `Part ${p.idx}`">
                          {{ p.title || `Part ${p.idx}` }}
                        </span>
                      </span>
                    </td>
                    <!-- File count -->
                    <td class="px-4 py-2 tabular-nums text-muted-foreground">
                      {{ p.file_count ?? '—' }}
                    </td>
                    <!-- Lens count chip -->
                    <td class="px-4 py-2">
                      <Badge variant="secondary" class="px-1.5 text-[10px]">
                        {{ p.lens_keys.length }} lens{{ p.lens_keys.length === 1 ? '' : 'es' }}
                      </Badge>
                    </td>
                    <!-- State -->
                    <td class="px-4 py-2">
                      <Badge :variant="partStateVariant(p.state)">{{ p.state }}</Badge>
                    </td>
                    <!-- Run link -->
                    <td class="px-4 py-2">
                      <RouterLink
                        v-if="p.run_uid"
                        :to="{ name: 'run-detail', params: { uid: p.run_uid } }"
                        class="inline-flex items-center gap-1 text-xs underline-offset-2 hover:underline"
                        @click.stop
                      >
                        <Activity class="h-3 w-3" />
                        <span class="font-mono">{{ p.run_uid.slice(0, 8) }}</span>
                      </RouterLink>
                      <span v-else class="text-xs text-muted-foreground">—</span>
                    </td>
                  </tr>
                  <!-- Expanded detail row -->
                  <tr v-if="expandedParts.has(p.idx)" class="border-t border-border bg-muted/20">
                    <td />
                    <td colspan="6" class="px-4 py-3">
                      <div class="space-y-2">
                        <!-- Scope paths -->
                        <div>
                          <p class="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">Scope</p>
                          <ul class="space-y-0.5">
                            <li
                              v-if="!p.scope_paths.length"
                              class="font-mono text-xs text-muted-foreground"
                            >
                              whole repo
                            </li>
                            <li
                              v-for="path in p.scope_paths"
                              :key="path"
                              class="font-mono text-xs text-muted-foreground"
                            >
                              {{ path }}
                            </li>
                          </ul>
                        </div>
                        <!-- Area keys -->
                        <div v-if="p.area_keys.length">
                          <p class="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">Areas</p>
                          <span class="flex flex-wrap gap-1">
                            <Badge
                              v-for="key in p.area_keys"
                              :key="key"
                              variant="outline"
                              class="px-1.5 font-mono text-[10px]"
                            >
                              {{ key }}
                            </Badge>
                          </span>
                        </div>
                        <!-- Lens keys -->
                        <div v-if="p.lens_keys.length">
                          <p class="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">Lenses</p>
                          <span class="flex flex-wrap gap-1">
                            <Badge
                              v-for="key in p.lens_keys"
                              :key="key"
                              variant="secondary"
                              class="px-1.5 font-mono text-[10px]"
                            >
                              {{ key }}
                            </Badge>
                          </span>
                        </div>
                      </div>
                    </td>
                  </tr>
                </template>
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>

      <!-- Summary digest (post-finalization) -->
      <Card v-if="hasSummary">
        <CardHeader class="pb-2">
          <h2 class="text-sm font-semibold">Summary</h2>
        </CardHeader>
        <CardContent class="space-y-4">
          <div v-if="campaign.summary.counts" class="flex flex-wrap items-center gap-2">
            <span class="text-sm text-muted-foreground">
              {{ campaign.summary.counts.total }} finding{{ campaign.summary.counts.total === 1 ? '' : 's' }}
            </span>
            <Badge v-for="[sev, n] in severityCounts" :key="sev" :variant="severityVariant(sev)">
              {{ sev }}: {{ n }}
            </Badge>
          </div>

          <div v-if="campaign.summary.coverage?.parts?.length" class="space-y-1">
            <h3 class="text-xs font-medium uppercase tracking-wide text-muted-foreground">Coverage</h3>
            <table class="w-full text-sm">
              <tbody>
                <tr
                  v-for="cp in campaign.summary.coverage.parts"
                  :key="cp.idx"
                  class="border-t border-border first:border-t-0"
                >
                  <td class="py-1.5 pr-3 tabular-nums text-muted-foreground">{{ cp.idx }}</td>
                  <td class="max-w-[260px] truncate py-1.5 pr-3 font-medium">{{ cp.title || `Part ${cp.idx}` }}</td>
                  <td class="whitespace-nowrap py-1.5 pr-3 tabular-nums">
                    {{ cp.covered }} covered · {{ cp.skipped }} skipped
                  </td>
                  <td class="py-1.5">
                    <Badge :variant="partStateVariant(cp.state)">{{ cp.state }}</Badge>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <div v-if="campaign.summary.coverage?.feature_rollup?.length" class="space-y-1">
            <h3 class="text-xs font-medium uppercase tracking-wide text-muted-foreground">Feature health</h3>
            <ul class="divide-y divide-border">
              <li v-for="fr in campaign.summary.coverage.feature_rollup" :key="fr.feature_key" class="py-1.5">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="min-w-0 truncate font-mono text-xs font-medium">{{ fr.feature_key || '(top-level)' }}</span>
                  <Badge :variant="coverageStateVariant(fr.state)" class="px-1.5 text-[10px]">{{ fr.state }}</Badge>
                  <span class="text-xs text-muted-foreground">
                    {{ fr.covered }}/{{ fr.leaf_count }} sub-feature{{ fr.leaf_count === 1 ? '' : 's' }} covered
                    <template v-if="fr.findings"> · {{ fr.findings }} finding{{ fr.findings === 1 ? '' : 's' }}</template>
                  </span>
                </div>
              </li>
            </ul>
          </div>

          <div v-if="campaign.summary.coverage?.holes?.length" class="space-y-1">
            <h3 class="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Coverage holes
            </h3>
            <ul class="space-y-0.5">
              <li
                v-for="hole in campaign.summary.coverage.holes"
                :key="hole"
                class="font-mono text-xs text-muted-foreground"
              >
                {{ hole }}
              </li>
            </ul>
          </div>

          <div v-if="campaign.summary.failed_parts?.length" class="space-y-1">
            <h3 class="text-xs font-medium uppercase tracking-wide text-muted-foreground">Failed parts</h3>
            <div class="flex flex-wrap gap-1.5">
              <Badge v-for="idx in campaign.summary.failed_parts" :key="idx" variant="destructive">
                part {{ idx }}
              </Badge>
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- Events timeline -->
      <Card v-if="events.length">
        <CardHeader class="pb-2">
          <h2 class="text-sm font-semibold">Events</h2>
        </CardHeader>
        <CardContent>
          <ul class="space-y-1.5">
            <li
              v-for="(e, i) in events"
              :key="`${e.ts}-${i}`"
              class="flex items-baseline gap-2 text-xs text-muted-foreground"
            >
              <span class="w-20 shrink-0 whitespace-nowrap tabular-nums">{{ formatRelativeTime(e.ts) }}</span>
              <span>{{ eventLabel(e) }}</span>
            </li>
          </ul>
        </CardContent>
      </Card>
    </template>

    <AlertDialog v-model:open="deleteOpen">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete this campaign?</AlertDialogTitle>
          <AlertDialogDescription>
            “{{ campaign?.title || campaign?.uid.slice(0, 12) }}” and its plan are removed
            permanently. Runs it already dispatched — and the findings they produced — are kept.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            class="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            @click="deleteCampaign"
          >
            Delete campaign
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </div>
</template>
