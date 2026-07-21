<script setup lang="ts">
import { computed, ref, onMounted, watch } from 'vue'
import { RouterLink } from 'vue-router'
import {
  Check,
  ChevronDown,
  ChevronRight,
  FolderTree,
  Map as MapIcon,
  TriangleAlert,
  X,
} from 'lucide-vue-next'
import { useAreaStore } from '@/stores/areaStore'
import { useCampaignStore } from '@/stores/campaignStore'
import { useCurrentRepo } from '@/composables/useCurrentRepo'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { areaStaleTitle } from '@/lib/areas'
import AreaEditReviewCard from '@/components/areas/AreaEditReviewCard.vue'
import { MarkdownView } from '@/components/ui/markdown'
import { PageHeader } from '@/components/ui/page-header'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
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
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import type { AreaDTO, AreaEditDTO, CampaignAreasPreview } from '@/types/api'

const areaStore = useAreaStore()
const campaignStore = useCampaignStore()
const toast = useToast()
const { uid: repoUid } = useCurrentRepo()

const loading = ref(true)
const error = ref<string | null>(null)

// ── Load ─────────────────────────────────────────────────────────────────────

/** Best-effort partition health (campaign-areas preview) — null hides the strip. */
const preview = ref<CampaignAreasPreview | null>(null)

