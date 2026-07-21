<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import {
  AlertTriangle,
  BookOpen,
  Brain,
  Check,
  ChevronDown,
  ChevronRight,
  FolderOpen,
  GitPullRequest,
  Pencil,
  Pin,
  PinOff,
  Plus,
  Search,
  ShieldCheck,
  Sparkles,
  Trash2,
  Wand2,
  X,
} from 'lucide-vue-next'
import { useAgentStore } from '@/stores/agentStore'
import { useAreaStore } from '@/stores/areaStore'
import { useDocStore } from '@/stores/docStore'
import type { AgentDTO } from '@/types/api'
import { useMemoryStore } from '@/stores/memoryStore'
import { useCurrentRepo } from '@/composables/useCurrentRepo'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { collapseContext, lineDiff } from '@/lib/lineDiff'
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
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import type { DocDTO, DocEditDTO, MemoryDTO } from '@/types/api'

const docs = useDocStore()
const memories = useMemoryStore()
const agents = useAgentStore()
const areaStore = useAreaStore()
const route = useRoute()
const router = useRouter()
const toast = useToast()
const { uid: repoUid, slug: repoSlug } = useCurrentRepo()

const loading = ref(true)
const error = ref<string | null>(null)

const selectedUid = ref('')

// ── Load ─────────────────────────────────────────────────────────────────────

async function reload() {
  if (!repoUid.value) return
  loading.value = true
  error.value = null
  // Docs gate: the doc tree is generated one page per area, so generation
  // needs the area map first. Best-effort — an areas fetch failure must
  // never gate (or break) the docs page.
  void areaStore
    .fetchAreas(repoUid.value)
    .then(() => (areasKnown.value = true))
    .catch(() => (areasKnown.value = false))
  try {
    await Promise.all([
      docs.fetchAll({ repository_uid: repoUid.value }),
      docs.fetchEdits({ repository_uid: repoUid.value, status: 'pending' }),
      loadMemories(),
    ])
    const requestedDoc = String(route.query.doc || '')
    const bySlug = docs.list.find((d) => d.slug === requestedDoc)
    if (bySlug) selectedUid.value = bySlug.uid
    else if (!docs.list.some((d) => d.uid === selectedUid.value)) {
      selectedUid.value = pages.value[0]?.uid || ''
    }
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(reload)
watch(repoUid, reload)

// ── Docs gate: no enabled areas → generation has nothing to hang pages on ────

/** true once the areas fetch landed — an unknown map never gates. */
const areasKnown = ref(false)
const docsGated = computed(() => areasKnown.value && !areaStore.areas.some((a) => a.enabled))
const GATE_TOOLTIP = 'Map areas first — the doc tree is generated one page per area'

// ── Pages: a folder tree derived from "/"-segmented slugs ───────────────────

const pages = computed<DocDTO[]>(() =>
  [...docs.list].sort((a, b) => {
    if (a.pinned !== b.pinned) return a.pinned ? -1 : 1
    return a.slug.localeCompare(b.slug)
  }),
)

/** Root pages (no "/" in the slug) come first; folders group by first segment. */
const rootPages = computed<DocDTO[]>(() => pages.value.filter((d) => !d.slug.includes('/')))

interface DocFolder {
  name: string
  pages: DocDTO[]
}

const folders = computed<DocFolder[]>(() => {
  const byFolder = new Map<string, DocDTO[]>()
  for (const doc of pages.value) {
    if (!doc.slug.includes('/')) continue
    const folder = doc.slug.split('/')[0]
    const list = byFolder.get(folder) || []
    list.push(doc)
    byFolder.set(folder, list)
  }
  return [...byFolder.entries()]
    .map(([name, docsInFolder]) => ({
      name,
      pages: docsInFolder.sort((a, b) => a.slug.localeCompare(b.slug)),
    }))
    .sort((a, b) => a.name.localeCompare(b.name))
})

// Expanded by default; the set tracks folders the user collapsed.
const collapsedFolders = ref<Set<string>>(new Set())

function toggleFolder(name: string) {
  const next = new Set(collapsedFolders.value)
  if (next.has(name)) next.delete(name)
  else next.add(name)
  collapsedFolders.value = next
}

const selected = computed<DocDTO | null>(() => docs.list.find((d) => d.uid === selectedUid.value) || null)

const staleDocs = computed(() => docs.list.filter((d) => d.stale))

/** Rough token estimate for the combined pinned pages agents see every run. */
const pinnedTokenEstimate = computed(() => {
  const chars = docs.list.filter((d) => d.pinned).reduce((sum, d) => sum + d.body.length, 0)
  return Math.round(chars / 4)
})

/** Page label without the folder prefix, for rows nested under a folder header. */
function leafSlug(doc: DocDTO): string {
  const i = doc.slug.indexOf('/')
  return i >= 0 ? doc.slug.slice(i + 1) : doc.slug
}

function selectPage(doc: DocDTO) {
  selectedUid.value = doc.uid
  cancelEdit()
  router.replace({ query: { ...route.query, doc: doc.slug } })
}

async function togglePin(doc: DocDTO) {
  try {
    await docs.setPinned(doc.uid, !doc.pinned)
  } catch (e: unknown) {
    toast.error('Pin failed', e instanceof Error ? e.message : String(e))
  }
}

// ── Reset (destructive: wipe the whole doc tree) ─────────────────────────────
const resetDocsOpen = ref(false)
const resettingDocs = ref(false)

async function confirmResetDocs() {
  if (!repoUid.value) return
  resetDocsOpen.value = false
  resettingDocs.value = true
  try {
    const result = await docs.resetAll(repoUid.value)
    toast.success(
      'Documentation deleted',
      `${result.docs_deleted} page${result.docs_deleted === 1 ? '' : 's'} and ${result.edits_deleted} edit${result.edits_deleted === 1 ? '' : 's'} removed.`,
    )
    await reload()
  } catch (e: unknown) {
    toast.error('Reset failed', e instanceof Error ? e.message : String(e))
  } finally {
    resettingDocs.value = false
  }
}

const deletePageOpen = ref(false)
const pendingDeletePage = ref<DocDTO | null>(null)

function deleteSelected() {
  if (!selected.value) return
  pendingDeletePage.value = selected.value
  deletePageOpen.value = true
}

async function confirmDeleteSelected() {
  const page = pendingDeletePage.value
  if (!page) return
  deletePageOpen.value = false
  try {
    await docs.remove(page.uid)
    selectedUid.value = pages.value[0]?.uid || ''
    toast.success('Page deleted')
  } catch (e: unknown) {
    toast.error('Delete failed', e instanceof Error ? e.message : String(e))
  }
}

// ── Generate docs / Sync to repo ─────────────────────────────────────────────

const generating = ref(false)

async function generateDocs() {
  if (!repoUid.value || generating.value || docsGated.value) return
  generating.value = true
  try {
    const result = await docs.generate(repoUid.value)
    toast.success(
      'Generate docs dispatched',
      result.summary || (result.run_uid ? `run ${result.run_uid.slice(0, 8)}` : undefined),
      result.run_uid ? { label: 'View run', to: { name: 'run-detail', params: { uid: result.run_uid } } } : undefined,
    )
  } catch (e: unknown) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Generate failed', msg)
  } finally {
    generating.value = false
  }
}

const syncing = ref(false)
const lastExport = ref<{ pr_url: string; pr_number: number; pages: number } | null>(null)

async function syncToRepo() {
  if (!repoUid.value || syncing.value) return
  syncing.value = true
  try {
    const result = await docs.exportToRepo(repoUid.value)
    lastExport.value = { pr_url: result.pr_url, pr_number: result.pr_number, pages: result.pages }
    toast.success('Docs synced to repo', result.pr_url ? `PR #${result.pr_number}` : result.status)
  } catch (e: unknown) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Sync failed', msg)
  } finally {
    syncing.value = false
  }
}

