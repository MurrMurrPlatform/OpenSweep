<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter, RouterLink } from 'vue-router'
import { Sparkles, FileText, ChevronRight, Pencil, Save } from 'lucide-vue-next'
import { useAgentStore } from '@/stores/agentStore'
import { useDocStore } from '@/stores/docStore'
import { useCurrentRepo } from '@/composables/useCurrentRepo'
import { useToast } from '@/composables/useToast'
import { PageHeader } from '@/components/ui/page-header'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import MarkdownView from '@/components/ui/markdown/MarkdownView.vue'
import SaveAsAgentDialog from '@/components/agents/SaveAsAgentDialog.vue'
import type { AgentDTO } from '@/types/api'

const agents = useAgentStore()
const docs = useDocStore()
const router = useRouter()
const toast = useToast()
const { uid: repoUid, repo: currentRepo } = useCurrentRepo()

const available = ref<AgentDTO[]>([])

const activeTag = ref<string>('all')
const selectedAgent = ref<AgentDTO | null>(null)
const customMode = ref(false)
const editingBody = ref(false)
const body = ref('')
const effort = ref<'quick' | 'normal' | 'deep'>('normal')
const submitting = ref(false)

// The Ask page dispatches ask runs: agents that produce findings or answers
// are launchable here. System write/review/verification agents belong to
// flows that dispatch them automatically.
function launchable(all: AgentDTO[]): AgentDTO[] {
  return all.filter((a) => a.produces === 'findings' || a.produces === 'answer')
}

onMounted(async () => {
  available.value = launchable(await agents.fetchAll({ enabled_only: true }))
})

const allTags = computed(() => {
  const tags = new Set<string>()
  for (const a of available.value) for (const t of a.tags) tags.add(t)
  return ['all', ...Array.from(tags).sort()]
})

const filteredAgents = computed(() => {
  if (activeTag.value === 'all') return available.value
  return available.value.filter((a) => a.tags.includes(activeTag.value))
})

function pickAgent(a: AgentDTO) {
  selectedAgent.value = a
  customMode.value = false
  editingBody.value = false
  body.value = a.prompt
  const eff = (a.default_effort || 'normal').toString()
  effort.value = (['quick', 'normal', 'deep'].includes(eff) ? eff : 'normal') as
    | 'quick'
    | 'normal'
    | 'deep'
}

function startCustom() {
  selectedAgent.value = null
  customMode.value = true
  editingBody.value = true
  body.value = ''
  effort.value = 'normal'
}

const saveAgentOpen = ref(false)

function saveAsAgent() {
  if (!body.value.trim()) {
    toast.warn('Cannot save', 'Add some content first.')
    return
  }
  saveAgentOpen.value = true
}

async function onAgentSaved(created: AgentDTO) {
  available.value = launchable(await agents.fetchAll({ enabled_only: true }))
  const found = available.value.find((a) => a.uid === created.uid)
  if (found) pickAgent(found)
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
    // A per-run edit to an agent's prompt must actually take effect: send the
    // edited text as custom_intent and drop the agent uid so the server uses
    // the edit, not the stored prompt. Unedited agent → send the uid; custom
    // mode → send the body directly.
    const edited =
      !customMode.value &&
      selectedAgent.value != null &&
      body.value !== selectedAgent.value.prompt
    const useCustom = customMode.value || edited
    // No doc uids: the backend dispatches one repo-scoped ask run instead of
    // fanning out per documentation page.
    const result = await docs.audit(repoUid.value, [], {
      agent_uid: useCustom ? undefined : selectedAgent.value?.uid,
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
      subtitle="Pick an agent — or write your own prompt — and dispatch a run. Output lands as Findings in your inbox."
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

    <!-- Agent grid -->
    <section class="stagger-children grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
      <button
        v-for="a in filteredAgents"
        :key="a.uid"
        type="button"
        :class="[
          'card-interactive rounded-lg border bg-card p-4 text-left shadow-sm hover:bg-accent',
          selectedAgent?.uid === a.uid ? 'border-primary ring-2 ring-primary/20' : 'border-border',
        ]"
        @click="pickAgent(a)"
      >
        <div class="flex items-start gap-2">
          <FileText class="h-4 w-4 flex-shrink-0 text-muted-foreground" />
          <div class="min-w-0 flex-1">
            <div class="truncate text-sm font-semibold">{{ a.title }}</div>
            <div class="mt-1 text-xs text-muted-foreground line-clamp-2">{{ a.description || '—' }}</div>
          </div>
        </div>
        <div class="mt-3 flex flex-wrap gap-1">
          <span
            v-for="t in a.tags.slice(0, 3)"
            :key="t"
            class="rounded-md bg-muted px-1.5 py-0.5 text-[10px] uppercase text-muted-foreground"
          >
            {{ t }}
          </span>
          <Badge v-if="a.provenance === 'user'" class="px-1.5 text-[10px] uppercase">yours</Badge>
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
            <div class="mt-1 text-xs text-muted-foreground">Write a free-form prompt for this one-off run.</div>
          </div>
        </div>
      </button>
    </section>

    <!-- Selected agent / config -->
    <Card v-if="selectedAgent || customMode">
      <CardContent class="space-y-4 p-6">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div class="min-w-0">
            <div class="text-xs uppercase text-muted-foreground">{{ customMode ? 'Custom prompt' : 'Selected agent' }}</div>
            <div class="truncate text-base font-semibold">{{ selectedAgent?.title || 'Free-form run' }}</div>
          </div>
          <div class="flex flex-wrap gap-2">
            <Button
              v-if="!customMode && selectedAgent"
              variant="outline"
              size="sm"
              as="router-link"
              :to="{ name: 'agent-detail', params: { uid: selectedAgent.uid } }"
            >
              <Pencil />
              Edit in library
            </Button>
            <Button
              v-if="body.trim()"
              variant="outline"
              size="sm"
              @click="saveAsAgent"
            >
              <Save />
              Save as agent
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
                <SelectItem value="quick">Quick</SelectItem>
                <SelectItem value="normal">Normal</SelectItem>
                <SelectItem value="deep">Deep</SelectItem>
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
            Run
            <ChevronRight />
          </Button>
        </div>
      </CardContent>
    </Card>

    <Card v-else>
      <CardContent class="p-6">
        <div class="text-sm text-muted-foreground">
          Pick an agent above to get started, or click <strong>Custom…</strong> to write your own.
        </div>
        <div class="mt-3 text-xs text-muted-foreground">
          Agents are managed in the
          <RouterLink :to="{ name: 'agent-library' }" class="text-primary hover:underline">Agent library</RouterLink>.
        </div>
      </CardContent>
    </Card>

    <SaveAsAgentDialog
      v-model:open="saveAgentOpen"
      :prefill="{
        title: selectedAgent?.title || '',
        description: selectedAgent?.description || '',
        prompt: body,
        produces: 'findings',
        effort: effort,
        tags: selectedAgent?.tags || [],
      }"
      @created="onAgentSaved"
    />
  </div>
</template>
