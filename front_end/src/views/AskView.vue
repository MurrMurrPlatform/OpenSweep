<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter, RouterLink } from 'vue-router'
import { Sparkles, FileText, ChevronRight, Pencil, Save } from 'lucide-vue-next'
import {
  useAgentPromptStore,
  isAgentBasePrompt,
  isStageDefaultPrompt,
  type AgentPromptDTO,
} from '@/stores/agentPromptStore'
import { useDocStore } from '@/stores/docStore'
import { useCurrentRepo } from '@/composables/useCurrentRepo'
import { useToast } from '@/composables/useToast'
import { PageHeader } from '@/components/ui/page-header'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import MarkdownView from '@/components/ui/markdown/MarkdownView.vue'

const agentPrompts = useAgentPromptStore()
const docs = useDocStore()
const router = useRouter()
const toast = useToast()
const { uid: repoUid, repo: currentRepo } = useCurrentRepo()

const prompts = ref<AgentPromptDTO[]>([])

const activeTag = ref<string>('all')
const selectedPrompt = ref<AgentPromptDTO | null>(null)
const customMode = ref(false)
const editingBody = ref(false)
const body = ref('')
const effort = ref<'short' | 'normal' | 'deep' | 'unlimited'>('normal')
const submitting = ref(false)

// The Ask page dispatches audit runs. Hide internal composition layers:
// agent bases (opensweep://agent/*) are already every run's instructions
// layer, and stage defaults for other stages (review/fix/document/…) belong
// to flows that apply them automatically — only the ask stage default is a
// meaningful card here.
function launchable(all: AgentPromptDTO[]): AgentPromptDTO[] {
  return all.filter(
    (p) =>
      !isAgentBasePrompt(p) &&
      (!isStageDefaultPrompt(p) || p.source_url === 'opensweep://workflow/ask'),
  )
}

onMounted(async () => {
  prompts.value = launchable(await agentPrompts.fetchAll({ enabled_only: true }))
})

const allTags = computed(() => {
  const tags = new Set<string>()
  for (const p of prompts.value) for (const t of p.tags) tags.add(t)
  return ['all', ...Array.from(tags).sort()]
})

const filteredPrompts = computed(() => {
  if (activeTag.value === 'all') return prompts.value
  return prompts.value.filter((p) => p.tags.includes(activeTag.value))
})

function pickPrompt(p: AgentPromptDTO) {
  selectedPrompt.value = p
  customMode.value = false
  editingBody.value = false
  body.value = p.body
  const eff = (p.default_effort || 'normal').toString()
  const legacy: Record<string, string> = { quick: 'short', small: 'short', light: 'short', large: 'deep' }
  const mapped = legacy[eff] ?? eff
  effort.value = (['short', 'normal', 'deep', 'unlimited'].includes(mapped) ? mapped : 'normal') as
    | 'short'
    | 'normal'
    | 'deep'
    | 'unlimited'
}

function startCustom() {
  selectedPrompt.value = null
  customMode.value = true
  editingBody.value = true
  body.value = ''
  effort.value = 'normal'
}

const savePromptOpen = ref(false)
const newPromptTitle = ref('')

function saveAsNewPrompt() {
  if (!body.value.trim()) {
    toast.warn('Cannot save', 'Add some content first.')
    return
  }
  newPromptTitle.value = selectedPrompt.value?.title || 'New prompt'
  savePromptOpen.value = true
}

async function submitSaveAsNewPrompt() {
  const title = newPromptTitle.value.trim()
  // Empty title aborts, exactly like the old window.prompt cancel/empty path.
  if (!title) return
  savePromptOpen.value = false
  try {
    const created = await agentPrompts.create({
      title,
      description: selectedPrompt.value?.description || '',
      body: body.value,
      default_scope: 'repository',
      default_effort: effort.value,
      default_job_type: 'audit',
      tags: selectedPrompt.value?.tags || [],
      enabled: true,
    })
    prompts.value = launchable(await agentPrompts.fetchAll({ enabled_only: true }))
    const found = prompts.value.find((p) => p.uid === created.uid)
    if (found) pickPrompt(found)
    toast.info('Prompt saved', `"${title}" added to library.`)
  } catch (e: unknown) {
    toast.error('Save failed', e instanceof Error ? e.message : String(e))
  }
}