// ── Draft / Verify (per-page LLM runs) ───────────────────────────────────────

const drafting = ref(false)
const verifying = ref(false)
const lastDocRun = ref<{ kind: 'draft' | 'verify'; runUid: string } | null>(null)

async function draftSelected() {
  if (!selected.value || drafting.value) return
  drafting.value = true
  try {
    const { run_uid } = await docs.draft(selected.value.uid)
    lastDocRun.value = { kind: 'draft', runUid: run_uid }
    toast.success('Draft run dispatched', `run ${run_uid.slice(0, 8)}`)
  } catch (e: unknown) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Draft failed', msg)
  } finally {
    drafting.value = false
  }
}

async function verifySelected() {
  if (!selected.value || verifying.value) return
  verifying.value = true
  try {
    const { run_uid } = await docs.verify(selected.value.uid)
    lastDocRun.value = { kind: 'verify', runUid: run_uid }
    toast.success('Verify run dispatched', `run ${run_uid.slice(0, 8)}`)
  } catch (e: unknown) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Verify failed', msg)
  } finally {
    verifying.value = false
  }
}

// ── New page ─────────────────────────────────────────────────────────────────

const createOpen = ref(false)
const creating = ref(false)
const newSlug = ref('')
const newTitle = ref('')
const newSummary = ref('')
const newWatchPaths = ref('')

function openCreate() {
  newSlug.value = ''
  newTitle.value = ''
  newSummary.value = ''
  newWatchPaths.value = ''
  createOpen.value = true
}

function kebab(s: string): string {
  return s.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
}

/** Path-like slug: kebab each "/" segment, keep the segments. */
function pathSlug(s: string): string {
  return s
    .split('/')
    .map(kebab)
    .filter(Boolean)
    .join('/')
}

function parsePathList(raw: string): string[] {
  return raw
    .split(/[\n,]+/)
    .map((p) => p.trim())
    .filter(Boolean)
}

// Auto-derive the slug from the title until the user edits the slug manually.
watch(newTitle, (t, prev) => {
  if (!newSlug.value || newSlug.value === kebab(prev || '')) newSlug.value = kebab(t)
})

async function createPage() {
  if (!repoUid.value || !newSlug.value.trim() || creating.value) return
  creating.value = true
  try {
    const doc = await docs.create({
      repository_uid: repoUid.value,
      slug: pathSlug(newSlug.value),
      title: newTitle.value.trim() || newSlug.value,
      summary: newSummary.value.trim(),
      watch_paths: parsePathList(newWatchPaths.value),
    })
    createOpen.value = false
    selectPage(doc)
    startEdit()
    toast.success('Page created', doc.slug)
  } catch (e: unknown) {
    toast.error('Create failed', e instanceof Error ? e.message : String(e))
  } finally {
    creating.value = false
  }
}

// ── Edit-in-place ────────────────────────────────────────────────────────────

const editing = ref(false)
const saving = ref(false)
const draftTitle = ref('')
const draftSummary = ref('')
const draftBody = ref('')
const draftWatchPaths = ref('')

function startEdit() {
  if (!selected.value) return
  draftTitle.value = selected.value.title
  draftSummary.value = selected.value.summary
  draftBody.value = selected.value.body
  draftWatchPaths.value = selected.value.watch_paths.join('\n')
  editing.value = true
}

function cancelEdit() {
  editing.value = false
}

