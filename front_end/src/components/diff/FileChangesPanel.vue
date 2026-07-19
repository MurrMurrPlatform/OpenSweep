<script setup lang="ts">
/**
 * Two-pane file-changes explorer shared by the run Files tab and the PR
 * Files panel. Left: the changed files (or the full workspace tree when the
 * source provides one) as a nested tree; right: a GitHub-style diff for the
 * selection. The data source is a caller-provided `fetch` returning the
 * `/runs/{uid}/changes` shape. Refetches whenever `refreshKey` bumps — only
 * the very first load shows a loading state, later refreshes swap data in
 * place. `resetKey` changes drop all state and reload from scratch.
 */
import { computed, onMounted, ref, watch } from 'vue'
import {
  ChevronRight,
  File,
  FileDiff,
  Folder,
  FolderOpen,
  RefreshCw,
} from 'lucide-vue-next'
import DiffView from '@/components/diff/DiffView.vue'
import { Skeleton } from '@/components/ui/skeleton'
import type { RunChangedFile, RunChangesDTO } from '@/types/api'

const props = defineProps<{
  fetch: () => Promise<RunChangesDTO>
  refreshKey?: number
  /** State (selection, expansion, data) resets when this changes. */
  resetKey?: string
  /** Shown when the source exists but reports zero changed files. */
  emptyMessage?: string
  /** Shown when the source reports source === 'none'. */
  noneMessage?: string
}>()

const emit = defineEmits<{ loaded: [count: number] }>()

const data = ref<RunChangesDTO | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)
const mode = ref<'changed' | 'all'>('changed')
const selectedPath = ref<string | null>(null)
const expanded = ref<Set<string>>(new Set())

// ── Fetching ─────────────────────────────────────────────────────────────────

