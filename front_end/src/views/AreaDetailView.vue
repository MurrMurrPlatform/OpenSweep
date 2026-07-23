<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import {
  Activity,
  AlertTriangle,
  ArrowLeft,
  BookOpen,
  ChevronDown,
  ChevronRight,
  FolderTree,
  Layers,
  Pencil,
  Sparkles,
  Trash2,
  X,
} from 'lucide-vue-next'
import { useAreaStore } from '@/stores/areaStore'
import { useRepositoryStore } from '@/stores/repositoryStore'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { areaKindHelp, areaKindVariant, areaStaleTitle } from '@/lib/areas'
import { buildTreeRows } from '@/lib/treeRows'
import { formatRelativeTime } from '@/lib/utils'
import AreaEditDialog from '@/components/areas/AreaEditDialog.vue'
import AreaEditReviewCard from '@/components/areas/AreaEditReviewCard.vue'
import { MarkdownView } from '@/components/ui/markdown'
import { PageHeader } from '@/components/ui/page-header'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Skeleton } from '@/components/ui/skeleton'
import { ErrorState } from '@/components/ui/error-state'
import { Textarea } from '@/components/ui/textarea'
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
import type { AreaDetailDTO, AreaEditDTO } from '@/types/api'

const route = useRoute()
const router = useRouter()
const areaStore = useAreaStore()
const repositories = useRepositoryStore()
const toast = useToast()

const uid = computed(() => String(route.params.uid))
const detail = ref<AreaDetailDTO | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)

const area = computed(() => detail.value?.area ?? null)

// ── Back link (list views are repo-scoped; details are flat) ─────────────────
const repoSlug = computed(() => {
  const repoUid = area.value?.repository_uid
  return repoUid ? repositories.find(repoUid)?.slug ?? null : null
})

onMounted(load)
watch(uid, () => void load())