async function reload() {
  if (!repoUid.value) return
  loading.value = true
  error.value = null
  // The health strip is best-effort: any preview failure just hides it.
  preview.value = null
  void campaignStore
    .fetchAreas(repoUid.value)
    .then((p) => (preview.value = p))
    .catch(() => (preview.value = null))
  try {
    await Promise.all([
      areaStore.fetchAreas(repoUid.value),
      areaStore.fetchEdits(repoUid.value, 'pending'),
    ])
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
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

// ── Health strip (partition drift, from the campaign-areas preview) ──────────

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
const ignored = computed(() => sortedAreas.value.filter((a) => a.kind === 'ignore'))

/** area_key → file_count, from the preview (best-effort rollups). */
const fileCountByKey = computed<Map<string, number | null>>(() => {
  const m = new Map<string, number | null>()
  for (const a of preview.value?.areas ?? []) if (a.area_key) m.set(a.area_key, a.file_count)
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
    if (warnings.length) {
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
      subtitle="The reviewed audit partition: path-keyed areas that carry what to check where. Agents propose the map; every change lands here for review."
    >
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
      <Button
        size="sm"
        :loading="mapping"
        title="Dispatch one LLM run that walks the repository and proposes the area map. Proposals land below as pending edits."
        @click="mapAreas"
      >
        <MapIcon v-if="!mapping" />
        Map areas
      </Button>
    </PageHeader>

    <template v-if="loading">
      <Skeleton class="h-24" />
      <Skeleton class="h-64" />
    </template>

    <ErrorState v-else-if="error" title="Couldn't load areas" :message="error">
      <Button variant="outline" size="sm" @click="reload">Retry</Button>
    </ErrorState>

    <template v-else>
      <!-- ── Health strip (drift between the map and the tree) ───────────── -->
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

      <template v-else>
        <!-- ── Partition: the subsystem tree ─────────────────────────────── -->
        <Card>
          <CardHeader class="flex-row items-center justify-between space-y-0">
            <CardTitle class="text-base">Partition</CardTitle>
            <span class="text-xs text-muted-foreground">
              {{ subsystems.length }} subsystem area{{ subsystems.length === 1 ? '' : 's' }}
            </span>
          </CardHeader>
          <CardContent class="p-0">
            <div v-if="!subsystems.length" class="p-4 text-sm text-muted-foreground">
              No subsystem areas — the exclusive partition is empty.
            </div>
            <ul v-else class="divide-y divide-border">
              <li v-for="row in partitionRows" :key="`${row.type}:${row.key}`">
                <!-- Implicit parent: a group header derived from key segments -->
                <div
                  v-if="row.type === 'group'"
                  class="flex items-center gap-1.5 bg-muted/40 py-1.5 pr-3 text-xs font-semibold text-muted-foreground"
                  :style="indent(row.depth)"
                >
                  <FolderTree class="h-3.5 w-3.5 shrink-0" />
                  <span class="truncate font-mono">{{ row.name }}/</span>
                  <span v-if="row.fileTotal != null" class="ml-auto font-normal tabular-nums">
                    {{ n(row.fileTotal) }} files
                  </span>
                </div>

                <template v-else-if="row.area">
                  <RouterLink
                    :to="{ name: 'area-detail', params: { uid: row.area.uid } }"
                    class="flex items-start gap-2 py-2 pr-3 transition-colors hover:bg-accent/50"
                    :class="{ 'opacity-60': !row.area.enabled }"
                    :style="indent(row.depth)"
                  >
                    <button
                      type="button"
                      class="mt-0.5 rounded-sm p-1 text-muted-foreground hover:text-foreground"
                      :title="row.area.spec ? 'Toggle spec preview' : 'No spec yet'"
                      :disabled="!row.area.spec"
                      @click.stop.prevent="toggleSpec(row.area.uid)"
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
                    <span v-if="row.fileTotal != null" class="shrink-0 text-xs tabular-nums text-muted-foreground">
                      {{ n(row.fileTotal) }} files
                    </span>
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

        <!-- ── Features: cross-cutting spec overlays ─────────────────────── -->
        <Card v-if="features.length">
          <CardHeader class="flex-row items-center justify-between space-y-0">
            <CardTitle class="text-base">Features</CardTitle>
            <span class="text-xs text-muted-foreground">
              {{ features.length }} overlay{{ features.length === 1 ? '' : 's' }} — audited against their spec, on top of the partition
            </span>
          </CardHeader>
          <CardContent class="p-0">
            <ul class="divide-y divide-border">
              <li v-for="a in features" :key="a.uid">
                <RouterLink
                  :to="{ name: 'area-detail', params: { uid: a.uid } }"
                  class="flex items-start gap-2 border-l-2 border-l-primary/60 py-2 pl-2.5 pr-3 transition-colors hover:bg-accent/50"
                  :class="{ 'opacity-60': !a.enabled }"
                >
                  <button
                    type="button"
                    class="mt-0.5 rounded-sm p-1 text-muted-foreground hover:text-foreground"
                    :title="a.spec ? 'Toggle spec preview' : 'No spec yet'"
                    :disabled="!a.spec"
                    @click.stop.prevent="toggleSpec(a.uid)"
                  >
                    <component :is="expandedSpecs.has(a.uid) ? ChevronDown : ChevronRight" class="h-3.5 w-3.5" />
                  </button>
                  <div class="min-w-0 flex-1">
                    <div class="flex flex-wrap items-center gap-1.5">
                      <span class="truncate text-sm font-medium">{{ a.title || a.key }}</span>
                      <Badge variant="info" class="px-1.5 text-[10px]">feature</Badge>
                      <Badge v-if="!a.enabled" variant="outline" class="px-1.5 text-[10px]">disabled</Badge>
                      <span
                        v-if="a.stale"
                        class="h-2 w-2 shrink-0 rounded-full bg-amber-500"
                        :title="areaStaleTitle(a)"
                      />
                      <Badge v-if="a.pending_edits > 0" variant="warn" class="px-1.5 text-[10px]" title="Pending agent edits">
                        {{ a.pending_edits }}
                      </Badge>
                      <span
                        v-if="a.scope_paths.length"
                        class="rounded-full border border-border px-2 py-0.5 text-[10px] text-muted-foreground"
                        :title="a.scope_paths.join('\n')"
                      >
                        spans {{ a.scope_paths.length }} path{{ a.scope_paths.length === 1 ? '' : 's' }}
                      </span>
                    </div>
                    <div class="truncate font-mono text-[10px] text-muted-foreground">{{ a.key }}</div>
                  </div>
                </RouterLink>
                <div
                  v-if="expandedSpecs.has(a.uid) && a.spec"
                  class="mb-2 ml-9 mr-3 rounded-md border border-border p-3"
                >
                  <MarkdownView :model-value="a.spec" preview-only />
                </div>
              </li>
            </ul>
          </CardContent>
        </Card>

        <!-- ── Ignored: not auditable, spec says why ─────────────────────── -->
        <Card v-if="ignored.length">
          <CardContent class="p-0">
            <details>
              <summary class="cursor-pointer select-none px-4 py-3 text-sm font-medium text-muted-foreground hover:text-foreground">
                Ignored ({{ ignored.length }}) — not auditable; the reason lives in the spec
              </summary>
              <ul class="divide-y divide-border border-t border-border">
                <li v-for="a in ignored" :key="a.uid">
                  <RouterLink
                    :to="{ name: 'area-detail', params: { uid: a.uid } }"
                    class="flex items-baseline gap-2 px-4 py-2 text-muted-foreground transition-colors hover:bg-accent/50"
                    :class="{ 'opacity-60': !a.enabled }"
                  >
                    <span class="shrink-0 font-mono text-xs">{{ a.key }}</span>
                    <span class="min-w-0 truncate text-xs italic" :title="a.spec">{{ specSummary(a) || 'no reason recorded' }}</span>
                    <Badge v-if="!a.enabled" variant="outline" class="shrink-0 px-1.5 text-[10px]">disabled</Badge>
                  </RouterLink>
                </li>
              </ul>
            </details>
          </CardContent>
        </Card>
      </template>
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
