<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ChevronRight, FolderTree, Layers, TriangleAlert } from 'lucide-vue-next'
import { useCampaignStore } from '@/stores/campaignStore'
import { useAreaStore } from '@/stores/areaStore'
import { useLensStore } from '@/stores/lensStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { buildTreeRows } from '@/lib/treeRows'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
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
import type {
  AgentEffort,
  AreaDTO,
  CampaignDTO,
  CampaignKind,
  CampaignSelection,
  CreateCampaignRequest,
  LensDTO,
} from '@/types/api'

const props = defineProps<{
  open: boolean
  repositoryUid: string
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  created: [campaign: CampaignDTO]
}>()

const campaigns = useCampaignStore()
const areaStore = useAreaStore()
const lensStore = useLensStore()
const toast = useToast()

// ── Core form state ──────────────────────────────────────────────────────────

const kind = ref<CampaignKind>('subsystem')
const selection = ref<CampaignSelection>('all')
const k = ref(3)
const effort = ref<AgentEffort | 'default'>('default')
const maxParallel = ref(2)
const title = ref('')
const creating = ref(false)

// ── Areas (for coverage picker) ──────────────────────────────────────────────

const allAreas = ref<AreaDTO[]>([])
const loadingAreas = ref(false)

/** Areas filtered by the current kind (subsystem → kind==='subsystem' leaves,
 *  feature → kind==='feature' leaves). Group + ignore areas are excluded from
 *  coverage selection. */
const kindAreas = computed<AreaDTO[]>(() => {
  if (kind.value === 'global' || kind.value === 'batch') return []
  const targetKind = kind.value === 'subsystem' ? 'subsystem' : 'feature'
  return allAreas.value.filter((a) => a.kind === targetKind && a.enabled)
})

const kindAreaTreeRows = computed(() => buildTreeRows(kindAreas.value, (a) => a.key))

/** Keys the user has explicitly selected; empty = whole tree (all). */
const coverageKeys = ref<Set<string>>(new Set())