async function saveEdit() {
  if (!selected.value || saving.value) return
  saving.value = true
  try {
    await docs.update(selected.value.uid, {
      title: draftTitle.value,
      summary: draftSummary.value,
      body: draftBody.value,
      watch_paths: parsePathList(draftWatchPaths.value),
    })
    editing.value = false
    toast.success('Page saved')
  } catch (e: unknown) {
    toast.error('Save failed', e instanceof Error ? e.message : String(e))
  } finally {
    saving.value = false
  }
}

// ── "Update docs" — dispatch a document run (KNOWLEDGE_V3_DOCUMENTATION §7) ──
// The document playbook compares Docs + Memories against the code and files
// DocEdits (including NEW pages) that land in the pending-edits queue below.

const updateDocsOpen = ref(false)
const dispatchingDocs = ref(false)
const docPrompts = ref<AgentDTO[]>([])
const selectedDocPromptUid = ref('')

// reka Select disallows empty-string item values; use a sentinel for the
// "default playbook" option and translate to '' in the underlying model.
const DEFAULT_PROMPT = '__default__'
const docPromptSelection = computed({
  get: () => selectedDocPromptUid.value || DEFAULT_PROMPT,
  set: (v: string) => {
    selectedDocPromptUid.value = v === DEFAULT_PROMPT ? '' : v
  },
})

async function openUpdateDocs() {
  updateDocsOpen.value = true
  selectedDocPromptUid.value = ''
  try {
    const all = await agents.fetchAll({ enabled_only: true })
    // Alternative strategies only. System agents (the document base and
    // stage guidance) are what the default option already runs — listing
    // them here showed the same run under three different names.
    docPrompts.value = all.filter(
      (a) => a.produces === 'documentation' && a.provenance !== 'system',
    )
  } catch {
    docPrompts.value = []
  }
}

async function documentAgentUid(): Promise<string> {
  const all = await agents.fetchAll({ provenance: 'system', produces: 'documentation' })
  const doc = all.find((a) => a.key === 'document')
  if (!doc) throw new Error('document system agent not found')
  return doc.uid
}

async function dispatchDocumentRun() {
  if (!repoUid.value || dispatchingDocs.value) return
  dispatchingDocs.value = true
  try {
    const picked = docPrompts.value.find((a) => a.uid === selectedDocPromptUid.value) || null
    // The system document agent supplies the canned instructions when no
    // library agent is picked; a picked agent overrides it for
    // repo-specific documentation policy.
    const agentUid = picked?.uid || (await documentAgentUid())
    const run = await agents.dispatch(agentUid, { repository_uid: repoUid.value })
    updateDocsOpen.value = false
    toast.success('Document run dispatched', `run ${run.uid.slice(0, 8)}`)
    router.push({ name: 'run-detail', params: { uid: run.uid } })
  } catch (e: unknown) {
    toast.error('Dispatch failed', e instanceof Error ? e.message : String(e))
  } finally {
    dispatchingDocs.value = false
  }
}

// ── Pending edits ────────────────────────────────────────────────────────────

const selectedEdits = computed<DocEditDTO[]>(() =>
  docs.edits.filter((e) => selected.value && e.doc_uid === selected.value.uid),
)

/** Everything not shown inline on the selected page — other pages + new-page proposals. */
const otherEdits = computed<DocEditDTO[]>(() =>
  docs.edits.filter((e) => !selected.value || e.doc_uid !== selected.value.uid),
)

function editTarget(edit: DocEditDTO): string {
  if (!edit.doc_uid) return `new page · ${edit.slug || edit.title || 'untitled'}`
  const doc = docs.list.find((d) => d.uid === edit.doc_uid)
  return doc ? doc.slug : edit.slug || edit.doc_uid.slice(0, 8)
}

function diffFor(edit: DocEditDTO) {
  return collapseContext(lineDiff(edit.current_body || '', edit.proposed_body || ''))
}

const resolvingUid = ref('')

async function acceptEdit(edit: DocEditDTO) {
  if (resolvingUid.value) return
  resolvingUid.value = edit.uid
  try {
    const doc = await docs.acceptEdit(edit.uid)
    if (!selectedUid.value) selectedUid.value = doc.uid
    toast.success('Edit accepted', doc.slug)
  } catch (e: unknown) {
    toast.error('Accept failed', e instanceof Error ? e.message : String(e))
  } finally {
    resolvingUid.value = ''
  }
}

