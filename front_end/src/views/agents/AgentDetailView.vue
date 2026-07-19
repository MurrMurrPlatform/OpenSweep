<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { History, Play, Trash2 } from 'lucide-vue-next'
import { useAgentStore } from '@/stores/agentStore'
import { useCurrentRepo } from '@/composables/useCurrentRepo'
import { useToast } from '@/composables/useToast'
import { ApiError } from '@/services/api'
import { PageHeader } from '@/components/ui/page-header'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import MarkdownView from '@/components/ui/markdown/MarkdownView.vue'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import AgentRevisionsSheet from '@/components/agents/AgentRevisionsSheet.vue'
import ProducesBadge from '@/components/agents/ProducesBadge.vue'
import { PRODUCES_OPTIONS } from '@/lib/produces'
import type { AgentDTO, AgentEffort, ProducesKind } from '@/types/api'

const route = useRoute()
const router = useRouter()
const agents = useAgentStore()
const toast = useToast()
const { uid: repoUid } = useCurrentRepo()

const isCreate = computed(() => route.name === 'agent-create')
const agent = ref<AgentDTO | null>(null)
const loading = ref(true)
const saving = ref(false)
const dispatching = ref(false)
const revisionsOpen = ref(false)

// Form state
const title = ref('')
const description = ref('')
const prompt = ref('')
const produces = ref<ProducesKind>('findings')
const effort = ref<AgentEffort>('normal')
const tagsText = ref('')
const enabled = ref(true)

// System agents: the form edits the ORG OVERRIDE (mode append/replace).
const overrideMode = ref<'append' | 'replace'>('append')

const isSystem = computed(() => agent.value?.provenance === 'system')

onMounted(load)

async function load() {
  loading.value = true
  try {
    if (!isCreate.value) {
      agent.value = await agents.get(String(route.params.uid))
      hydrate(agent.value)
    }
  } finally {
    loading.value = false
  }
}

function hydrate(a: AgentDTO) {
  title.value = a.title
  description.value = a.description
  prompt.value = a.prompt
  produces.value = a.produces
  effort.value = a.default_effort
  tagsText.value = a.tags.join(', ')
  enabled.value = a.enabled
}

const dirty = computed(() => {
  if (isCreate.value) return title.value.trim().length > 0
  if (!agent.value) return false
  return (
    title.value !== agent.value.title ||
    description.value !== agent.value.description ||
    prompt.value !== agent.value.prompt ||
    produces.value !== agent.value.produces ||
    effort.value !== agent.value.default_effort ||
    tagsText.value !== agent.value.tags.join(', ') ||
    enabled.value !== agent.value.enabled
  )
})

function parsedTags(): string[] {
  return tagsText.value
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean)
}

async function save() {
  if (saving.value) return
  saving.value = true
  try {
    if (isCreate.value) {
      const created = await agents.create({
        title: title.value.trim(),
        description: description.value,
        prompt: prompt.value,
        produces: produces.value,
        default_effort: effort.value,
        tags: parsedTags(),
        enabled: enabled.value,
      })
      toast.success('Agent created')
      router.replace({ name: 'agent-detail', params: { uid: created.uid } })
      agent.value = created
      hydrate(created)
    } else if (agent.value && isSystem.value) {
      // Editing a system agent = saving an org override of its prompt.
      await agents.saveOverride(agent.value.uid, {
        mode: overrideMode.value,
        body: prompt.value,
      })
      toast.success('Org override saved', 'Your org now uses this tuning; the shared default is untouched.')
      agent.value = await agents.get(agent.value.uid)
      hydrate(agent.value)
    } else if (agent.value) {
      agent.value = await agents.update(agent.value.uid, {
        title: title.value.trim(),
        description: description.value,
        prompt: prompt.value,
        produces: produces.value,
        default_effort: effort.value,
        tags: parsedTags(),
        enabled: enabled.value,
      })
      hydrate(agent.value)
      toast.success('Agent saved')
    }
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t save', msg)
  } finally {
    saving.value = false
  }
}

async function restoreDefault() {
  if (!agent.value) return
  try {
    await agents.restoreDefault(agent.value.uid)
    toast.success('Platform default restored')
    agent.value = await agents.get(agent.value.uid)
    hydrate(agent.value)
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t restore default', msg)
  }
}

async function removeAgent() {
  if (!agent.value) return
  try {
    await agents.remove(agent.value.uid)
    toast.success('Agent deleted')
    router.push({ name: 'agent-library' })
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Couldn’t delete', msg)
  }
}

async function runNow() {
  if (!agent.value || !repoUid.value || dispatching.value) return
  dispatching.value = true
  try {
    const run = await agents.dispatch(agent.value.uid, { repository_uid: repoUid.value })
    toast.success('Run dispatched')
    router.push({ name: 'run-detail', params: { uid: run.uid } })
  } catch (e) {
    const msg = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
    toast.error('Dispatch failed', msg)
  } finally {
    dispatching.value = false
  }
}
</script>