async function fetchChanges() {
  if (!data.value) loading.value = true
  try {
    const res = await props.fetch()
    data.value = res
    error.value = null
    emit('loaded', res.files.length)
    if (!selectedPath.value && res.files.length) {
      selectedPath.value = res.files[0].path
    }
    if (mode.value === 'changed') expandAll()
  } catch (e: unknown) {
    // Keep the last good data on refresh errors; only surface on first load.
    if (!data.value) error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(fetchChanges)
watch(() => props.refreshKey, () => void fetchChanges())
watch(() => props.resetKey, () => {
  data.value = null
  selectedPath.value = null
  expanded.value = new Set()
  void fetchChanges()
})

// ── Tree building (pure helpers) ─────────────────────────────────────────────

interface TreeNode {
  name: string
  path: string
  children?: TreeNode[]
  /** Set on file leaves; carries the change when the file was touched. */
  file?: RunChangedFile | null
}

function buildTree(paths: string[], changed: Map<string, RunChangedFile>): TreeNode[] {
  const roots: TreeNode[] = []
  const dirIndex = new Map<string, TreeNode>()

  for (const fullPath of paths) {
    const parts = fullPath.split('/').filter(Boolean)
    let siblings = roots
    let prefix = ''
    for (let i = 0; i < parts.length; i++) {
      const name = parts[i]
      prefix = prefix ? `${prefix}/${name}` : name
      const isLeaf = i === parts.length - 1
      if (isLeaf) {
        siblings.push({ name, path: fullPath, file: changed.get(fullPath) ?? null })
      } else {
        let dir = dirIndex.get(prefix)
        if (!dir) {
          dir = { name, path: prefix, children: [] }
          dirIndex.set(prefix, dir)
          siblings.push(dir)
        }
        siblings = dir.children!
      }
    }
  }

  const sortLevel = (nodes: TreeNode[]) => {
    nodes.sort((a, b) => {
      const aDir = a.children ? 0 : 1
      const bDir = b.children ? 0 : 1
      return aDir !== bDir ? aDir - bDir : a.name.localeCompare(b.name)
    })
    for (const n of nodes) if (n.children) sortLevel(n.children)
  }
  sortLevel(roots)
  return roots
}

function collectDirPaths(nodes: TreeNode[], into: string[] = []): string[] {
  for (const n of nodes) {
    if (n.children) {
      into.push(n.path)
      collectDirPaths(n.children, into)
    }
  }
  return into
}

const changedByPath = computed(() => {
  const map = new Map<string, RunChangedFile>()
  for (const f of data.value?.files ?? []) map.set(f.path, f)
  return map
})

const changedTree = computed(() =>
  buildTree((data.value?.files ?? []).map((f) => f.path), changedByPath.value),
)
const allTree = computed(() => buildTree(data.value?.tree ?? [], changedByPath.value))
const tree = computed(() => (mode.value === 'changed' ? changedTree.value : allTree.value))

/** Flatten the visible portion of the tree into rows for a flat v-for. */
const visibleRows = computed(() => {
  const rows: Array<{ node: TreeNode; depth: number }> = []
  const walk = (nodes: TreeNode[], depth: number) => {
    for (const n of nodes) {
      rows.push({ node: n, depth })
      if (n.children && expanded.value.has(n.path)) walk(n.children, depth + 1)
    }
  }
  walk(tree.value, 0)
  return rows
})

function expandAll() {
  expanded.value = new Set(collectDirPaths(changedTree.value))
}

function toggleDir(path: string) {
  const next = new Set(expanded.value)
  if (next.has(path)) next.delete(path)
  else next.add(path)
  expanded.value = next
}

function setMode(next: 'changed' | 'all') {
  if (mode.value === next) return
  mode.value = next
  // Changed mode: everything open. All-files mode: everything collapsed.
  if (next === 'changed') expandAll()
  else expanded.value = new Set()
}

// ── Selection / summary ──────────────────────────────────────────────────────

const selectedFile = computed<RunChangedFile | null>(() =>
  selectedPath.value ? changedByPath.value.get(selectedPath.value) ?? null : null,
)

const totalAdditions = computed(() =>
  (data.value?.files ?? []).reduce((sum, f) => sum + f.additions, 0),
)
const totalDeletions = computed(() =>
  (data.value?.files ?? []).reduce((sum, f) => sum + f.deletions, 0),
)

const STATUS_CHIP: Record<RunChangedFile['status'], { letter: string; cls: string }> = {
  added: { letter: 'A', cls: 'bg-good/15 text-good' },
  modified: { letter: 'M', cls: 'bg-warn/15 text-warn' },
  deleted: { letter: 'D', cls: 'bg-bad/15 text-bad' },
  renamed: { letter: 'R', cls: 'bg-primary/15 text-primary' },
}

function relativeTime(iso: string): string {
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return iso
  const deltaS = Math.round((Date.now() - t) / 1000)
  if (deltaS < 60) return 'just now'
  if (deltaS < 3600) return `${Math.round(deltaS / 60)}m ago`
  if (deltaS < 86400) return `${Math.round(deltaS / 3600)}h ago`
  return `${Math.round(deltaS / 86400)}d ago`
}
</script>

<template>
  <div class="rounded-md border bg-card overflow-hidden">
    <!-- First load -->
    <div v-if="loading && !data" class="p-4 space-y-3">
      <Skeleton class="h-6 w-1/3" />
      <Skeleton class="h-64" />
    </div>

    <div v-else-if="error && !data" class="p-4 text-sm text-bad">
      Couldn't load changes: {{ error }}
      <button class="ml-2 text-primary hover:underline" @click="fetchChanges">Retry</button>
    </div>

    <template v-else-if="data">
      <!-- Summary strip -->
      <div class="flex flex-wrap items-center gap-3 border-b bg-muted/60 px-4 py-2 text-xs">
        <span class="font-medium text-foreground">
          {{ data.files.length }} file{{ data.files.length === 1 ? '' : 's' }} changed
        </span>
        <span v-if="totalAdditions" class="text-good">+{{ totalAdditions }}</span>
        <span v-if="totalDeletions" class="text-bad">−{{ totalDeletions }}</span>
        <span
          v-if="data.base"
          class="rounded-full border bg-muted px-2 py-0.5 font-mono text-[10px] text-muted-foreground"
          title="Diff base"
        >
          {{ data.base }}
        </span>
        <button
          type="button"
          class="ml-auto grid h-7 w-7 place-items-center rounded-sm border text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
          title="Refresh changes"
          @click="fetchChanges"
        >
          <RefreshCw class="h-3.5 w-3.5" />
        </button>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-[260px_1fr]">
        <!-- LEFT: file explorer -->
        <div class="min-w-0 border-b md:border-b-0 md:border-r">
          <!-- The All-files toggle needs a workspace tree — a PR diff has none. -->
          <div v-if="data.tree.length" class="flex items-center gap-1 border-b p-2">
            <button
              type="button"
              :class="[
                'rounded-sm px-2 py-1 text-[11px] font-medium transition-colors',
                mode === 'changed'
                  ? 'bg-accent text-foreground'
                  : 'text-muted-foreground hover:text-foreground',
              ]"
              @click="setMode('changed')"
            >
              Changed ({{ data.files.length }})
            </button>
            <button
              type="button"
              :class="[
                'rounded-sm px-2 py-1 text-[11px] font-medium transition-colors',
                mode === 'all' ? 'bg-accent text-foreground' : 'text-muted-foreground hover:text-foreground',
              ]"
              @click="setMode('all')"
            >
              All files
            </button>
          </div>

          <div
            v-if="data.source === 'snapshot' && data.captured_at"
            class="border-b px-3 py-1.5 text-[10px] text-muted-foreground"
          >
            snapshot · {{ relativeTime(data.captured_at) }}
          </div>

          <div class="max-h-[560px] overflow-auto py-1">
            <div v-if="data.source === 'none'" class="px-3 py-6 text-xs text-muted-foreground">
              {{ noneMessage || 'No workspace and no snapshot for this run yet.' }}
            </div>
            <div v-else-if="!visibleRows.length" class="px-3 py-6 text-xs text-muted-foreground">
              {{
                mode === 'changed'
                  ? emptyMessage || 'No files changed in this run yet.'
                  : 'No files in the workspace tree.'
              }}
            </div>

            <template v-else>
              <template v-for="row in visibleRows" :key="`${mode}:${row.node.path}`">
                <!-- Directory row -->
                <button
                  v-if="row.node.children"
                  type="button"
                  class="flex w-full items-center gap-1.5 px-2 py-1 text-left text-xs text-muted-foreground hover:bg-accent transition-colors"
                  :style="{ paddingLeft: `${8 + row.depth * 14}px` }"
                  @click="toggleDir(row.node.path)"
                >
                  <ChevronRight
                    class="h-3 w-3 shrink-0 text-muted-foreground transition-transform"
                    :class="expanded.has(row.node.path) ? 'rotate-90' : ''"
                  />
                  <FolderOpen v-if="expanded.has(row.node.path)" class="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  <Folder v-else class="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  <span class="truncate">{{ row.node.name }}</span>
                </button>

                <!-- File row -->
                <button
                  v-else
                  type="button"
                  class="flex w-full items-center gap-1.5 px-2 py-1 text-left text-xs transition-colors"
                  :class="
                    selectedPath === row.node.path
                      ? 'bg-primary/10 text-foreground'
                      : 'text-muted-foreground hover:bg-accent'
                  "
                  :style="{ paddingLeft: `${8 + row.depth * 14 + 14}px` }"
                  :title="row.node.path"
                  @click="selectedPath = row.node.path"
                >
                  <span
                    v-if="row.node.file"
                    class="grid h-4 w-4 shrink-0 place-items-center rounded-sm text-[9px] font-bold"
                    :class="STATUS_CHIP[row.node.file.status].cls"
                  >
                    {{ STATUS_CHIP[row.node.file.status].letter }}
                  </span>
                  <File v-else class="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  <span
                    class="min-w-0 truncate"
                    :class="row.node.file?.status === 'deleted' ? 'line-through opacity-70' : ''"
                  >
                    {{ row.node.name }}
                  </span>
                  <span
                    v-if="row.node.file && (row.node.file.additions || row.node.file.deletions)"
                    class="ml-auto shrink-0 whitespace-nowrap text-[9px] tabular-nums"
                  >
                    <span v-if="row.node.file.additions" class="text-good">+{{ row.node.file.additions }}</span>
                    <span v-if="row.node.file.deletions" class="ml-1 text-bad">−{{ row.node.file.deletions }}</span>
                  </span>
                </button>
              </template>
            </template>
          </div>
        </div>

        <!-- RIGHT: detail pane -->
        <div class="min-w-0 p-3">
          <!-- Selected changed file -->
          <template v-if="selectedFile">
            <div
              v-if="selectedFile.binary"
              class="grid h-40 place-items-center rounded-md border border-dashed text-xs text-muted-foreground"
            >
              Binary file — no diff to display.
            </div>
            <div
              v-else-if="selectedFile.too_large"
              class="grid h-40 place-items-center rounded-md border border-dashed text-xs text-muted-foreground"
            >
              Diff too large to display.
            </div>
            <div
              v-else-if="selectedFile.status === 'deleted' && !selectedFile.patch"
              class="grid h-40 place-items-center rounded-md border border-dashed text-xs text-muted-foreground"
            >
              File deleted — the previous contents aren't available.
            </div>
            <div
              v-else-if="selectedFile.status === 'renamed' && !selectedFile.patch"
              class="grid h-40 place-items-center rounded-md border border-dashed text-xs text-muted-foreground"
            >
              Renamed from {{ selectedFile.old_path || 'another path' }} — contents unchanged.
            </div>
            <DiffView
              v-else
              :patch="selectedFile.patch"
              :file="selectedFile.path"
              :status="selectedFile.status"
              max-height="560px"
            />
          </template>

          <!-- Selected unchanged file (all-files mode) -->
          <div
            v-else-if="selectedPath"
            class="grid h-40 place-items-center rounded-md border border-dashed text-xs text-muted-foreground"
          >
            No changes in this file during this run.
          </div>

          <!-- Nothing selected → summary -->
          <div v-else class="grid h-40 place-items-center text-center">
            <div class="space-y-1.5">
              <FileDiff class="mx-auto h-6 w-6 text-muted-foreground" />
              <div class="text-sm text-muted-foreground">
                {{ data.files.length }} file{{ data.files.length === 1 ? '' : 's' }} changed
                <template v-if="totalAdditions || totalDeletions">
                  · <span class="text-good">+{{ totalAdditions }}</span>
                  <span class="text-bad"> −{{ totalDeletions }}</span>
                </template>
              </div>
              <div v-if="data.base" class="font-mono text-[10px] text-muted-foreground">base {{ data.base }}</div>
              <div class="text-xs text-muted-foreground">Select a file to see its diff.</div>
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
