<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import {
  Activity,
  ArrowLeft,
  Ban,
  FolderTree,
  Gauge,
  Globe,
  RefreshCw,
  Rocket,
  User,
  Zap,
} from 'lucide-vue-next'
import { PageHeader } from '@/components/ui/page-header'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { ErrorState } from '@/components/ui/error-state'
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
import type { CampaignDTO, CampaignEvent } from '@/types/api'

const route = useRoute()
const campaigns = useCampaignStore()
const repositories = useRepositoryStore()
const toast = useToast()

const uid = computed(() => String(route.params.uid))
const campaign = ref<CampaignDTO | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)
const launching = ref(false)
const cancelling = ref(false)

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

      <!-- Parts -->
      <Card>
        <CardContent class="p-0">
          <div class="overflow-x-auto">
            <table class="w-full text-sm">
              <thead>
                <tr class="text-left text-xs uppercase tracking-wide text-muted-foreground">
                  <th class="px-4 py-2 font-medium">#</th>
                  <th class="px-4 py-2 font-medium">Part</th>
                  <th class="px-4 py-2 font-medium">Scope</th>
                  <th class="px-4 py-2 font-medium">Lenses</th>
                  <th class="px-4 py-2 font-medium">Files</th>
                  <th class="px-4 py-2 font-medium">State</th>
                  <th class="px-4 py-2 font-medium">Run</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="p in parts" :key="p.idx" class="border-t border-border">
                  <td class="px-4 py-2 tabular-nums text-muted-foreground">{{ p.idx }}</td>
                  <td class="max-w-[240px] px-4 py-2">
                    <span class="flex items-center gap-1.5 font-medium">
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
                      <span class="truncate">{{ p.title || `Part ${p.idx}` }}</span>
                      <Badge v-if="p.kind === 'feature'" variant="info" class="shrink-0 px-1.5 text-[10px]" title="Spec-anchored feature audit">
                        feature · spec audit
                      </Badge>
                    </span>
                    <span v-if="p.area_keys.length" class="mt-1 flex max-w-[240px] flex-wrap gap-1">
                      <Badge
                        v-for="key in p.area_keys"
                        :key="key"
                        variant="outline"
                        class="px-1.5 font-mono text-[10px]"
                        title="Area bundled into this part"
                      >
                        {{ key }}
                      </Badge>
                    </span>
                  </td>
                  <td class="max-w-[220px] px-4 py-2">
                    <span
                      class="block truncate font-mono text-xs text-muted-foreground"
                      :title="p.scope_paths.join('\n')"
                    >
                      {{ scopePathsLabel(p.scope_paths) }}
                    </span>
                  </td>
                  <td class="px-4 py-2">
                    <span class="flex max-w-[260px] flex-wrap gap-1">
                      <Badge
                        v-for="key in p.lens_keys"
                        :key="key"
                        variant="secondary"
                        class="px-1.5 font-mono text-[10px]"
                      >
                        {{ key }}
                      </Badge>
                    </span>
                  </td>
                  <td class="px-4 py-2 tabular-nums text-muted-foreground">
                    {{ p.file_count ?? '—' }}
                  </td>
                  <td class="px-4 py-2">
                    <Badge :variant="partStateVariant(p.state)">{{ p.state }}</Badge>
                  </td>
                  <td class="px-4 py-2">
                    <RouterLink
                      v-if="p.run_uid"
                      :to="{ name: 'run-detail', params: { uid: p.run_uid } }"
                      class="inline-flex items-center gap-1 text-xs underline-offset-2 hover:underline"
                    >
                      <Activity class="h-3 w-3" />
                      <span class="font-mono">{{ p.run_uid.slice(0, 8) }}</span>
                    </RouterLink>
                    <span v-else class="text-xs text-muted-foreground">—</span>
                  </td>
                </tr>
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
  </div>
</template>