async function load() {
  loading.value = true
  error.value = null
  try {
    detail.value = await areaStore.fetchDetail(uid.value)
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

/** Silent refetch after a mutation — keeps the page current without a spinner. */
async function refresh() {
  try {
    detail.value = await areaStore.fetchDetail(uid.value)
  } catch {
    /* transient — the page keeps its last good state */
  }
}

const keySegments = computed(() => (area.value?.key ?? '').split('/').filter(Boolean))

/** Surface PATCH partition warnings the same way everywhere. */
function toastPatch(title: string, key: string, warnings: string[]) {
  if (warnings.length) toast.warn(`${title} with warnings`, warnings.join('; '))
  else toast.success(title, key)
}

// ── Enabled toggle (inline PATCH) ────────────────────────────────────────────

const togglingEnabled = ref(false)

async function setEnabled(enabled: boolean) {
  const a = area.value
  if (!a || togglingEnabled.value) return
  togglingEnabled.value = true
  try {
    const { area: saved, warnings } = await areaStore.patchArea(a.uid, { enabled })
    toastPatch(enabled ? 'Area enabled' : 'Area disabled', saved.key, warnings)
    await refresh()
  } catch (e: unknown) {
    toast.error('Update failed', e instanceof Error ? e.message : String(e))
  } finally {
    togglingEnabled.value = false
  }
}

// ── Scope: expandable file list per path ─────────────────────────────────────

const expandedPaths = ref<Set<string>>(new Set())

function togglePath(path: string) {
  const next = new Set(expandedPaths.value)
  if (next.has(path)) next.delete(path)
  else next.add(path)
  expandedPaths.value = next
}

// ── Coverage display ─────────────────────────────────────────────────────────

function outcomeVariant(outcome: string) {
  if (['covered', 'done', 'pass', 'clean'].includes(outcome)) return 'success' as const
  if (['failed', 'fail', 'error'].includes(outcome)) return 'destructive' as const
  if (['skipped', 'partial'].includes(outcome)) return 'warn' as const
  return 'secondary' as const
}

function verdictVariant(verdict: string) {
  if (['pass', 'ok', 'clean'].includes(verdict)) return 'success' as const
  if (['fail', 'violation', 'violations'].includes(verdict)) return 'destructive' as const
  if (['warn', 'attention', 'partial'].includes(verdict)) return 'warn' as const
  return 'secondary' as const
}

// ── Pending edit review (shared card, same semantics as the list view) ───────

const resolvingUid = ref('')

async function acceptEdit(edit: AreaEditDTO) {
  if (resolvingUid.value) return
  resolvingUid.value = edit.uid
  try {
    const { area: saved, warnings } = await areaStore.acceptEdit(edit.uid)
    if (warnings.length) toast.warn(`Accepted ${saved.key} with warnings`, warnings.join('; '))
    else toast.success('Edit accepted', saved.key)
    await refresh()
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
    await refresh()
  } catch (e: unknown) {
    toast.error('Reject failed', e instanceof Error ? e.message : String(e))
  } finally {
    resolvingUid.value = ''
  }
}

// ── Feature hierarchy (parent feature → sub-feature leaves) ──────────────────

/** Leaves lacking a spec get skipped at audit time — surface the warning + fix. */
const specMissingLeaves = computed(() =>
  (detail.value?.sub_features ?? []).filter((f) => !f.has_spec),
)

/** Aggregated coverage across the parent's sub-feature leaves. */
const featureRollup = computed(() => {
  const leaves = detail.value?.sub_features ?? []
  return {
    total: leaves.length,
    covered: leaves.filter((f) => f.coverage_count > 0).length,
    stale: leaves.filter((f) => f.stale).length,
    missingSpec: leaves.filter((f) => !f.has_spec).length,
  }
})

/** Sub-feature list rendered as a hierarchy. */
const subFeatureTreeRows = computed(() =>
  buildTreeRows(detail.value?.sub_features ?? [], (f) => f.key),
)

const expandedSpecs = ref<Set<string>>(new Set())
function toggleSpec(key: string) {
  const next = new Set(expandedSpecs.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expandedSpecs.value = next
}

/** Depth-based left padding for sub-feature tree rows (matches AreasView). */
function indentSubFeature(depth: number) {
  return { paddingLeft: `${12 + depth * 20}px` }
}

/** Whether any other sub-feature is nested under this key. */
function hasSubFeatureChildren(key: string): boolean {
  return (detail.value?.sub_features ?? []).some((f) => f.key.startsWith(key + '/'))
}

const collapsedSubFeatureKeys = ref<Set<string>>(new Set())
function toggleSubFeatureCollapse(key: string) {
  const next = new Set(collapsedSubFeatureKeys.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  collapsedSubFeatureKeys.value = next
}

const visibleSubFeatureRows = computed(() =>
  subFeatureTreeRows.value.filter((row) => {
    const segments = row.key.split('/')
    for (let i = 1; i < segments.length; i++) {
      if (collapsedSubFeatureKeys.value.has(segments.slice(0, i).join('/'))) return false
    }
    return true
  }),
)

const generatingSpecs = ref(false)
async function generateSpecs() {
  const repoUid = area.value?.repository_uid
  if (!repoUid || generatingSpecs.value) return
  generatingSpecs.value = true
  try {
    const result = await areaStore.generateSpecs(repoUid)
    const targets = result.targets.length
      ? `drafting specs for: ${result.targets.join(', ')}`
      : undefined
    if (result.errors.length) toast.error('Generate specs partially dispatched', result.errors.join(' · '))
    else toast.success(result.summary || 'Generate specs dispatched', targets)
  } catch (e: unknown) {
    // 409 = nothing needs a spec right now.
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    if (e instanceof ApiError && e.status === 409) toast.success('Specs are up to date', msg)
    else toast.error('Couldn’t generate specs', msg)
  } finally {
    generatingSpecs.value = false
  }
}

// ── Inline spec editor ───────────────────────────────────────────────────────

const specEditing = ref(false)
const specDraft = ref('')
const specSaving = ref(false)

function startSpecEdit() {
  specDraft.value = area.value?.spec ?? ''
  specEditing.value = true
}

function cancelSpecEdit() {
  specEditing.value = false
  specDraft.value = ''
}

async function saveSpec() {
  const a = area.value
  if (!a || specSaving.value) return
  specSaving.value = true
  try {
    const { area: saved, warnings } = await areaStore.patchArea(a.uid, { spec: specDraft.value })
    toastPatch('Spec saved', saved.key, warnings)
    specEditing.value = false
    specDraft.value = ''
    await refresh()
  } catch (e: unknown) {
    toast.error("Couldn't save spec", e instanceof Error ? e.message : String(e))
  } finally {
    specSaving.value = false
  }
}

// ── Revise with AI ────────────────────────────────────────────────────────────

const reviseInstruction = ref('')
const revising = ref(false)

async function reviseSpec() {
  const a = area.value
  const instruction = reviseInstruction.value.trim()
  if (!a || !instruction || revising.value) return
  revising.value = true
  try {
    const result = await areaStore.reviseSpec(a.uid, instruction)
    reviseInstruction.value = ''
    toast.success(
      'AI revision dispatched',
      `Run ${result.run_uid.slice(0, 8)} — a pending edit will appear in the review queue when it finishes.`,
    )
  } catch (e: unknown) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error("Couldn't dispatch revision", msg)
  } finally {
    revising.value = false
  }
}

// ── Danger row: edit + delete ────────────────────────────────────────────────

const editOpen = ref(false)
const deleteOpen = ref(false)

async function confirmDelete() {
  const a = area.value
  if (!a) return
  deleteOpen.value = false
  try {
    await areaStore.deleteArea(a.uid)
    toast.success('Area deleted', a.key)
    void router.push(repoSlug.value ? { name: 'areas', params: { repoSlug: repoSlug.value } } : { name: 'root' })
  } catch (e: unknown) {
    toast.error('Delete failed', e instanceof Error ? e.message : String(e))
  }
}
</script>

<template>
  <div class="space-y-4">
    <template v-if="loading && !detail">
      <Skeleton class="h-24" />
      <Skeleton class="h-40" />
      <Skeleton class="h-64" />
    </template>

    <ErrorState v-else-if="error && !detail" title="Couldn't load area" :message="error">
      <Button variant="outline" size="sm" @click="load">Retry</Button>
    </ErrorState>

    <template v-else-if="detail && area">
      <PageHeader :title="area.title || area.key">
        <template #breadcrumb>
          <RouterLink
            v-if="repoSlug"
            :to="{ name: 'areas', params: { repoSlug } }"
            class="mb-1 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft class="h-3 w-3" /> Areas
          </RouterLink>
          <div class="mb-1 flex flex-wrap items-center gap-2">
            <span class="font-mono text-xs text-muted-foreground">
              <template v-for="(seg, i) in keySegments" :key="i">
                <span v-if="i > 0" class="text-muted-foreground/50"> / </span>
                <span :class="{ 'text-foreground': i === keySegments.length - 1 }">{{ seg }}</span>
              </template>
            </span>
            <Badge :variant="areaKindVariant(area.kind)" class="px-1.5 text-[10px]" :title="areaKindHelp(area.kind)">{{ area.kind }}</Badge>
            <span
              v-if="area.stale"
              class="h-2 w-2 shrink-0 rounded-full bg-amber-500"
              :title="areaStaleTitle(area)"
            />
            <span v-if="area.last_reviewed_at" class="text-xs text-muted-foreground" :title="area.last_reviewed_at">
              reviewed {{ formatRelativeTime(area.last_reviewed_at) }}
            </span>
            <span v-if="area.code_changed_at" class="text-xs text-muted-foreground" :title="area.code_changed_at">
              code changed {{ formatRelativeTime(area.code_changed_at) }}
            </span>
          </div>
        </template>

        <div class="flex items-center gap-2" title="Disabled areas are ignored by planning">
          <Switch
            :model-value="area.enabled"
            :disabled="togglingEnabled"
            @update:model-value="setEnabled($event)"
          />
          <span class="text-xs text-muted-foreground">{{ area.enabled ? 'enabled' : 'disabled' }}</span>
        </div>
      </PageHeader>

      <!-- ── Spec ────────────────────────────────────────────────────────── -->
      <Card>
        <CardHeader class="flex-row flex-wrap items-center justify-between gap-2 space-y-0 pb-2">
          <CardTitle class="text-base">{{ detail.is_feature_parent ? 'Charter (optional)' : 'Spec' }}</CardTitle>
          <Button v-if="!specEditing" variant="ghost" size="sm" @click="startSpecEdit">
            <Pencil class="h-3.5 w-3.5" /> Edit
          </Button>
        </CardHeader>
        <CardContent class="space-y-3">
          <!-- Inline editor -->
          <template v-if="specEditing">
            <Textarea
              v-model="specDraft"
              class="min-h-40 font-mono text-xs"
              placeholder="Spec markdown…"
            />
            <div class="flex items-center gap-2">
              <Button size="sm" :loading="specSaving" @click="saveSpec">Save</Button>
              <Button variant="ghost" size="sm" :disabled="specSaving" @click="cancelSpecEdit">
                <X class="h-3.5 w-3.5" /> Cancel
              </Button>
            </div>
          </template>
          <template v-else>
            <MarkdownView v-if="area.spec" :model-value="area.spec" preview-only />
            <p v-else class="text-sm text-muted-foreground">
              {{ detail.is_feature_parent
                ? 'No charter — a parent feature groups its sub-features; each sub-feature carries its own spec below.'
                : 'No spec yet — click Edit to add one, or use Revise with AI below.' }}
            </p>
          </template>

          <!-- Revise with AI -->
          <div v-if="!specEditing" class="border-t border-border pt-3">
            <p class="mb-2 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
              <Sparkles class="h-3.5 w-3.5" /> Revise with AI
            </p>
            <div class="flex gap-2">
              <Input
                v-model="reviseInstruction"
                placeholder="e.g. make criterion 2 more concrete"
                class="text-sm"
                @keydown.enter.prevent="reviseSpec"
              />
              <Button
                size="sm"
                variant="outline"
                :disabled="!reviseInstruction.trim() || revising"
                :loading="revising"
                @click="reviseSpec"
              >
                Revise
              </Button>
            </div>
            <p class="mt-1.5 text-xs text-muted-foreground">
              Dispatches an AI run that proposes a revised spec as a pending edit for your review.
            </p>
          </div>
        </CardContent>
      </Card>

      <!-- ── Feature hierarchy (parent feature → sub-feature leaves) ──────── -->
      <Card v-if="detail.is_feature_parent">
        <CardHeader class="flex-row flex-wrap items-center justify-between gap-2 space-y-0 pb-2">
          <CardTitle class="flex items-center gap-2 text-base">
            <Layers class="h-4 w-4 text-muted-foreground" /> Sub-features
            <span class="text-xs font-normal text-muted-foreground">
              · {{ featureRollup.covered }}/{{ featureRollup.total }} covered
              <template v-if="featureRollup.stale"> · {{ featureRollup.stale }} stale</template>
            </span>
          </CardTitle>
          <Button
            variant="outline"
            size="sm"
            :loading="generatingSpecs"
            title="Draft specs for sub-features lacking one and refresh stale specs — lands as pending area edits."
            @click="generateSpecs"
          >
            <Sparkles v-if="!generatingSpecs" /> Generate specs
          </Button>
        </CardHeader>
        <CardContent class="p-0">
          <!-- No spec → audit skipped warning -->
          <div
            v-if="specMissingLeaves.length"
            class="mx-4 mb-2 flex flex-wrap items-center gap-2 rounded-sm border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-800 dark:text-amber-300"
          >
            <AlertTriangle class="h-4 w-4 shrink-0" />
            <span class="min-w-0">
              {{ specMissingLeaves.length }} sub-feature{{ specMissingLeaves.length === 1 ? '' : 's' }} ha{{ specMissingLeaves.length === 1 ? 's' : 've' }}
              no spec — their audit is skipped until one exists.
            </span>
            <Button variant="outline" size="sm" class="ml-auto" :loading="generatingSpecs" @click="generateSpecs">
              <Sparkles v-if="!generatingSpecs" /> Generate specs
            </Button>
          </div>

          <p v-if="!detail.sub_features.length" class="px-4 pb-4 text-sm text-muted-foreground">
            No sub-features under this feature yet.
          </p>
          <ul v-else class="divide-y divide-border">
            <li v-for="row in visibleSubFeatureRows" :key="`${row.type}:${row.key}`">
              <!-- Implicit group header derived from key segments -->
              <button
                v-if="row.type === 'group'"
                type="button"
                class="flex w-full items-center gap-1.5 bg-muted/40 py-1.5 pr-3 text-left text-xs font-semibold text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground"
                :style="indentSubFeature(row.depth)"
                :title="collapsedSubFeatureKeys.has(row.key) ? 'Expand' : 'Collapse'"
                @click="toggleSubFeatureCollapse(row.key)"
              >
                <component :is="collapsedSubFeatureKeys.has(row.key) ? ChevronRight : ChevronDown" class="h-3.5 w-3.5 shrink-0" />
                <FolderTree class="h-3.5 w-3.5 shrink-0" />
                <span class="truncate font-mono">{{ row.name }}/</span>
              </button>

              <template v-else-if="row.type === 'leaf'">
                <div class="py-2 pr-4" :style="indentSubFeature(row.depth)">
                  <div class="flex flex-wrap items-center gap-2">
                    <button
                      v-if="hasSubFeatureChildren(row.key)"
                      type="button"
                      class="shrink-0 rounded-sm p-1 text-muted-foreground hover:text-foreground"
                      :title="collapsedSubFeatureKeys.has(row.key) ? 'Expand children' : 'Collapse children'"
                      @click="toggleSubFeatureCollapse(row.key)"
                    >
                      <component :is="collapsedSubFeatureKeys.has(row.key) ? ChevronRight : ChevronDown" class="h-3.5 w-3.5" />
                    </button>
                    <RouterLink
                      :to="{ name: 'area-detail', params: { uid: row.item.uid } }"
                      class="min-w-0 truncate text-sm font-medium hover:underline"
                    >
                      {{ row.item.title || row.item.key }}
                    </RouterLink>
                    <span class="truncate font-mono text-[10px] text-muted-foreground">{{ row.item.key }}</span>
                    <span
                      v-if="row.item.stale"
                      class="h-2 w-2 shrink-0 rounded-full bg-amber-500"
                      title="Code changed under this sub-feature's scope since its last review"
                    />
                    <Badge v-if="!row.item.has_spec" variant="warn" class="px-1.5 text-[10px]" title="No spec — audit skipped until one exists">no spec</Badge>
                    <Badge
                      :variant="row.item.coverage_count > 0 ? 'success' : 'secondary'"
                      class="ml-auto px-1.5 text-[10px]"
                      :title="`${row.item.coverage_count} coverage stamp${row.item.coverage_count === 1 ? '' : 's'}`"
                    >
                      {{ row.item.coverage_count > 0 ? `covered ×${row.item.coverage_count}` : 'not covered' }}
                    </Badge>
                    <button
                      v-if="row.item.spec"
                      type="button"
                      class="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                      @click="toggleSpec(row.item.key)"
                    >
                      <component :is="expandedSpecs.has(row.item.key) ? ChevronDown : ChevronRight" class="h-3.5 w-3.5" />
                      spec
                    </button>
                  </div>
                  <div v-if="row.item.spec && expandedSpecs.has(row.item.key)" class="mt-2 rounded-md border border-border p-3">
                    <MarkdownView :model-value="row.item.spec" preview-only />
                  </div>
                </div>
              </template>
            </li>
          </ul>
        </CardContent>
      </Card>

      <!-- ── Scope ───────────────────────────────────────────────────────── -->
      <Card>
        <CardHeader class="pb-2">
          <CardTitle class="text-base">Scope</CardTitle>
        </CardHeader>
        <CardContent class="p-0">
          <div
            v-if="detail.tree_degraded"
            class="mx-4 mb-2 flex items-center gap-2 rounded-sm border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-800 dark:text-amber-300"
          >
            {{ detail.tree_degraded }}
          </div>
          <p v-if="!detail.scope.length" class="px-4 pb-4 text-sm text-muted-foreground">No scope paths.</p>
          <table v-else class="w-full text-sm">
            <thead>
              <tr class="text-left text-xs uppercase tracking-wide text-muted-foreground">
                <th class="px-4 py-2 font-medium">Path</th>
                <th class="px-4 py-2 font-medium">Matches</th>
                <th class="px-4 py-2 font-medium">State</th>
              </tr>
            </thead>
            <tbody>
              <template v-for="entry in detail.scope" :key="entry.path">
                <tr class="border-t border-border">
                  <td class="px-4 py-2">
                    <button
                      type="button"
                      class="flex items-center gap-1 font-mono text-xs"
                      :class="entry.files.length ? 'hover:text-primary' : 'cursor-default'"
                      :title="entry.files.length ? 'Toggle matched files' : 'No matched files to show'"
                      @click="entry.files.length && togglePath(entry.path)"
                    >
                      <component
                        :is="expandedPaths.has(entry.path) ? ChevronDown : ChevronRight"
                        class="h-3.5 w-3.5 shrink-0"
                        :class="{ 'opacity-30': !entry.files.length }"
                      />
                      {{ entry.path }}
                    </button>
                  </td>
                  <td class="px-4 py-2 tabular-nums text-muted-foreground">
                    {{ entry.file_count ?? '—' }}
                  </td>
                  <td class="px-4 py-2">
                    <Badge v-if="entry.dead" variant="destructive" class="px-1.5 text-[10px]" title="This path matches nothing in the current tree">
                      dead
                    </Badge>
                    <span v-else class="text-xs text-muted-foreground">ok</span>
                  </td>
                </tr>
                <tr v-if="expandedPaths.has(entry.path) && entry.files.length">
                  <td colspan="3" class="px-4 pb-2">
                    <ul class="ml-5 max-h-56 space-y-0.5 overflow-y-auto rounded-md border border-border p-2">
                      <li v-for="f in entry.files" :key="f" class="font-mono text-[11px] text-muted-foreground">
                        {{ f }}
                      </li>
                      <li
                        v-if="entry.file_count != null && entry.file_count > entry.files.length"
                        class="text-[11px] italic text-muted-foreground"
                      >
                        … {{ entry.file_count - entry.files.length }} more
                      </li>
                    </ul>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </CardContent>
      </Card>

      <!-- ── Related docs ────────────────────────────────────────────────── -->
      <Card>
        <CardHeader class="pb-2">
          <CardTitle class="text-base">Related docs</CardTitle>
        </CardHeader>
        <CardContent class="space-y-3">
          <div v-if="!detail.related_docs.length" class="text-sm text-muted-foreground">
            No related docs — pages watching this area's paths are offered to its audit runs as likely-relevant context.
          </div>
          <template v-else>
            <p class="text-xs text-muted-foreground">
              Offered to this area's audit runs as likely-relevant context (not limited to these) — agents pull the ones they need.
            </p>
            <ul class="space-y-1">
              <li v-for="doc in detail.related_docs" :key="doc.uid">
                <RouterLink
                  v-if="repoSlug"
                  :to="{ name: 'documentation', params: { repoSlug }, query: { doc: doc.slug } }"
                  class="inline-flex items-center gap-1.5 text-sm text-primary hover:underline"
                >
                  <BookOpen class="h-3.5 w-3.5" />
                  {{ doc.title || doc.slug }}
                  <span class="font-mono text-xs text-muted-foreground">{{ doc.slug }}</span>
                </RouterLink>
                <span v-else class="inline-flex items-center gap-1.5 text-sm">
                  <BookOpen class="h-3.5 w-3.5" />
                  {{ doc.title || doc.slug }}
                  <span class="font-mono text-xs text-muted-foreground">{{ doc.slug }}</span>
                </span>
              </li>
            </ul>
          </template>
        </CardContent>
      </Card>

      <!-- ── Related areas ───────────────────────────────────────────────── -->
      <Card v-if="detail.related_areas.length">
        <CardHeader class="pb-2">
          <CardTitle class="text-base">Related areas</CardTitle>
        </CardHeader>
        <CardContent>
          <div class="flex flex-wrap gap-1.5">
            <RouterLink
              v-for="rel in detail.related_areas"
              :key="rel.uid"
              :to="{ name: 'area-detail', params: { uid: rel.uid } }"
              class="inline-flex items-center gap-1.5 rounded-full border border-border px-2.5 py-0.5 text-xs transition-colors hover:bg-accent"
              :title="rel.title"
            >
              <span class="font-mono">{{ rel.key }}</span>
              <Badge :variant="areaKindVariant(rel.kind)" class="px-1 text-[9px]">{{ rel.kind }}</Badge>
            </RouterLink>
          </div>
        </CardContent>
      </Card>

      <!-- ── Coverage ────────────────────────────────────────────────────── -->
      <Card>
        <CardHeader class="pb-2">
          <CardTitle class="text-base">Coverage</CardTitle>
        </CardHeader>
        <CardContent class="p-0">
          <p v-if="!detail.coverage.length" class="px-4 pb-4 text-sm text-muted-foreground">
            No campaign has covered this area yet.
          </p>
          <ul v-else class="divide-y divide-border">
            <li v-for="(stamp, i) in detail.coverage" :key="`${stamp.run_uid}-${i}`" class="space-y-1 px-4 py-2">
              <div class="flex flex-wrap items-center gap-2">
                <Badge :variant="outcomeVariant(stamp.outcome)" class="px-1.5 text-[10px]">{{ stamp.outcome }}</Badge>
                <span v-if="stamp.checked_at" class="text-xs text-muted-foreground" :title="stamp.checked_at">
                  {{ formatRelativeTime(stamp.checked_at) }}
                </span>
                <RouterLink
                  v-if="stamp.run_uid"
                  :to="{ name: 'run-detail', params: { uid: stamp.run_uid } }"
                  class="inline-flex items-center gap-1 text-xs underline-offset-2 hover:underline"
                >
                  <Activity class="h-3 w-3" />
                  <span class="font-mono">{{ stamp.run_uid.slice(0, 8) }}</span>
                </RouterLink>
              </div>
              <div v-if="stamp.lens_verdicts.length" class="flex flex-wrap gap-1.5">
                <Badge
                  v-for="lv in stamp.lens_verdicts"
                  :key="lv.lens"
                  :variant="verdictVariant(lv.verdict)"
                  class="px-1.5 font-mono text-[10px]"
                  :title="lv.note"
                >
                  {{ lv.lens }}: {{ lv.verdict }}
                </Badge>
              </div>
            </li>
          </ul>
        </CardContent>
      </Card>

      <!-- ── Pending edits ───────────────────────────────────────────────── -->
      <Card v-if="detail.pending_edits.length">
        <CardHeader class="flex-row items-center justify-between space-y-0">
          <CardTitle class="text-base">Pending edits</CardTitle>
          <span class="text-xs text-muted-foreground">
            {{ detail.pending_edits.length }} awaiting review
          </span>
        </CardHeader>
        <CardContent class="p-0">
          <AreaEditReviewCard
            v-for="edit in detail.pending_edits"
            :key="edit.uid"
            :edit="edit"
            :warnings="edit.warnings"
            :current-scope-paths="edit.area_uid ? area.scope_paths : undefined"
            :resolving="resolvingUid === edit.uid"
            :disabled="!!resolvingUid && resolvingUid !== edit.uid"
            @accept="acceptEdit(edit)"
            @reject="rejectEdit(edit)"
          />
        </CardContent>
      </Card>

      <!-- ── Danger row ──────────────────────────────────────────────────── -->
      <div class="flex items-center justify-end gap-2">
        <Button variant="outline" size="sm" @click="editOpen = true">
          <Pencil /> Edit
        </Button>
        <Button
          variant="outline"
          size="sm"
          class="text-destructive hover:text-destructive"
          @click="deleteOpen = true"
        >
          <Trash2 /> Delete
        </Button>
      </div>
    </template>

    <AreaEditDialog
      :open="editOpen"
      :area="area"
      @update:open="editOpen = $event"
      @saved="refresh"
    />

    <AlertDialog v-model:open="deleteOpen">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete area</AlertDialogTitle>
          <AlertDialogDescription>
            Delete area "{{ area?.title || area?.key }}"? Pending edits against it are rejected. This cannot be undone.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            class="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            @click="confirmDelete"
          >
            Delete
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </div>
</template>
