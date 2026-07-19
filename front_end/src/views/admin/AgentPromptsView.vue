<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Pencil, Plus, Save, Trash2, Download } from 'lucide-vue-next'
import {
  useAgentPromptStore,
  isAgentBasePrompt,
  isStageDefaultPrompt,
  type AgentPromptDTO,
} from '@/stores/agentPromptStore'
import { useToast } from '@/composables/useToast'
import { PageHeader } from '@/components/ui/page-header'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
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
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Skeleton } from '@/components/ui/skeleton'
import MarkdownView from '@/components/ui/markdown/MarkdownView.vue'

const store = useAgentPromptStore()
const toast = useToast()

// SelectItem values can't be empty strings — sentinel for "all sources".
const ALL_SOURCES = '__all__'

const prompts = ref<AgentPromptDTO[]>([])
const loading = ref(true)
const importing = ref(false)
const saving = ref(false)
const editing = ref<AgentPromptDTO | null>(null)
const deleteOpen = ref(false)
const pendingDelete = ref<AgentPromptDTO | null>(null)
const filterTag = ref<string>('')
const filterSource = ref<string>(ALL_SOURCES)

onMounted(reload)

async function reload() {
  loading.value = true
  try {
    prompts.value = await store.fetchAll()
  } finally {
    loading.value = false
  }
}

const allTags = computed(() => {
  const set = new Set<string>()
  for (const p of prompts.value) for (const t of p.tags) set.add(t)
  return Array.from(set).sort()
})

const filtered = computed(() => {
  return prompts.value.filter((p) => {
    if (filterTag.value && !p.tags.includes(filterTag.value)) return false
    if (filterSource.value !== ALL_SOURCES && p.source !== filterSource.value) return false
    return true
  })
})

async function importFromEcc() {
  importing.value = true
  try {
    const result = await store.importEcc()
    toast.info(
      'ECC import',
      `${result.imported} imported, ${result.skipped_user_edited} user-edited kept. commit=${result.source_commit.slice(0, 8)}`,
    )
    await reload()
  } catch (e: unknown) {
    toast.error('Import failed', e instanceof Error ? e.message : String(e))
  } finally {
    importing.value = false
  }
}

function startEdit(p: AgentPromptDTO) {
  editing.value = JSON.parse(JSON.stringify(p))
}

function startCreate() {
  editing.value = {
    uid: '',
    title: '',
    description: '',
    body: '',
    default_job_type: 'audit',
    default_scope: 'repository',
    default_effort: 'normal',
    tags: [],
    source: 'user',
    source_url: '',
    source_commit: '',
    enabled: true,
  } as AgentPromptDTO
}

function cancelEdit() {
  editing.value = null
}

async function saveEdit() {
  if (!editing.value || saving.value) return
  saving.value = true
  try {
    if (!editing.value.uid) {
      await store.create({
        title: editing.value.title,
        description: editing.value.description,
        body: editing.value.body,
        default_job_type: editing.value.default_job_type,
        default_scope: editing.value.default_scope,
        default_effort: editing.value.default_effort,
        tags: editing.value.tags,
        enabled: editing.value.enabled,
      })
      toast.info('Prompt created', editing.value.title)
    } else {
      await store.update(editing.value.uid, {
        title: editing.value.title,
        description: editing.value.description,
        body: editing.value.body,
        default_job_type: editing.value.default_job_type,
        default_scope: editing.value.default_scope,
        default_effort: editing.value.default_effort,
        tags: editing.value.tags,
        enabled: editing.value.enabled,
      })
      toast.info('Prompt updated', editing.value.title)
    }
    editing.value = null
    await reload()
  } catch (e: unknown) {
    toast.error('Save failed', e instanceof Error ? e.message : String(e))
  } finally {
    saving.value = false
  }
}

function deletePrompt(p: AgentPromptDTO) {
  pendingDelete.value = p
  deleteOpen.value = true
}

async function confirmDeletePrompt() {
  const p = pendingDelete.value
  if (!p) return
  deleteOpen.value = false
  try {
    await store.remove(p.uid)
    toast.info('Prompt deleted', p.title)
    await reload()
  } catch (e: unknown) {
    toast.error('Delete failed', e instanceof Error ? e.message : String(e))
  }
}