function toggleCoverageKey(key: string) {
  const next = new Set(coverageKeys.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  coverageKeys.value = next
}

function toggleGroupCoverage(groupKey: string) {
  // Toggle all areas whose key starts with groupKey (the group's subtree)
  const inGroup = kindAreas.value.filter((a) => a.key === groupKey || a.key.startsWith(groupKey + '/'))
  const allSelected = inGroup.every((a) => coverageKeys.value.has(a.key))
  const next = new Set(coverageKeys.value)
  for (const a of inGroup) {
    if (allSelected) next.delete(a.key)
    else next.add(a.key)
  }
  coverageKeys.value = next
}

function isGroupPartiallySelected(groupKey: string): boolean {
  const inGroup = kindAreas.value.filter((a) => a.key === groupKey || a.key.startsWith(groupKey + '/'))
  const selectedCount = inGroup.filter((a) => coverageKeys.value.has(a.key)).length
  return selectedCount > 0 && selectedCount < inGroup.length
}

function isGroupFullySelected(groupKey: string): boolean {
  const inGroup = kindAreas.value.filter((a) => a.key === groupKey || a.key.startsWith(groupKey + '/'))
  return inGroup.length > 0 && inGroup.every((a) => coverageKeys.value.has(a.key))
}

/** Depth-based left padding for tree rows. */
function indent(depth: number) {
  return { paddingLeft: `${12 + depth * 18}px` }
}

// ── Lenses ───────────────────────────────────────────────────────────────────

const allLenses = ref<LensDTO[]>([])
const loadingLenses = ref(false)
const selectedLensKeys = ref<Set<string>>(new Set())

/** Compute the default pre-checked lens set for the current kind:
 *  - subsystem → lenses whose global_agent_key is EMPTY (local/subsystem lenses)
 *  - feature   → lens with key 'implementation-gaps' (if present)
 *  - global    → lenses whose global_agent_key is non-empty
 *  - batch     → n/a (no lens picker shown) */
function defaultLensKeysForKind(k: CampaignKind, lenses: LensDTO[]): Set<string> {
  if (k === 'subsystem') {
    return new Set(lenses.filter((l) => l.enabled && !l.global_agent_key).map((l) => l.key))
  }
  if (k === 'feature') {
    return new Set(lenses.filter((l) => l.enabled && l.key === 'implementation-gaps').map((l) => l.key))
  }
  if (k === 'global') {
    return new Set(lenses.filter((l) => l.enabled && l.global_agent_key).map((l) => l.key))
  }
  return new Set()
}

function toggleLens(key: string) {
  const next = new Set(selectedLensKeys.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  selectedLensKeys.value = next
}

// Re-derive lens defaults whenever kind changes (after lenses are loaded).
watch(kind, (newKind) => {
  if (allLenses.value.length) {
    selectedLensKeys.value = defaultLensKeysForKind(newKind, allLenses.value)
  }
  // Also reset coverage when kind changes
  coverageKeys.value = new Set()
})

// ── Live plan preview ─────────────────────────────────────────────────────────

const planPreview = ref<{
  total_runs: number
  by_kind: { area?: number; feature?: number; global?: number }
  uncovered_files: number
  degraded: string
  source: string
} | null>(null)
const loadingPreview = ref(false)
let previewDebounce: number | undefined
let previewGeneration = 0

/** Build the request body from current form state. */
function buildPreviewBody(): CreateCampaignRequest {
  return {
    kind: kind.value,
    coverage_keys: coverageKeys.value.size > 0 ? [...coverageKeys.value] : [],
    selection: selection.value,
    lens_keys: [...selectedLensKeys.value],
    effort: effort.value === 'default' ? '' : effort.value,
    k: selection.value === 'rotation' ? k.value : undefined,
  }
}

function schedulePreview() {
  window.clearTimeout(previewDebounce)
  loadingPreview.value = true
  previewDebounce = window.setTimeout(async () => {
    const gen = ++previewGeneration
    try {
      const result = await campaigns.previewPlan(props.repositoryUid, buildPreviewBody())
      if (gen === previewGeneration) planPreview.value = result
    } catch {
      if (gen === previewGeneration) planPreview.value = null
    } finally {
      if (gen === previewGeneration) loadingPreview.value = false
    }
  }, 300)
}

// Trigger preview whenever any relevant field changes.
watch([kind, coverageKeys, selection, selectedLensKeys, k], schedulePreview, { deep: true })

onBeforeUnmount(() => window.clearTimeout(previewDebounce))

const previewSummary = computed(() => {
  const p = planPreview.value
  if (!p) return ''
  const n = (x: number) => x.toLocaleString('en-US')
  const parts = [`≈ ${n(p.total_runs)} run${p.total_runs === 1 ? '' : 's'}`]
  const bk = p.by_kind
  const kindParts: string[] = []
  if (bk.area) kindParts.push(`${n(bk.area)} area`)
  if (bk.feature) kindParts.push(`${n(bk.feature)} feature`)
  if (bk.global) kindParts.push(`${n(bk.global)} global`)
  if (kindParts.length) parts.push(kindParts.join(' + '))
  if (p.uncovered_files > 0) parts.push(`${n(p.uncovered_files)} uncovered`)
  return parts.join(' · ')
})

// ── Dialog open / reset ───────────────────────────────────────────────────────

watch(
  () => props.open,
  async (open) => {
    if (!open) return
    kind.value = 'subsystem'
    selection.value = 'all'
    k.value = 3
    effort.value = 'default'
    maxParallel.value = 2
    title.value = ''
    coverageKeys.value = new Set()
    planPreview.value = null
    previewGeneration++

    // Load areas for the coverage picker.
    loadingAreas.value = true
    areaStore
      .fetchAreas(props.repositoryUid)
      .then((data) => (allAreas.value = data))
      .catch(() => (allAreas.value = []))
      .finally(() => (loadingAreas.value = false))

    // Load lenses and pre-check defaults for the initial kind.
    loadingLenses.value = true
    try {
      const lenses = await lensStore.fetchAll()
      allLenses.value = lenses
      selectedLensKeys.value = defaultLensKeysForKind(kind.value, lenses)
    } catch (e) {
      toast.error("Couldn't load lenses", e instanceof Error ? e.message : String(e))
    } finally {
      loadingLenses.value = false
    }

    // Kick off the first preview after data is loaded.
    schedulePreview()
  },
)

// ── Submit ────────────────────────────────────────────────────────────────────

const canCreate = computed(() => {
  if (creating.value || !props.repositoryUid) return false
  if (kind.value !== 'batch' && selectedLensKeys.value.size === 0) return false
  return true
})

async function create() {
  if (!canCreate.value) return
  creating.value = true
  try {
    const campaign = await campaigns.create(props.repositoryUid, {
      kind: kind.value,
      coverage_keys: coverageKeys.value.size > 0 ? [...coverageKeys.value] : [],
      selection: selection.value,
      lens_keys: kind.value !== 'batch' ? [...selectedLensKeys.value] : [],
      effort: effort.value === 'default' ? '' : effort.value,
      k: selection.value === 'rotation' ? k.value : undefined,
      max_parallel: Number.isFinite(maxParallel.value) ? Math.max(Math.trunc(maxParallel.value), 1) : undefined,
      title: title.value.trim() || undefined,
    })
    emit('created', campaign)
    emit('update:open', false)
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error("Couldn't create campaign", msg)
  } finally {
    creating.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="emit('update:open', $event)">
    <DialogContent class="sm:max-w-2xl">
      <DialogHeader>
        <DialogTitle>New campaign</DialogTitle>
        <DialogDescription>
          Plan a bounded audit sweep of this repository. Nothing runs until you launch it.
        </DialogDescription>
      </DialogHeader>

      <div class="space-y-3">
        <!-- ── Kind + Effort ── -->
        <div class="grid gap-3 md:grid-cols-2">
          <div class="space-y-1.5">
            <Label>Kind</Label>
            <Select :model-value="kind" @update:model-value="kind = $event as CampaignKind">
              <SelectTrigger class="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="subsystem">Subsystem — area-by-area audit</SelectItem>
                <SelectItem value="feature">Feature — feature-by-feature audit</SelectItem>
                <SelectItem value="global">Global — whole-repo sweeps</SelectItem>
                <SelectItem value="batch">Audit everything — subsystem + feature + global</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div class="space-y-1.5">
            <Label>Effort</Label>
            <Select :model-value="effort" @update:model-value="effort = $event as AgentEffort | 'default'">
              <SelectTrigger class="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="default">Default — areas normal, sweeps deep</SelectItem>
                <SelectItem value="short">Short</SelectItem>
                <SelectItem value="normal">Normal</SelectItem>
                <SelectItem value="deep">Deep</SelectItem>
                <SelectItem value="unlimited">Unlimited</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        <!-- ── Selection + k ── -->
        <div class="grid gap-3 md:grid-cols-2">
          <div class="space-y-1.5">
            <Label>Selection</Label>
            <Select :model-value="selection" @update:model-value="selection = $event as CampaignSelection">
              <SelectTrigger class="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All — every area in scope</SelectItem>
                <SelectItem value="stale">Stale — code changed since last review</SelectItem>
                <SelectItem value="rotation">Rotation — k areas per pass</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div v-if="selection === 'rotation'" class="space-y-1.5">
            <Label for="campaign-k">Areas per pass (k)</Label>
            <Input id="campaign-k" v-model.number="k" type="number" min="1" class="max-w-32" />
          </div>
          <div v-else class="space-y-1.5">
            <Label for="campaign-max-parallel">Max parallel runs</Label>
            <Input
              id="campaign-max-parallel"
              v-model.number="maxParallel"
              type="number"
              min="1"
              class="max-w-32"
            />
            <p class="text-xs text-muted-foreground">How many part runs execute at once.</p>
          </div>
        </div>
        <div v-if="selection === 'rotation'" class="space-y-1.5">
          <Label for="campaign-max-parallel-rotation">Max parallel runs</Label>
          <Input
            id="campaign-max-parallel-rotation"
            v-model.number="maxParallel"
            type="number"
            min="1"
            class="max-w-32"
          />
          <p class="text-xs text-muted-foreground">How many part runs execute at once.</p>
        </div>

        <!-- ── Live plan preview ── -->
        <div class="rounded-md border bg-muted/30 px-3 py-2 text-sm">
          <div v-if="loadingPreview || (!planPreview && (loadingAreas || loadingLenses))" class="text-muted-foreground">
            Sizing plan…
          </div>
          <template v-else-if="planPreview">
            <div class="flex flex-wrap items-center gap-2">
              <span class="font-medium">{{ previewSummary }}</span>
              <Badge v-if="planPreview.source === 'batch'" variant="outline" class="px-1.5 text-[10px]">batch</Badge>
              <Badge v-else-if="planPreview.source" variant="outline" class="px-1.5 text-[10px]" title="Partition source">
                {{ planPreview.source === 'area-map' ? 'area map' : planPreview.source }}
              </Badge>
              <Badge v-if="planPreview.degraded" variant="warn" class="px-1.5 text-[10px]" :title="planPreview.degraded">
                <TriangleAlert class="h-3 w-3" /> degraded
              </Badge>
            </div>
            <p v-if="kind === 'batch'" class="mt-1 text-xs text-muted-foreground">
              Creates three child campaigns (subsystem + feature + global) that run independently.
            </p>
          </template>
          <div v-else class="text-muted-foreground">Plan preview unavailable.</div>
        </div>

        <!-- ── Coverage picker (subsystem / feature only) ── -->
        <div v-if="kind !== 'global' && kind !== 'batch'" class="space-y-1.5">
          <Label>Coverage <span class="font-normal text-muted-foreground">(optional — empty = whole tree)</span></Label>
          <div class="max-h-48 overflow-y-auto rounded-md border">
            <div v-if="loadingAreas" class="p-3 text-sm text-muted-foreground">Loading areas…</div>
            <div v-else-if="!kindAreas.length" class="p-3 text-sm text-muted-foreground">
              No {{ kind }} areas available.
            </div>
            <template v-else>
              <div
                v-for="row in kindAreaTreeRows"
                :key="`${row.type}:${row.key}`"
              >
                <!-- Group row: toggle all areas in the subtree -->
                <button
                  v-if="row.type === 'group'"
                  type="button"
                  class="flex w-full items-center gap-2 bg-muted/40 py-1.5 pr-3 text-left text-xs font-semibold text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground"
                  :style="indent(row.depth)"
                  @click="toggleGroupCoverage(row.key)"
                >
                  <input
                    type="checkbox"
                    class="h-3.5 w-3.5 cursor-pointer accent-primary"
                    :checked="isGroupFullySelected(row.key)"
                    :indeterminate="isGroupPartiallySelected(row.key)"
                    tabindex="-1"
                    @click.stop
                    @change="toggleGroupCoverage(row.key)"
                  />
                  <FolderTree class="h-3.5 w-3.5 shrink-0" />
                  <span class="truncate font-mono">{{ row.name }}/</span>
                </button>

                <!-- Leaf row -->
                <label
                  v-else-if="row.type === 'leaf'"
                  class="flex cursor-pointer items-start gap-2 px-3 py-1.5 text-sm hover:bg-accent"
                  :style="indent(row.depth)"
                >
                  <input
                    type="checkbox"
                    class="mt-0.5 h-3.5 w-3.5 cursor-pointer accent-primary"
                    :checked="coverageKeys.has(row.key)"
                    @change="toggleCoverageKey(row.key)"
                  />
                  <span class="min-w-0">
                    <span class="block truncate font-medium">{{ row.item.title || row.name }}</span>
                    <span class="block font-mono text-[10px] text-muted-foreground">{{ row.key }}</span>
                  </span>
                </label>
              </div>
            </template>
          </div>
          <p v-if="coverageKeys.size > 0" class="text-xs text-muted-foreground">
            {{ coverageKeys.size }} area{{ coverageKeys.size === 1 ? '' : 's' }} selected
          </p>
        </div>

        <!-- ── Lenses (hidden for batch) ── -->
        <div v-if="kind !== 'batch'" class="space-y-1.5">
          <Label>Lenses</Label>
          <p class="text-xs text-muted-foreground">
            Lenses narrow what audit runs check. Pre-checked defaults match the selected kind.
          </p>
          <div class="max-h-48 overflow-y-auto rounded-md border">
            <div v-if="loadingLenses" class="p-3 text-sm text-muted-foreground">Loading…</div>
            <div v-else-if="!allLenses.length" class="p-3 text-sm text-muted-foreground">
              No lenses available.
            </div>
            <label
              v-for="l in allLenses"
              v-else
              :key="l.key"
              class="flex cursor-pointer items-start gap-2.5 px-3 py-2 text-sm hover:bg-accent"
            >
              <input
                type="checkbox"
                class="mt-1 h-4 w-4 cursor-pointer accent-primary"
                :checked="selectedLensKeys.has(l.key)"
                @change="toggleLens(l.key)"
              />
              <span class="min-w-0">
                <span class="flex flex-wrap items-center gap-1.5 font-medium">
                  {{ l.title || l.key }}
                  <Badge v-if="!l.enabled" variant="outline" class="px-1.5 text-[10px]">disabled</Badge>
                  <Badge v-if="l.global_agent_key" variant="secondary" class="px-1.5 text-[10px]">global</Badge>
                </span>
                <span class="block font-mono text-xs text-muted-foreground">{{ l.key }}</span>
              </span>
            </label>
          </div>
        </div>

        <!-- ── Title ── -->
        <div class="space-y-1.5">
          <Label for="campaign-title">Title (optional)</Label>
          <Input id="campaign-title" v-model="title" placeholder="What is this sweep for?" />
        </div>
      </div>

      <DialogFooter>
        <Button variant="ghost" size="sm" @click="emit('update:open', false)">Cancel</Button>
        <Button size="sm" :disabled="!canCreate" :loading="creating" @click="create">
          <Layers /> Create campaign
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