const canSubmit = computed(() => {
  if (!repoUid.value) return false
  if (!body.value.trim()) return false
  return true
})

async function submit() {
  if (submitting.value || !canSubmit.value || !repoUid.value) return
  submitting.value = true
  try {
    // A per-run edit to a library prompt's body must actually take effect:
    // send the edited text as custom_intent and drop the prompt uid so the
    // server uses the edit, not the stored body. Unedited prompt → send the
    // uid; custom mode → send the body directly.
    const edited =
      !customMode.value &&
      selectedPrompt.value != null &&
      body.value !== selectedPrompt.value.body
    const useCustom = customMode.value || edited
    // No doc uids: the backend dispatches one repo-scoped ask run instead of
    // fanning out per documentation page.
    const result = await docs.audit(repoUid.value, [], {
      agent_prompt_uid: useCustom ? undefined : selectedPrompt.value?.uid,
      custom_intent: useCustom ? body.value : undefined,
      effort: effort.value,
    })
    if (result.runs_dispatched.length > 0) {
      router.push({ name: 'run-detail', params: { uid: result.runs_dispatched[0] } })
    } else {
      toast.warn('Audit dispatched but no runs queued', result.summary || 'Check logs.')
    }
  } catch (e: unknown) {
    toast.error('Audit failed', e instanceof Error ? e.message : String(e))
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="space-y-4">
    <PageHeader
      title="Ask"
      subtitle="Pick a curated prompt — or write your own — and dispatch an Audit run. Output lands as Findings in your inbox."
    />

    <!-- Tag filter -->
    <div class="flex flex-wrap gap-2">
      <button
        v-for="tag in allTags"
        :key="tag"
        type="button"
        :class="[
          'rounded-full border px-3 py-1 text-xs uppercase tracking-wide transition-colors',
          activeTag === tag
            ? 'border-primary bg-primary/10 text-primary'
            : 'border-border text-muted-foreground hover:bg-accent',
        ]"
        @click="activeTag = tag"
      >
        {{ tag }}
      </button>
    </div>

    <!-- Prompt grid -->
    <section class="stagger-children grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
      <button
        v-for="p in filteredPrompts"
        :key="p.uid"
        type="button"
        :class="[
          'card-interactive rounded-lg border bg-card p-4 text-left shadow-sm hover:bg-accent',
          selectedPrompt?.uid === p.uid ? 'border-primary ring-2 ring-primary/20' : 'border-border',
        ]"
        @click="pickPrompt(p)"
      >
        <div class="flex items-start gap-2">
          <FileText class="h-4 w-4 flex-shrink-0 text-muted-foreground" />
          <div class="min-w-0 flex-1">
            <div class="truncate text-sm font-semibold">{{ p.title }}</div>
            <div class="mt-1 text-xs text-muted-foreground line-clamp-2">{{ p.description || '—' }}</div>
          </div>
        </div>
        <div class="mt-3 flex flex-wrap gap-1">
          <span
            v-for="t in p.tags.slice(0, 3)"
            :key="t"
            class="rounded-md bg-muted px-1.5 py-0.5 text-[10px] uppercase text-muted-foreground"
          >
            {{ t }}
          </span>
          <Badge v-if="p.source === 'user'" class="px-1.5 text-[10px] uppercase">user</Badge>
        </div>
      </button>

      <!-- Custom card -->
      <button
        type="button"
        :class="[
          'card-interactive rounded-lg border-2 border-dashed p-4 text-left hover:bg-accent',
          customMode ? 'border-primary bg-primary/5' : 'border-border',
        ]"
        @click="startCustom"
      >
        <div class="flex items-start gap-2">
          <Pencil class="h-4 w-4 flex-shrink-0 text-muted-foreground" />
          <div>
            <div class="text-sm font-semibold">Custom…</div>
            <div class="mt-1 text-xs text-muted-foreground">Write a free-form prompt for this one-off audit.</div>
          </div>
        </div>
      </button>
    </section>

    <!-- Selected prompt / config -->
    <Card v-if="selectedPrompt || customMode">
      <CardContent class="space-y-4 p-6">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div class="min-w-0">
            <div class="text-xs uppercase text-muted-foreground">{{ customMode ? 'Custom prompt' : 'Selected prompt' }}</div>
            <div class="truncate text-base font-semibold">{{ selectedPrompt?.title || 'Free-form audit' }}</div>
          </div>
          <div class="flex flex-wrap gap-2">
            <Button
              v-if="!customMode && selectedPrompt"
              variant="outline"
              size="sm"
              as="router-link"
              :to="{ name: 'admin-agent-prompts' }"
            >
              <Pencil />
              Edit in library
            </Button>
            <Button
              v-if="body.trim()"
              variant="outline"
              size="sm"
              @click="saveAsNewPrompt"
            >
              <Save />
              Save as new prompt
            </Button>
            <Button
              variant="outline"
              size="sm"
              @click="editingBody = !editingBody"
            >
              <Pencil />
              {{ editingBody ? 'Done editing' : 'Edit body for this run' }}
            </Button>
          </div>
        </div>

        <MarkdownView
          v-model="body"
          :editing="editingBody"
          min-height="280px"
          placeholder="Describe what the agent should look for…"
        />

        <!-- Effort -->
        <div class="grid gap-3 md:grid-cols-2">
          <div class="space-y-1.5">
            <Label>Effort</Label>
            <Select v-model="effort">
              <SelectTrigger class="w-full sm:w-56">
                <SelectValue placeholder="Effort" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="short">Short</SelectItem>
                <SelectItem value="normal">Normal</SelectItem>
                <SelectItem value="deep">Deep</SelectItem>
                <SelectItem value="unlimited">Unlimited</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        <div class="flex flex-wrap items-center justify-between gap-2 border-t pt-3">
          <div class="text-xs text-muted-foreground">
            Workspace: <span class="font-mono">{{ currentRepo?.slug || '—' }}</span>
          </div>
          <Button
            :disabled="!canSubmit"
            :loading="submitting"
            @click="submit"
          >
            <Sparkles />
            Run audit
            <ChevronRight />
          </Button>
        </div>
      </CardContent>
    </Card>

    <Card v-else>
      <CardContent class="p-6">
        <div class="text-sm text-muted-foreground">
          Pick a prompt above to get started, or click <strong>Custom…</strong> to write your own.
        </div>
        <div class="mt-3 text-xs text-muted-foreground">
          Prompts are managed under
          <RouterLink :to="{ name: 'admin-agent-prompts' }" class="text-primary hover:underline">Admin › Agent prompts</RouterLink>.
        </div>
      </CardContent>
    </Card>

    <Dialog v-model:open="savePromptOpen">
      <DialogContent class="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>New prompt title?</DialogTitle>
        </DialogHeader>
        <div class="space-y-2">
          <Label for="new-prompt-title">Title</Label>
          <Input
            id="new-prompt-title"
            v-model="newPromptTitle"
            autofocus
            @keydown.enter.prevent="submitSaveAsNewPrompt"
          />
        </div>
        <DialogFooter>
          <Button variant="ghost" size="sm" @click="savePromptOpen = false">Cancel</Button>
          <Button size="sm" :disabled="!newPromptTitle.trim()" @click="submitSaveAsNewPrompt">
            <Save /> Save prompt
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>