<template>
  <div class="space-y-4">
    <template v-if="loading && !isCreate">
      <Skeleton class="h-12 w-2/3" />
      <Skeleton class="h-64" />
    </template>

    <template v-else>
      <PageHeader :title="isCreate ? 'New agent' : agent?.title || 'Agent'">
        <template #breadcrumb>
          <div v-if="agent" class="mb-1 flex items-center gap-2">
            <ProducesBadge :produces="agent.produces" />
            <Badge v-if="isSystem" variant="outline">System agent</Badge>
            <Badge v-if="agent.has_org_override" variant="info">Org override active</Badge>
          </div>
        </template>
        <Button
          v-if="agent && !isCreate"
          variant="outline"
          size="sm"
          @click="revisionsOpen = true"
        >
          <History /> History
        </Button>
        <Button
          v-if="agent && !isCreate && repoUid && agent.produces !== 'code-changes'"
          variant="outline"
          size="sm"
          :loading="dispatching"
          @click="runNow"
        >
          <Play /> Run now
        </Button>
        <Button size="sm" :disabled="!dirty" :loading="saving" @click="save">
          {{ isSystem ? 'Save org override' : 'Save' }}
        </Button>
      </PageHeader>

      <Card v-if="isSystem">
        <CardContent class="flex flex-wrap items-center justify-between gap-3 p-4 text-sm text-muted-foreground">
          <span>
            System agents are shared platform defaults. Editing the prompt here saves an
            <strong>override for your organization</strong> — the shared default stays untouched.
          </span>
          <div class="flex items-center gap-3">
            <div class="flex items-center gap-1.5">
              <Label class="text-xs">Mode</Label>
              <Select
                :model-value="overrideMode"
                @update:model-value="overrideMode = $event as 'append' | 'replace'"
              >
                <SelectTrigger class="h-8 w-32"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="append">Append</SelectItem>
                  <SelectItem value="replace">Replace</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <AlertDialog v-if="agent?.has_org_override">
              <AlertDialogTrigger as-child>
                <Button variant="outline" size="sm">Restore platform default</Button>
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle>Restore the platform default?</AlertDialogTitle>
                  <AlertDialogDescription>
                    Your org's override is retired (history is kept); runs go back to the
                    shared instructions.
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>Cancel</AlertDialogCancel>
                  <AlertDialogAction @click="restoreDefault">Restore</AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent class="space-y-4 p-4">
          <div class="grid gap-3 md:grid-cols-2">
            <div class="space-y-1.5">
              <Label for="agent-title">Name</Label>
              <Input id="agent-title" v-model="title" :disabled="isSystem" placeholder="Nightly security sweep" />
            </div>
            <div class="space-y-1.5">
              <Label for="agent-desc">Description</Label>
              <Input id="agent-desc" v-model="description" :disabled="isSystem" placeholder="What this agent is for" />
            </div>
          </div>

          <div class="space-y-1.5">
            <Label>Prompt</Label>
            <MarkdownView
              v-model="prompt"
              :editing="true"
              min-height="240px"
              placeholder="The agent's instructions — what to investigate, judge, or write…"
            />
          </div>

          <div class="grid gap-3 md:grid-cols-3">
            <div class="space-y-1.5">
              <Label>Produces</Label>
              <Select
                :model-value="produces"
                :disabled="isSystem"
                @update:model-value="produces = $event as ProducesKind"
              >
                <SelectTrigger class="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem v-for="o in PRODUCES_OPTIONS" :key="o.value" :value="o.value">
                    {{ o.label }} — {{ o.description }}
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div class="space-y-1.5">
              <Label>Default effort</Label>
              <Select
                :model-value="effort"
                :disabled="isSystem"
                @update:model-value="effort = $event as AgentEffort"
              >
                <SelectTrigger class="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="quick">Quick</SelectItem>
                  <SelectItem value="normal">Normal</SelectItem>
                  <SelectItem value="deep">Deep</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div class="space-y-1.5">
              <Label for="agent-tags">Tags (comma-separated)</Label>
              <Input id="agent-tags" v-model="tagsText" :disabled="isSystem" placeholder="security, weekly" />
            </div>
          </div>

          <div v-if="!isSystem && !isCreate" class="flex items-center justify-between border-t pt-4">
            <div class="flex items-center gap-2">
              <Switch v-model="enabled" />
              <Label>Enabled</Label>
            </div>
            <AlertDialog>
              <AlertDialogTrigger as-child>
                <Button variant="destructive" size="sm"><Trash2 /> Delete agent</Button>
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle>Delete this agent?</AlertDialogTitle>
                  <AlertDialogDescription>
                    Scheduled bindings must be removed first; run history is kept.
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>Cancel</AlertDialogCancel>
                  <AlertDialogAction @click="removeAgent">Delete</AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
          </div>
        </CardContent>
      </Card>

      <AgentRevisionsSheet
        v-if="agent"
        v-model:open="revisionsOpen"
        :agent-uid="agent.uid"
        :system="isSystem"
        @reverted="load"
      />
    </template>
  </div>
</template>