async function rejectEdit(edit: DocEditDTO) {
  if (resolvingUid.value) return
  resolvingUid.value = edit.uid
  try {
    await docs.rejectEdit(edit.uid)
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
const pendingBulkUids = ref<string[]>([])

function resolveAllOther(action: 'accept' | 'reject') {
  const uids = otherEdits.value.map((e) => e.uid)
  if (!uids.length || bulkResolving.value) return
  pendingBulkAction.value = action
  pendingBulkUids.value = uids
  bulkResolveOpen.value = true
}

async function confirmResolveAllOther() {
  const action = pendingBulkAction.value
  const uids = pendingBulkUids.value
  if (!action || !uids.length) return
  bulkResolveOpen.value = false
  bulkResolving.value = action
  try {
    const result = action === 'accept' ? await docs.bulkAccept(uids) : await docs.bulkReject(uids)
    if (result.errors?.length) {
      toast.warn(`Some edits failed to ${action}`, result.errors.join('; '))
    } else {
      toast.success(action === 'accept' ? `Accepted ${uids.length} edits` : `Rejected ${uids.length} edits`)
    }
    if (repoUid.value) {
      await Promise.all([
        docs.fetchAll({ repository_uid: repoUid.value }),
        docs.fetchEdits({ repository_uid: repoUid.value, status: 'pending' }),
      ])
    }
  } catch (e: unknown) {
    toast.error(`Bulk ${action} failed`, e instanceof Error ? e.message : String(e))
  } finally {
    bulkResolving.value = null
  }
}

// ── Memories ─────────────────────────────────────────────────────────────────

const memoryQuery = ref('')
const memoriesLoading = ref(false)
let memorySearchTimer: number | undefined

async function loadMemories() {
  if (!repoUid.value) return
  memoriesLoading.value = true
  try {
    await memories.fetchAll({
      repository_uid: repoUid.value,
      q: memoryQuery.value.trim() || undefined,
    })
  } finally {
    memoriesLoading.value = false
  }
}

watch(memoryQuery, () => {
  if (memorySearchTimer) window.clearTimeout(memorySearchTimer)
  memorySearchTimer = window.setTimeout(loadMemories, 300)
})

onBeforeUnmount(() => {
  if (memorySearchTimer) window.clearTimeout(memorySearchTimer)
})

const deleteMemoryOpen = ref(false)
const pendingDeleteMemory = ref<MemoryDTO | null>(null)

function deleteMemory(m: MemoryDTO) {
  pendingDeleteMemory.value = m
  deleteMemoryOpen.value = true
}

async function confirmDeleteMemory() {
  const m = pendingDeleteMemory.value
  if (!m) return
  deleteMemoryOpen.value = false
  try {
    await memories.remove(m.uid)
  } catch (e: unknown) {
    toast.error('Delete failed', e instanceof Error ? e.message : String(e))
  }
}
</script>

<template>
  <div class="space-y-4">
    <PageHeader
      title="Documentation"
      subtitle="The repository's wiki: path-organized pages that watch the code they describe. Pinned pages ride along in every run's prompt; agents propose edits that land here as diffs."
    >
      <Button
        v-if="docs.list.length || docs.edits.length"
        variant="outline"
        size="sm"
        class="text-destructive hover:text-destructive"
        :loading="resettingDocs"
        title="Delete every doc page and pending edit for this repository."
        @click="resetDocsOpen = true"
      >
        Reset docs…
      </Button>
      <span :title="docsGated ? GATE_TOOLTIP : 'Dispatch one LLM run that proposes doc pages for this repository. Proposals land below as pending edits.'">
        <Button variant="outline" size="sm" :loading="generating" :disabled="docsGated" @click="generateDocs">
          <Wand2 v-if="!generating" />
          Generate docs
        </Button>
      </span>
      <Button variant="outline" size="sm" :loading="syncing" @click="syncToRepo" title="Sync docs to the repo as an AGENTS.md + docs/** pull request.">
        <GitPullRequest v-if="!syncing" />
        Sync to repo
      </Button>
      <Button size="sm" @click="openUpdateDocs">
        <Sparkles />
        Update docs
      </Button>
    </PageHeader>

    <template v-if="loading">
      <div class="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
        <Skeleton class="h-64" />
        <Skeleton class="h-64" />
      </div>
    </template>

    <ErrorState v-else-if="error" title="Couldn't load documentation" :message="error">
      <Button variant="outline" size="sm" @click="reload">Retry</Button>
    </ErrorState>

    <template v-else>
      <!-- Docs gate: generation hangs pages on the area map — map first. -->
      <div
        v-if="docsGated"
        class="flex items-center gap-2 rounded-sm border border-border bg-muted px-3 py-2 text-sm text-muted-foreground"
      >
        <BookOpen class="h-4 w-4 shrink-0" />
        <span>
          The doc tree is generated one page per area —
          <RouterLink
            v-if="repoSlug"
            :to="{ name: 'areas', params: { repoSlug } }"
            class="text-primary hover:underline"
          >map the areas first</RouterLink>
          <template v-else>map the areas first</template>.
        </span>
      </div>

      <!-- Stale banner: pages whose watched code moved on without them. -->
      <div
        v-if="staleDocs.length"
        class="flex items-center gap-2 rounded-sm border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-800 dark:text-amber-300"
      >
        <AlertTriangle class="h-4 w-4 shrink-0" />
        {{ staleDocs.length }} page{{ staleDocs.length === 1 ? '' : 's' }} behind the code
      </div>

      <!-- Last export result: link to the PR that carries the docs. -->
      <div
        v-if="lastExport?.pr_url"
        class="flex items-center gap-2 rounded-md border border-border bg-muted px-3 py-2 text-sm"
      >
        <GitPullRequest class="h-4 w-4 shrink-0 text-muted-foreground" />
        <span>Synced {{ lastExport.pages }} page{{ lastExport.pages === 1 ? '' : 's' }} —</span>
        <a :href="lastExport.pr_url" target="_blank" rel="noopener noreferrer" class="text-primary hover:underline">
          PR #{{ lastExport.pr_number }} →
        </a>
      </div>

      <section class="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)] items-start">
        <!-- ── Table of contents: the page tree ───────────────────────────── -->
        <Card>
          <CardHeader class="flex-row items-center justify-between space-y-0">
            <CardTitle class="text-base">Pages</CardTitle>
            <Button variant="outline" size="sm" @click="openCreate">
              <Plus /> New page
            </Button>
          </CardHeader>
          <CardContent class="p-0">
            <div class="px-4 py-2 text-xs text-muted-foreground border-b border-border">
              Pinned pages ≈ {{ pinnedTokenEstimate.toLocaleString() }} tokens per run
            </div>

            <template v-if="pages.length">
              <!-- Root pages first -->
              <ul class="divide-y divide-border">
                <li v-for="doc in rootPages" :key="doc.uid">
                  <div
                    :class="[
                      'flex items-center gap-2 px-3 py-2 cursor-pointer transition-colors',
                      selectedUid === doc.uid ? 'bg-primary/10' : 'hover:bg-accent',
                    ]"
                    @click="selectPage(doc)"
                  >
                    <button
                      type="button"
                      :title="doc.pinned ? 'Unpin — stop injecting into every run' : 'Pin — inject verbatim into every run'"
                      :class="['rounded-sm p-1', doc.pinned ? 'text-amber-600' : 'text-muted-foreground hover:text-foreground']"
                      @click.stop="togglePin(doc)"
                    >
                      <component :is="doc.pinned ? Pin : PinOff" class="h-3.5 w-3.5" />
                    </button>
                    <div class="min-w-0 flex-1">
                      <div class="flex items-center gap-1.5">
                        <span class="truncate text-sm font-medium">{{ doc.title || doc.slug }}</span>
                        <span
                          v-if="doc.stale"
                          class="h-2 w-2 shrink-0 rounded-full bg-amber-500"
                          :title="`Code changed since last review:\n${doc.stale_paths.join('\n')}`"
                        />
                      </div>
                      <div class="truncate font-mono text-[10px] text-muted-foreground">{{ doc.slug }}</div>
                    </div>
                    <Badge v-if="doc.pending_edits > 0" variant="warn" class="px-1.5 text-[10px]" title="Pending agent edits">
                      {{ doc.pending_edits }}
                    </Badge>
                  </div>
                </li>
              </ul>

              <!-- Folders (first slug segment) -->
              <div v-for="folder in folders" :key="folder.name">
                <button
                  type="button"
                  class="flex w-full items-center gap-1.5 border-t border-border px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground hover:bg-accent"
                  @click="toggleFolder(folder.name)"
                >
                  <component :is="collapsedFolders.has(folder.name) ? ChevronRight : ChevronDown" class="h-3.5 w-3.5" />
                  <FolderOpen class="h-3.5 w-3.5" />
                  <span class="truncate font-mono normal-case">{{ folder.name }}/</span>
                  <span class="ml-auto font-normal">{{ folder.pages.length }}</span>
                </button>
                <ul v-if="!collapsedFolders.has(folder.name)" class="divide-y divide-border">
                  <li v-for="doc in folder.pages" :key="doc.uid">
                    <div
                      :class="[
                        'flex items-center gap-2 py-2 pl-7 pr-3 cursor-pointer transition-colors',
                        selectedUid === doc.uid ? 'bg-primary/10' : 'hover:bg-accent',
                      ]"
                      @click="selectPage(doc)"
                    >
                      <button
                        type="button"
                        :title="doc.pinned ? 'Unpin — stop injecting into every run' : 'Pin — inject verbatim into every run'"
                        :class="['rounded-sm p-1', doc.pinned ? 'text-amber-600' : 'text-muted-foreground hover:text-foreground']"
                        @click.stop="togglePin(doc)"
                      >
                        <component :is="doc.pinned ? Pin : PinOff" class="h-3.5 w-3.5" />
                      </button>
                      <div class="min-w-0 flex-1">
                        <div class="flex items-center gap-1.5">
                          <span class="truncate text-sm font-medium">{{ doc.title || leafSlug(doc) }}</span>
                          <span
                            v-if="doc.stale"
                            class="h-2 w-2 shrink-0 rounded-full bg-amber-500"
                            :title="`Code changed since last review:\n${doc.stale_paths.join('\n')}`"
                          />
                        </div>
                        <div class="truncate font-mono text-[10px] text-muted-foreground">{{ leafSlug(doc) }}</div>
                      </div>
                      <Badge v-if="doc.pending_edits > 0" variant="warn" class="px-1.5 text-[10px]" title="Pending agent edits">
                        {{ doc.pending_edits }}
                      </Badge>
                    </div>
                  </li>
                </ul>
              </div>
            </template>

            <div v-else class="p-4">
              <EmptyState
                :icon="BookOpen"
                title="No pages yet"
                :description="docsGated
                  ? 'The doc tree is generated one page per area, so the wiki needs the area map first. Map the areas, then generate docs — or create a page by hand.'
                  : 'Generate docs to let an agent propose a wiki from the code — or create a page by hand.'"
                class="border-0"
              >
                <RouterLink v-if="docsGated && repoSlug" :to="{ name: 'areas', params: { repoSlug } }">
                  <Button size="sm" variant="outline">Map areas first</Button>
                </RouterLink>
                <span v-else :title="docsGated ? GATE_TOOLTIP : undefined">
                  <Button size="sm" :loading="generating" :disabled="docsGated" @click="generateDocs">
                    <Wand2 v-if="!generating" />
                    Generate docs
                  </Button>
                </span>
              </EmptyState>
            </div>
          </CardContent>
        </Card>

        <!-- ── Selected page ─────────────────────────────────────────────── -->
        <div class="space-y-4 min-w-0">
          <Card v-if="selected">
            <CardHeader class="flex-row items-start justify-between gap-2 space-y-0">
              <div class="min-w-0">
                <div class="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <span class="font-mono">{{ selected.slug }}</span>
                  <Badge v-if="selected.pinned" variant="warn" class="px-1.5 text-[10px]">
                    <Pin class="h-3 w-3" /> pinned
                  </Badge>
                  <Badge v-if="selected.stale" variant="warn" class="px-1.5 text-[10px]" :title="selected.stale_paths.join('\n')">
                    <AlertTriangle class="h-3 w-3" /> stale
                  </Badge>
                  <span v-if="selected.last_reviewed_at">reviewed {{ selected.last_reviewed_at.slice(0, 10) }}</span>
                  <span v-else-if="selected.updated_at">updated {{ selected.updated_at.slice(0, 10) }}</span>
                </div>
                <h2 class="truncate font-semibold">{{ selected.title || selected.slug }}</h2>
                <p v-if="selected.summary && !editing" class="text-xs text-muted-foreground">{{ selected.summary }}</p>
              </div>
              <div class="flex shrink-0 flex-wrap gap-2">
                <template v-if="!editing">
                  <Button
                    variant="outline"
                    size="sm"
                    :loading="drafting"
                    title="Dispatch an LLM run that drafts this page's body from its watched paths — lands as a pending edit"
                    @click="draftSelected"
                  >
                    <Wand2 v-if="!drafting" /> Draft
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    :loading="verifying"
                    :disabled="!selected.body"
                    title="Dispatch an LLM run that verifies this page's claims against the code — files findings"
                    @click="verifySelected"
                  >
                    <ShieldCheck v-if="!verifying" /> Verify
                  </Button>
                  <Button variant="outline" size="sm" @click="startEdit">
                    <Pencil /> Edit
                  </Button>
                  <Button variant="destructive" size="icon-sm" @click="deleteSelected">
                    <Trash2 />
                  </Button>
                </template>
                <template v-else>
                  <Button variant="ghost" size="sm" @click="cancelEdit">
                    <X /> Cancel
                  </Button>
                  <Button size="sm" :loading="saving" @click="saveEdit">
                    <Check /> Save
                  </Button>
                </template>
              </div>
            </CardHeader>
            <CardContent class="space-y-3">
              <!-- Run confirmation for Draft / Verify -->
              <div v-if="lastDocRun" class="rounded-md border border-border bg-muted px-3 py-2 text-xs">
                {{ lastDocRun.kind === 'draft' ? 'Draft' : 'Verify' }} run dispatched —
                <RouterLink
                  :to="{ name: 'run-detail', params: { uid: lastDocRun.runUid } }"
                  class="font-mono text-primary hover:underline"
                >{{ lastDocRun.runUid.slice(0, 8) }}</RouterLink>
              </div>

              <!-- Stale warning strip -->
              <div
                v-if="selected.stale && !editing"
                class="rounded-sm border border-amber-500/40 bg-amber-500/10 p-3 text-sm"
              >
                <div class="mb-1 flex items-center gap-1.5 font-medium text-amber-800 dark:text-amber-300">
                  <AlertTriangle class="h-4 w-4" /> Code changed since last review:
                </div>
                <ul class="space-y-0.5">
                  <li v-for="path in selected.stale_paths" :key="path" class="break-all font-mono text-xs text-muted-foreground">
                    {{ path }}
                  </li>
                </ul>
              </div>

              <template v-if="editing">
                <div class="grid gap-3 md:grid-cols-2">
                  <div class="space-y-1.5">
                    <Label>Title</Label>
                    <Input v-model="draftTitle" placeholder="Page title" />
                  </div>
                  <div class="space-y-1.5">
                    <Label>Summary (shown in the prompt index)</Label>
                    <Input v-model="draftSummary" placeholder="One line" />
                  </div>
                </div>
                <div class="space-y-1.5">
                  <Label>Watched paths (one per line — staleness is derived from them)</Label>
                  <Textarea
                    v-model="draftWatchPaths"
                    :rows="3"
                    placeholder="backend/queue/&#10;backend/workers.py"
                    class="font-mono text-xs"
                  />
                </div>
                <MarkdownView v-model="draftBody" editing min-height="360px" placeholder="Write markdown…" />
              </template>
              <template v-else>
                <MarkdownView v-if="selected.body" :model-value="selected.body" preview-only />
                <div v-else class="text-sm text-muted-foreground italic">
                  Empty page. Click Edit to write it, or Draft to let an agent propose content from the watched paths.
                </div>

                <!-- Watched paths -->
                <div>
                  <div class="mb-1 text-xs uppercase tracking-wide text-muted-foreground">Watched paths</div>
                  <div v-if="selected.watch_paths.length" class="flex flex-wrap gap-1.5">
                    <span
                      v-for="path in selected.watch_paths"
                      :key="path"
                      class="rounded-full border border-border px-2.5 py-0.5 font-mono text-xs"
                    >
                      {{ path }}
                    </span>
                  </div>
                  <div v-else class="text-xs text-muted-foreground italic">
                    No watched paths — click Edit to add the code paths this page documents.
                  </div>
                </div>
              </template>
            </CardContent>
          </Card>

          <Card v-else>
            <CardContent class="p-6">
              <EmptyState
                :icon="BookOpen"
                title="Nothing selected"
                description="Pick a page on the left."
                class="border-0"
              />
            </CardContent>
          </Card>

          <!-- Pending edits for the selected page -->
          <Card v-if="selectedEdits.length">
            <CardHeader class="flex-row items-center justify-between space-y-0">
              <CardTitle class="text-base">Pending edits for this page</CardTitle>
              <span class="text-xs text-muted-foreground">{{ selectedEdits.length }}</span>
            </CardHeader>
            <CardContent class="p-0">
              <div
                v-for="edit in selectedEdits"
                :key="edit.uid"
                class="border-b border-border p-4 last:border-b-0 space-y-2"
              >
                <div class="flex flex-wrap items-center justify-between gap-2">
                  <div class="min-w-0 text-xs text-muted-foreground">
                    <span v-if="edit.source_run_uid">
                      proposed by run
                      <RouterLink
                        :to="{ name: 'run-detail', params: { uid: edit.source_run_uid } }"
                        class="font-mono text-primary hover:underline"
                      >{{ edit.source_run_uid.slice(0, 8) }}</RouterLink>
                    </span>
                    <span v-if="edit.created_at"> · {{ edit.created_at.slice(0, 10) }}</span>
                  </div>
                  <div class="flex gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      :disabled="!!resolvingUid"
                      @click="rejectEdit(edit)"
                    >
                      <X /> Reject
                    </Button>
                    <Button
                      size="sm"
                      :loading="resolvingUid === edit.uid"
                      :disabled="!!resolvingUid && resolvingUid !== edit.uid"
                      @click="acceptEdit(edit)"
                    >
                      <Check /> Accept
                    </Button>
                  </div>
                </div>
                <p v-if="edit.rationale" class="text-sm text-muted-foreground">{{ edit.rationale }}</p>
                <div class="overflow-x-auto rounded-md border border-border bg-muted font-mono text-xs leading-5">
                  <div
                    v-for="(line, i) in diffFor(edit)"
                    :key="i"
                    :class="[
                      'whitespace-pre-wrap break-all px-3',
                      line.type === 'add' ? 'bg-green-500/15 text-green-800 dark:text-green-300' : '',
                      line.type === 'del' ? 'bg-red-500/15 text-red-800 dark:text-red-300' : '',
                      line.type === 'skip' ? 'py-0.5 text-center text-muted-foreground select-none' : '',
                    ]"
                  >
                    <template v-if="line.type === 'skip'">··· {{ line.count }} unchanged lines ···</template>
                    <template v-else>{{ line.type === 'add' ? '+' : line.type === 'del' ? '-' : ' ' }} {{ line.text }}</template>
                  </div>
                </div>
              </div>
            </CardContent>
          </Card>
        </div>
      </section>

      <!-- ── All pending edits (other pages + new-page proposals) ─────────── -->
      <Card v-if="otherEdits.length">
        <CardHeader class="flex-row flex-wrap items-center justify-between gap-2 space-y-0">
          <CardTitle class="text-base">Pending edits</CardTitle>
          <div class="flex items-center gap-2">
            <span class="text-xs text-muted-foreground">{{ otherEdits.length }} awaiting review</span>
            <Button
              variant="outline"
              size="sm"
              :loading="bulkResolving === 'reject'"
              :disabled="!!bulkResolving || !!resolvingUid"
              @click="resolveAllOther('reject')"
            >
              <X /> Reject all
            </Button>
            <Button
              size="sm"
              :loading="bulkResolving === 'accept'"
              :disabled="!!bulkResolving || !!resolvingUid"
              @click="resolveAllOther('accept')"
            >
              <Check /> Accept all
            </Button>
          </div>
        </CardHeader>
        <CardContent class="p-0">
          <div
            v-for="edit in otherEdits"
            :key="edit.uid"
            class="border-b border-border p-4 last:border-b-0 space-y-2"
          >
            <div class="flex flex-wrap items-center justify-between gap-2">
              <div class="min-w-0">
                <div class="text-sm font-medium">
                  {{ editTarget(edit) }}
                  <Badge v-if="!edit.doc_uid" variant="info" class="px-1.5 text-[10px]">new page</Badge>
                </div>
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
                <Button variant="outline" size="sm" :disabled="!!resolvingUid || !!bulkResolving" @click="rejectEdit(edit)">
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
            <div class="overflow-x-auto rounded-md border border-border bg-muted font-mono text-xs leading-5">
              <div
                v-for="(line, i) in diffFor(edit)"
                :key="i"
                :class="[
                  'whitespace-pre-wrap break-all px-3',
                  line.type === 'add' ? 'bg-green-500/15 text-green-800 dark:text-green-300' : '',
                  line.type === 'del' ? 'bg-red-500/15 text-red-800 dark:text-red-300' : '',
                  line.type === 'skip' ? 'py-0.5 text-center text-muted-foreground select-none' : '',
                ]"
              >
                <template v-if="line.type === 'skip'">··· {{ line.count }} unchanged lines ···</template>
                <template v-else>{{ line.type === 'add' ? '+' : line.type === 'del' ? '-' : ' ' }} {{ line.text }}</template>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- ── Memories ─────────────────────────────────────────────────────── -->
      <Card>
        <CardHeader class="flex-row flex-wrap items-center justify-between gap-2 space-y-0">
          <CardTitle class="flex items-center gap-2 text-sm">
            <Brain class="h-4 w-4 text-muted-foreground" /> Memories
            <span class="text-xs font-normal text-muted-foreground">· {{ memories.list.length }}</span>
          </CardTitle>
          <div class="relative w-full sm:w-64">
            <Search class="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground z-10" />
            <Input v-model="memoryQuery" placeholder="Search memories" class="pl-9" />
          </div>
        </CardHeader>
        <CardContent class="p-0">
          <div v-if="memoriesLoading && !memories.list.length" class="space-y-2 p-4">
            <Skeleton v-for="i in 3" :key="i" class="h-12" />
          </div>
          <div v-else-if="!memories.list.length" class="p-4">
            <EmptyState
              :icon="Brain"
              title="No memories"
              description="Agents record small learned facts here during runs. Curation is a delete button."
              class="border-0"
            />
          </div>
          <ul v-else class="divide-y divide-border">
            <li v-for="m in memories.list" :key="m.uid" class="flex items-start gap-3 px-4 py-3">
              <div class="min-w-0 flex-1">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="text-sm font-medium">{{ m.title }}</span>
                  <Badge v-if="m.possibly_stale" variant="warn" class="px-1.5 text-[10px]">code changed since</Badge>
                </div>
                <p class="mt-0.5 whitespace-pre-line text-sm text-muted-foreground">{{ m.body }}</p>
                <div class="mt-1 text-xs text-muted-foreground">
                  <RouterLink
                    v-if="m.source_run_uid"
                    :to="{ name: 'run-detail', params: { uid: m.source_run_uid } }"
                    class="font-mono text-primary hover:underline"
                  >run {{ m.source_run_uid.slice(0, 8) }}</RouterLink>
                  <span v-if="m.updated_at"> · {{ m.updated_at.slice(0, 10) }}</span>
                </div>
              </div>
              <Button variant="ghost" size="icon-sm" class="text-destructive" title="Delete memory" @click="deleteMemory(m)">
                <Trash2 />
              </Button>
            </li>
          </ul>
        </CardContent>
      </Card>
    </template>

    <!-- ── Update docs dialog ──────────────────────────────────────────────── -->
    <Dialog :open="updateDocsOpen" @update:open="updateDocsOpen = $event">
      <DialogContent class="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Update documentation</DialogTitle>
          <DialogDescription>
            Dispatches a document run: the agent compares these pages and memories against the current code, proposes edits (and new pages) as reviewable diffs, and prunes stale memories.
          </DialogDescription>
        </DialogHeader>
        <div class="space-y-3">
          <div class="space-y-1.5">
            <Label>Prompt</Label>
            <Select v-model="docPromptSelection">
              <SelectTrigger class="w-full">
                <SelectValue placeholder="Default document playbook" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem :value="DEFAULT_PROMPT">Default document playbook</SelectItem>
                <SelectItem v-for="p in docPrompts" :key="p.uid" :value="p.uid">{{ p.title }}</SelectItem>
              </SelectContent>
            </Select>
            <p class="text-xs text-muted-foreground">
              The default runs this repository's configured document-stage guidance (Workflow card).
              Alternative strategies with job type <code class="font-mono">document</code> appear here — manage them under
              <RouterLink :to="{ name: 'admin-agent-prompts' }" class="text-primary hover:underline">Admin › Agent prompts</RouterLink>.
            </p>
          </div>
        </div>
        <DialogFooter>
          <Button variant="ghost" size="sm" @click="updateDocsOpen = false">Cancel</Button>
          <Button size="sm" :loading="dispatchingDocs" @click="dispatchDocumentRun">
            <Sparkles /> Dispatch run
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <!-- ── New page dialog ─────────────────────────────────────────────────── -->
    <Dialog :open="createOpen" @update:open="createOpen = $event">
      <DialogContent class="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>New documentation page</DialogTitle>
          <DialogDescription>
            A curated markdown page about this repository. Use / in the slug to file it under a folder; pin it later to inject it into every run.
          </DialogDescription>
        </DialogHeader>
        <div class="space-y-3">
          <div class="space-y-1.5">
            <Label>Title</Label>
            <Input v-model="newTitle" placeholder="e.g. Queue workers" @keydown.enter="createPage" />
          </div>
          <div class="space-y-1.5">
            <Label>Slug</Label>
            <Input v-model="newSlug" placeholder="e.g. backend/queue-workers" class="font-mono" @keydown.enter="createPage" />
          </div>
          <div class="space-y-1.5">
            <Label>Summary</Label>
            <Input v-model="newSummary" placeholder="One line, shown in the prompt index" />
          </div>
          <div class="space-y-1.5">
            <Label>Watched paths (optional, one per line)</Label>
            <Textarea
              v-model="newWatchPaths"
              :rows="3"
              placeholder="backend/queue/&#10;backend/workers.py"
              class="font-mono text-xs"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="ghost" size="sm" @click="createOpen = false">Cancel</Button>
          <Button size="sm" :disabled="!newSlug.trim()" :loading="creating" @click="createPage">
            <Plus /> Create page
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <AlertDialog v-model:open="deletePageOpen">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete page</AlertDialogTitle>
          <AlertDialogDescription>
            Delete page "{{ pendingDeletePage?.title || pendingDeletePage?.slug }}"? This cannot be undone.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            class="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            @click="confirmDeleteSelected"
          >
            Delete
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>

    <AlertDialog v-model:open="resetDocsOpen">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete all documentation?</AlertDialogTitle>
          <AlertDialogDescription>
            This permanently deletes all {{ docs.list.length }} page{{ docs.list.length === 1 ? '' : 's' }}
            and {{ docs.edits.length }} pending edit{{ docs.edits.length === 1 ? '' : 's' }} for this repository.
            Memories anchored to these pages keep their content but lose their freshness anchor, and
            audits lose their doc-derived scoping until docs are regenerated. This cannot be undone.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction class="bg-destructive text-destructive-foreground hover:bg-destructive/90" @click="confirmResetDocs">
            Delete everything
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>

    <AlertDialog v-model:open="bulkResolveOpen">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{{ pendingBulkAction === 'accept' ? 'Accept' : 'Reject' }} pending edits</AlertDialogTitle>
          <AlertDialogDescription>
            {{ pendingBulkAction === 'accept' ? 'Accept' : 'Reject' }} all {{ pendingBulkUids.length }} pending edit{{ pendingBulkUids.length === 1 ? '' : 's' }}?
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction @click="confirmResolveAllOther">
            {{ pendingBulkAction === 'accept' ? 'Accept all' : 'Reject all' }}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>

    <AlertDialog v-model:open="deleteMemoryOpen">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete memory</AlertDialogTitle>
          <AlertDialogDescription>
            Delete memory "{{ pendingDeleteMemory?.title }}"?
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            class="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            @click="confirmDeleteMemory"
          >
            Delete
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </div>
</template>
