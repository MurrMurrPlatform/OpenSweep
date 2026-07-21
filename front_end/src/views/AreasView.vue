<script setup lang="ts">
import { computed, ref, onMounted, watch } from 'vue'
import { RouterLink } from 'vue-router'
import {
  Check,
  ChevronDown,
  ChevronRight,
  Map as MapIcon,
  Pencil,
  Trash2,
  X,
} from 'lucide-vue-next'
import { useAreaStore } from '@/stores/areaStore'
import { useCurrentRepo } from '@/composables/useCurrentRepo'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { MarkdownView } from '@/components/ui/markdown'
import { PageHeader } from '@/components/ui/page-header'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog'
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
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Switch } from '@/components/ui/switch'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import type { AreaDTO, AreaEditDTO, AreaKind } from '@/types/api'

const areaStore = useAreaStore()
const toast = useToast()
const { uid: repoUid } = useCurrentRepo()

const loading = ref(true)
const error = ref<string | null>(null)

// ── Load ─────────────────────────────────────────────────────────────────────

async function reload() {
  if (!repoUid.value) return
  loading.value = true
  error.value = null
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

// ── Hierarchy: keys are path-like; indent is derived from key depth ──────────

const sortedAreas = computed<AreaDTO[]>(() => [...areaStore.areas].sort((a, b) => a.key.localeCompare(b.key)))

function depth(key: string): number {
  return key.split('/').length - 1
}

const KIND_VARIANT: Record<AreaKind, 'secondary' | 'info' | 'outline'> = {
  subsystem: 'secondary',
  feature: 'info',
  ignore: 'outline',
}

function kindVariant(kind: string) {
  return KIND_VARIANT[kind as AreaKind] ?? 'secondary'
}

function staleTitle(a: AreaDTO): string {
  const count = `${a.stale_paths.length} path${a.stale_paths.length === 1 ? '' : 's'} changed since last review`
  const reviewed = a.last_reviewed_at ? `\nlast reviewed ${a.last_reviewed_at.slice(0, 10)}` : ''
  return count + reviewed
}

// Collapsible spec previews (areas + pending edits), keyed by uid.
const expandedSpecs = ref<Set<string>>(new Set())

function toggleSpec(uid: string) {
  const next = new Set(expandedSpecs.value)
  if (next.has(uid)) next.delete(uid)
  else next.add(uid)
  expandedSpecs.value = next
}

// ── Inline edit dialog ───────────────────────────────────────────────────────

const editOpen = ref(false)
const saving = ref(false)
const editingArea = ref<AreaDTO | null>(null)
const draftTitle = ref('')
const draftKind = ref<AreaKind>('subsystem')
const draftScopePaths = ref('')
const draftSpec = ref('')
const draftEnabled = ref(true)

function openEdit(a: AreaDTO) {
  editingArea.value = a
  draftTitle.value = a.title
  draftKind.value = a.kind
  draftScopePaths.value = a.scope_paths.join('\n')
  draftSpec.value = a.spec
  draftEnabled.value = a.enabled
  editOpen.value = true
}

async function saveEdit() {
  const target = editingArea.value
  if (!target || saving.value) return
  saving.value = true
  try {
    await areaStore.patchArea(target.uid, {
      title: draftTitle.value,
      kind: draftKind.value,
      scope_paths: draftScopePaths.value.split('\n').map((p) => p.trim()).filter(Boolean),
      spec: draftSpec.value,
      enabled: draftEnabled.value,
    })
    editOpen.value = false
    toast.success('Area saved', target.key)
  } catch (e: unknown) {
    toast.error('Save failed', e instanceof Error ? e.message : String(e))
  } finally {
    saving.value = false
  }
}

// ── Delete ───────────────────────────────────────────────────────────────────

const deleteOpen = ref(false)
const pendingDelete = ref<AreaDTO | null>(null)

function deleteArea(a: AreaDTO) {
  pendingDelete.value = a
  deleteOpen.value = true
}

async function confirmDelete() {
  const a = pendingDelete.value
  if (!a) return
  deleteOpen.value = false
  try {
    await areaStore.deleteArea(a.uid)
    toast.success('Area deleted', a.key)
  } catch (e: unknown) {
    toast.error('Delete failed', e instanceof Error ? e.message : String(e))
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
    if (repoUid.value) {
      await Promise.all([
        areaStore.fetchAreas(repoUid.value),
        areaStore.fetchEdits(repoUid.value, 'pending'),
      ])
    }
  } catch (e: unknown) {
    toast.error(`Bulk ${action} failed`, e instanceof Error ? e.message : String(e))
  } finally {
    bulkResolving.value = null
  }
}

function editHeading(edit: AreaEditDTO): string {
  return edit.title || edit.key
}
</script>

<template>
  <div class="space-y-4">
    <PageHeader
      title="Areas"
      subtitle="The reviewed audit partition: path-keyed areas that carry what to check where. Agents propose the map; every change lands here for review."
    >
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
          <div
            v-for="edit in areaStore.edits"
            :key="edit.uid"
            class="border-b border-border p-4 last:border-b-0 space-y-2"
          >
            <div class="flex flex-wrap items-center justify-between gap-2">
              <div class="min-w-0">
                <div class="flex flex-wrap items-center gap-1.5">
                  <span class="font-mono text-sm font-medium">{{ edit.key }}</span>
                  <Badge :variant="kindVariant(edit.kind)" class="px-1.5 text-[10px]">{{ edit.kind || 'subsystem' }}</Badge>
                  <Badge v-if="!edit.area_uid" variant="info" class="px-1.5 text-[10px]">new area</Badge>
                  <Badge v-else variant="warn" class="px-1.5 text-[10px]" title="Replaces the area's current spec">updates existing</Badge>
                </div>
                <div v-if="editHeading(edit) !== edit.key" class="text-sm">{{ editHeading(edit) }}</div>
                <div class="text-xs text-muted-foreground">
                  <span v-if="edit.source_run_uid">
                    run
                    <RouterLink
                      :to="{ name: 'run-detail', params: { uid: edit.source_run_uid } }"
                      class="font-mono text-primary hover:underline"
                    >{{ edit.source_run_uid.slice(0, 8) }}</RouterLink>
                  </span>
                  <span v-if="edit.created_at"> · {{ edit.created_at.slice(0, 10) }}</span>
                </div>
              </div>
              <div class="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  :disabled="!!resolvingUid || !!bulkResolving"
                  @click="rejectEdit(edit)"
                >
                  <X /> Reject
                </Button>
                <Button
                  size="sm"
                  :loading="resolvingUid === edit.uid"
                  :disabled="(!!resolvingUid && resolvingUid !== edit.uid) || !!bulkResolving"
                  @click="acceptEdit(edit)"
                >
                  <Check /> Accept
                </Button>
              </div>
            </div>

            <p v-if="edit.rationale" class="text-sm text-muted-foreground">{{ edit.rationale }}</p>

            <div v-if="edit.scope_paths.length" class="flex flex-wrap gap-1.5">
              <span
                v-for="path in edit.scope_paths"
                :key="path"
                class="rounded-full border border-border px-2.5 py-0.5 font-mono text-xs"
              >
                {{ path }}
              </span>
            </div>

            <!-- Collapsible spec preview -->
            <div v-if="edit.proposed_spec">
              <button
                type="button"
                class="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                @click="toggleSpec(edit.uid)"
              >
                <component :is="expandedSpecs.has(edit.uid) ? ChevronDown : ChevronRight" class="h-3.5 w-3.5" />
                {{ edit.current_spec ? 'Proposed spec (replaces the current one)' : 'Proposed spec' }}
              </button>
              <div v-if="expandedSpecs.has(edit.uid)" class="mt-2 space-y-2">
                <div class="rounded-md border border-border p-3">
                  <MarkdownView :model-value="edit.proposed_spec" preview-only />
                </div>
                <details v-if="edit.current_spec" class="rounded-md border border-border p-3">
                  <summary class="cursor-pointer text-xs text-muted-foreground">Current spec (being replaced)</summary>
                  <MarkdownView :model-value="edit.current_spec" preview-only class="mt-2" />
                </details>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- ── The area map ────────────────────────────────────────────────── -->
      <Card>
        <CardHeader class="flex-row items-center justify-between space-y-0">
          <CardTitle class="text-base">Area map</CardTitle>
          <span class="text-xs text-muted-foreground">{{ sortedAreas.length }} area{{ sortedAreas.length === 1 ? '' : 's' }}</span>
        </CardHeader>
        <CardContent class="p-0">
          <div v-if="!sortedAreas.length" class="p-4">
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
          </div>

          <ul v-else class="divide-y divide-border">
            <li v-for="a in sortedAreas" :key="a.uid">
              <div
                class="flex items-start gap-2 py-2 pr-3 transition-colors hover:bg-accent/50"
                :class="{ 'opacity-60': !a.enabled }"
                :style="{ paddingLeft: `${12 + depth(a.key) * 20}px` }"
              >
                <button
                  type="button"
                  class="mt-0.5 rounded-sm p-1 text-muted-foreground hover:text-foreground"
                  :title="a.spec ? 'Toggle spec preview' : 'No spec yet'"
                  :disabled="!a.spec"
                  @click="toggleSpec(a.uid)"
                >
                  <component :is="expandedSpecs.has(a.uid) ? ChevronDown : ChevronRight" class="h-3.5 w-3.5" />
                </button>
                <div class="min-w-0 flex-1">
                  <div class="flex flex-wrap items-center gap-1.5">
                    <span class="truncate text-sm font-medium">{{ a.title || a.key }}</span>
                    <Badge :variant="kindVariant(a.kind)" class="px-1.5 text-[10px]">{{ a.kind }}</Badge>
                    <Badge v-if="!a.enabled" variant="outline" class="px-1.5 text-[10px]">disabled</Badge>
                    <span
                      v-if="a.stale"
                      class="h-2 w-2 shrink-0 rounded-full bg-amber-500"
                      :title="staleTitle(a)"
                    />
                    <Badge v-if="a.pending_edits > 0" variant="warn" class="px-1.5 text-[10px]" title="Pending agent edits">
                      {{ a.pending_edits }}
                    </Badge>
                  </div>
                  <div class="truncate font-mono text-[10px] text-muted-foreground">{{ a.key }}</div>
                  <div v-if="a.scope_paths.length" class="mt-1 flex flex-wrap items-center gap-1.5">
                    <span
                      v-for="path in a.scope_paths.slice(0, 2)"
                      :key="path"
                      class="rounded-full border border-border px-2 py-0.5 font-mono text-[10px] text-muted-foreground"
                      :title="path"
                    >
                      {{ path }}
                    </span>
                    <span
                      v-if="a.scope_paths.length > 2"
                      class="text-[10px] text-muted-foreground"
                      :title="a.scope_paths.slice(2).join('\n')"
                    >
                      +{{ a.scope_paths.length - 2 }}
                    </span>
                  </div>
                  <div v-if="expandedSpecs.has(a.uid) && a.spec" class="mt-2 rounded-md border border-border p-3">
                    <MarkdownView :model-value="a.spec" preview-only />
                  </div>
                </div>
                <div class="flex shrink-0 gap-1">
                  <Button variant="ghost" size="icon-sm" title="Edit area" @click="openEdit(a)">
                    <Pencil />
                  </Button>
                  <Button variant="ghost" size="icon-sm" class="text-destructive" title="Delete area" @click="deleteArea(a)">
                    <Trash2 />
                  </Button>
                </div>
              </div>
            </li>
          </ul>
        </CardContent>
      </Card>
    </template>

    <!-- ── Edit dialog ─────────────────────────────────────────────────────── -->
    <Dialog :open="editOpen" @update:open="editOpen = $event">
      <DialogContent class="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Edit area</DialogTitle>
          <DialogDescription>
            <span class="font-mono">{{ editingArea?.key }}</span> — a human edit counts as a review and clears staleness.
          </DialogDescription>
        </DialogHeader>
        <div class="space-y-3">
          <div class="grid gap-3 md:grid-cols-2">
            <div class="space-y-1.5">
              <Label>Title</Label>
              <Input v-model="draftTitle" placeholder="Area title" />
            </div>
            <div class="space-y-1.5">
              <Label>Kind</Label>
              <Select :model-value="draftKind" @update:model-value="draftKind = $event as AreaKind">
                <SelectTrigger class="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="subsystem">subsystem — exclusive partition leaf</SelectItem>
                  <SelectItem value="feature">feature — cross-cutting spec overlay</SelectItem>
                  <SelectItem value="ignore">ignore — not auditable (spec says why)</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <div class="space-y-1.5">
            <Label>Scope paths (one per line)</Label>
            <Textarea
              v-model="draftScopePaths"
              :rows="3"
              placeholder="backend/delivery/&#10;backend/webhooks.py"
              class="font-mono text-xs"
            />
          </div>
          <div class="space-y-1.5">
            <Label>Spec (markdown — what to check here)</Label>
            <Textarea v-model="draftSpec" :rows="8" placeholder="What matters in this area…" class="font-mono text-xs" />
          </div>
          <div class="flex items-center gap-2">
            <Switch :model-value="draftEnabled" @update:model-value="draftEnabled = $event" />
            <Label>Enabled — disabled areas are ignored by planning</Label>
          </div>
        </div>
        <DialogFooter>
          <Button variant="ghost" size="sm" @click="editOpen = false">Cancel</Button>
          <Button size="sm" :loading="saving" @click="saveEdit">
            <Check /> Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <AlertDialog v-model:open="deleteOpen">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete area</AlertDialogTitle>
          <AlertDialogDescription>
            Delete area "{{ pendingDelete?.title || pendingDelete?.key }}"? Pending edits against it are rejected. This cannot be undone.
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
  </div>
</template>