async function toggleEnabled(p: AgentPromptDTO) {
  try {
    await store.update(p.uid, { enabled: !p.enabled })
    await reload()
  } catch (e: unknown) {
    toast.error('Toggle failed', e instanceof Error ? e.message : String(e))
  }
}

function sourceBadge(p: AgentPromptDTO) {
  if (p.source === 'imported') return 'bg-primary/10 text-primary'
  if (p.source === 'user') return 'bg-good/15 text-good'
  return 'bg-muted text-muted-foreground'
}

/** Which composition role a seeded platform row plays, so admins can tell
 * the always-applied layers apart from selectable strategies. */
function layerLabel(p: AgentPromptDTO): string {
  if (isAgentBasePrompt(p)) return 'agent base'
  if (isStageDefaultPrompt(p)) return 'stage default'
  return ''
}
</script>

<template>
  <div class="space-y-4">
    <PageHeader
      title="Agent prompts"
      subtitle="Reusable LLM intents for Audit, Discover, and Maintain runs. Imported from github.com/affaan-m/ECC and editable here."
    >
      <Button variant="outline" :loading="importing" @click="importFromEcc">
        <Download />
        Re-import from ECC
      </Button>
      <Button @click="startCreate">
        <Plus />
        New prompt
      </Button>
    </PageHeader>

    <Card>
      <CardContent class="p-4">
        <div class="flex flex-wrap items-center gap-2 text-xs">
          <span class="text-muted-foreground">Filters:</span>
          <Select v-model="filterSource">
            <SelectTrigger class="h-8 w-full sm:w-40 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem :value="ALL_SOURCES">All sources</SelectItem>
              <SelectItem value="imported">Imported</SelectItem>
              <SelectItem value="user">User</SelectItem>
              <SelectItem value="platform">Platform</SelectItem>
            </SelectContent>
          </Select>
          <button
            type="button"
            :class="[
              'rounded-full border px-2 py-0.5 uppercase transition-colors',
              filterTag === '' ? 'border-primary bg-primary/10 text-primary' : 'text-muted-foreground',
            ]"
            @click="filterTag = ''"
          >
            All tags
          </button>
          <button
            v-for="t in allTags"
            :key="t"
            type="button"
            :class="[
              'rounded-full border px-2 py-0.5 uppercase transition-colors',
              filterTag === t ? 'border-primary bg-primary/10 text-primary' : 'text-muted-foreground',
            ]"
            @click="filterTag = t"
          >
            {{ t }}
          </button>
        </div>
      </CardContent>
    </Card>

    <Card>
      <CardContent class="p-0">
        <div v-if="loading" class="space-y-2 p-4">
          <Skeleton v-for="i in 6" :key="i" class="h-12" />
        </div>
        <div v-else class="overflow-x-auto">
          <Table class="text-sm">
            <TableHeader>
              <TableRow>
                <TableHead>Title</TableHead>
                <TableHead>Tags</TableHead>
                <TableHead>Job type</TableHead>
                <TableHead>Source</TableHead>
                <TableHead>Enabled</TableHead>
                <TableHead></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <TableRow v-for="p in filtered" :key="p.uid">
                <TableCell class="max-w-[320px]">
                  <div class="flex items-center gap-2">
                    <span class="truncate font-medium">{{ p.title }}</span>
                    <span
                      v-if="layerLabel(p)"
                      class="flex-shrink-0 rounded-md bg-primary/10 px-1.5 py-0.5 text-[10px] uppercase text-primary"
                      title="Applied automatically by run composition — not shown in run-launch pickers"
                    >
                      {{ layerLabel(p) }}
                    </span>
                  </div>
                  <div class="truncate text-xs text-muted-foreground">{{ p.description }}</div>
                </TableCell>
                <TableCell>
                  <div class="flex flex-wrap gap-1">
                    <span
                      v-for="t in p.tags"
                      :key="t"
                      class="rounded-md bg-muted px-1.5 py-0.5 text-[10px] uppercase text-muted-foreground"
                    >
                      {{ t }}
                    </span>
                  </div>
                </TableCell>
                <TableCell class="text-xs font-mono">{{ p.default_job_type }}</TableCell>
                <TableCell>
                  <span :class="['rounded-md px-1.5 py-0.5 text-[10px] uppercase', sourceBadge(p)]">{{ p.source }}</span>
                </TableCell>
                <TableCell>
                  <Switch :model-value="p.enabled" @update:model-value="toggleEnabled(p)" />
                </TableCell>
                <TableCell class="whitespace-nowrap text-right">
                  <Button variant="ghost" size="sm" @click="startEdit(p)">
                    <Pencil />
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    class="text-destructive"
                    @click="deletePrompt(p)"
                  >
                    <Trash2 />
                  </Button>
                </TableCell>
              </TableRow>
              <TableRow v-if="!filtered.length">
                <TableCell colspan="6" class="p-4 text-center text-muted-foreground">No prompts match.</TableCell>
              </TableRow>
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>

    <Dialog :open="editing !== null" @update:open="(v) => { if (!v) cancelEdit() }">
      <DialogContent v-if="editing" class="sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>{{ editing.uid ? 'Edit prompt' : 'New prompt' }}</DialogTitle>
        </DialogHeader>
        <div class="max-h-[70vh] space-y-3 overflow-y-auto -mx-6 px-6">
          <div class="space-y-1.5">
            <Label for="prompt-title" class="text-xs uppercase text-muted-foreground">Title</Label>
            <Input id="prompt-title" v-model="editing.title" />
          </div>
          <div class="space-y-1.5">
            <Label for="prompt-description" class="text-xs uppercase text-muted-foreground">Description</Label>
            <Input id="prompt-description" v-model="editing.description" />
          </div>
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <div class="space-y-1.5">
              <Label class="text-xs uppercase text-muted-foreground">Job type</Label>
              <Select v-model="editing.default_job_type">
                <SelectTrigger class="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="audit">audit</SelectItem>
                  <SelectItem value="implement">implement</SelectItem>
                  <SelectItem value="sweep">sweep</SelectItem>
                  <SelectItem value="curate-map">curate-map</SelectItem>
                  <SelectItem value="discover-capabilities">discover-capabilities</SelectItem>
                  <SelectItem value="document">document</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div class="space-y-1.5">
              <Label class="text-xs uppercase text-muted-foreground">Default scope</Label>
              <Select v-model="editing.default_scope">
                <SelectTrigger class="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="repository">repository</SelectItem>
                  <SelectItem value="paths">paths</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div class="space-y-1.5">
              <Label class="text-xs uppercase text-muted-foreground">Default effort</Label>
              <Select v-model="editing.default_effort">
                <SelectTrigger class="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="short">short</SelectItem>
                  <SelectItem value="normal">normal</SelectItem>
                  <SelectItem value="deep">deep</SelectItem>
                  <SelectItem value="unlimited">unlimited</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <div class="space-y-1.5">
            <Label for="prompt-tags" class="text-xs uppercase text-muted-foreground">Tags (comma-separated)</Label>
            <Input
              id="prompt-tags"
              :model-value="editing.tags.join(', ')"
              @update:model-value="(v: string | number) => (editing!.tags = String(v).split(',').map((s) => s.trim()).filter(Boolean))"
            />
          </div>
          <div class="space-y-1.5">
            <Label class="text-xs uppercase text-muted-foreground">Body (markdown)</Label>
            <MarkdownView v-model="editing.body" editing min-height="320px" />
          </div>
          <div v-if="editing.source_url" class="text-xs text-muted-foreground">
            Source:
            <a :href="editing.source_url" target="_blank" rel="noopener" class="text-primary hover:underline">{{ editing.source_url }}</a>
            <span v-if="editing.source_commit"> · commit {{ editing.source_commit.slice(0, 8) }}</span>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" @click="cancelEdit">Cancel</Button>
          <Button :loading="saving" @click="saveEdit">
            <Save />
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <AlertDialog v-model:open="deleteOpen">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete prompt</AlertDialogTitle>
          <AlertDialogDescription>
            Delete prompt "{{ pendingDelete?.title }}"?
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction
            class="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            @click="confirmDeletePrompt"
          >
            Delete
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </div>
</template>
